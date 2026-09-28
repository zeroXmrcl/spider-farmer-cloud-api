from __future__ import annotations

import json
import queue
import secrets
import ssl
import threading
import time
from pathlib import Path
from typing import Any

from spiderfarmer.errors import SpiderFarmerError
from spiderfarmer.models import Session

MQTT_HOST = "sf.mqtt.spider-farmer.com"
MQTT_PORT = 8883
PREFIXES = {"CB", "PS", "LC"}
_CLIENT_ID_LIMIT = 23


def bundled_ca() -> Path:
    return Path(__file__).resolve().parent / "certs" / "mqtt-ca.pem"


def topics(prefix: str, serial: str) -> tuple[str, str]:
    folded = prefix.upper()
    if folded not in PREFIXES:
        raise SpiderFarmerError("topic", f"unknown prefix {prefix!r}")
    clean = serial.replace(":", "").upper()
    if not clean:
        raise SpiderFarmerError("topic", "missing serial")
    return (
        f"SF/GGS/{folded}/API/UP/{clean}",
        f"SF/GGS/{folded}/API/DOWN/{clean}",
    )


def command(method: str, serial: str, params: dict[str, Any] | None, *, now_ms: int) -> dict[str, Any]:
    body: dict[str, Any] = {"pid": serial.replace(":", "").upper()}
    if params:
        body.update(params)
    return {"method": method, "params": body, "msgId": str(now_ms)}


def client_id(user_id: str, *, now_ms: int | None = None) -> str:
    """MQTT 3.1 client id, unique per call and at most 23 characters.

    Prefers ``{userId}_{unixMilliseconds}{nonce}``. A long user id keeps a
    short unique tail so two connects in the same second still differ.
    """
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    nonce = secrets.token_hex(2)
    suffix = f"{stamp}{nonce}"
    raw = f"{user_id}_{suffix}"
    if len(raw) <= _CLIENT_ID_LIMIT:
        return raw
    tail = f"{str(stamp)[-8:]}{nonce}"
    room = _CLIENT_ID_LIMIT - len(tail) - 1
    head = user_id[:room] if room > 0 else ""
    if not head:
        return tail[:_CLIENT_ID_LIMIT]
    return f"{head}_{tail}"


def ssl_context(ca_file: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(ca_file))
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        context.verify_flags &= ~strict
    return context


def _connack_failed(reason_code: Any) -> bool:
    failed = getattr(reason_code, "is_failure", None)
    if isinstance(failed, bool):
        return failed
    try:
        return int(reason_code) != 0
    except (TypeError, ValueError):
        return True


class MqttSession:
    """Cloud MQTT for one logged-in account.

    ``set*`` methods are refused unless ``writes=True`` on the session or on
    that ``publish`` call. Reading does not need the flag. ``connect`` blocks
    until CONNACK. Readings arrive on the ``UP`` topic and are returned by
    ``wait``.
    """

    def __init__(
        self,
        session: Session,
        *,
        writes: bool = False,
        ca_file: Path | None = None,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
    ) -> None:
        self.session = session
        self.writes = writes
        self.ca_file = ca_file or bundled_ca()
        self.host = host
        self.port = port
        self._client: Any = None
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._subscriptions: set[str] = set()
        self._connected = threading.Event()
        self._connect_error: str | None = None

    def connect(self, timeout: float = 10) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise SpiderFarmerError(
                "mqtt",
                "install with pip install 'spiderfarmer[mqtt]'",
            ) from exc

        if self._client is not None:
            self.close()
        self._inbox = queue.Queue()
        self._connected.clear()
        self._connect_error = None
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id(self.session.user_id),
            protocol=mqtt.MQTTv31,
            clean_session=True,
        )
        client.tls_set_context(ssl_context(self.ca_file))
        client.username_pw_set(self.session.mqtt_name, self.session.mqtt_pwd)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client
        try:
            client.connect(self.host, self.port, keepalive=30)
            client.loop_start()
            if not self._connected.wait(timeout):
                raise SpiderFarmerError("mqtt", "connect timeout")
            if self._connect_error is not None:
                raise SpiderFarmerError("mqtt", self._connect_error)
        except SpiderFarmerError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise SpiderFarmerError("mqtt", "connect failed") from exc

    def subscribe(self, prefix: str, serial: str) -> tuple[str, str]:
        up, down = topics(prefix, serial)
        self._subscriptions.add(up)
        result, _mid = self._require().subscribe(up, qos=0)
        if result != 0:
            self._subscriptions.discard(up)
            raise SpiderFarmerError("mqtt", f"subscribe failed ({result})")
        return up, down

    def publish(
        self,
        prefix: str,
        serial: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        writes: bool | None = None,
        now_ms: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        allow = self.writes if writes is None else writes
        if method.startswith("set") and not allow:
            raise SpiderFarmerError("write", "pass writes=True to publish set* commands")
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise SpiderFarmerError(
                "mqtt",
                "install with pip install 'spiderfarmer[mqtt]'",
            ) from exc
        _up, down = topics(prefix, serial)
        payload = command(
            method,
            serial,
            params,
            now_ms=int(time.time() * 1000) if now_ms is None else now_ms,
        )
        info = self._require().publish(down, json.dumps(payload, separators=(",", ":")), qos=0)
        rc = getattr(info, "rc", None)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            raise SpiderFarmerError("mqtt", f"publish failed ({rc})")
        return down, payload

    def wait(self, timeout: float = 10, topic: str | None = None) -> dict[str, Any]:
        """Return the next ``UP`` message as ``{"topic", "payload"}``.

        ``topic`` skips messages for other devices and leaves them queued.
        """
        self._require()
        deadline = time.monotonic() + timeout
        skipped: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SpiderFarmerError("mqtt", "no message")
                try:
                    item = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise SpiderFarmerError("mqtt", "no message") from None
                if topic is None or item["topic"] == topic:
                    return item
                skipped.append(item)
        finally:
            for item in skipped:
                self._inbox.put(item)

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
        finally:
            client.loop_stop()

    def _on_connect(
        self,
        client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        if _connack_failed(reason_code):
            self._connect_error = f"connect refused ({reason_code})"
        else:
            for topic in tuple(self._subscriptions):
                client.subscribe(topic, qos=0)
        self._connected.set()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        raw = message.payload
        text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        self._inbox.put({"topic": message.topic, "payload": payload})

    def _require(self) -> Any:
        if self._client is None:
            raise SpiderFarmerError("mqtt", "not connected")
        return self._client
