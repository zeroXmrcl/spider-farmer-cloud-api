import json
import ssl
import sys
import threading
import unittest
from unittest.mock import patch

from spiderfarmer import MqttSession, __version__
from spiderfarmer.errors import SpiderFarmerError
from spiderfarmer.models import Session, topic_prefix
from spiderfarmer.mqtt import MqttSession as MqttSessionDirect
from spiderfarmer.mqtt import bundled_ca, client_id, command, ssl_context, topics


def _session() -> Session:
    return Session(token="rest-token", mqtt_name="a@b.c", mqtt_pwd="broker-secret", user_id="42", raw={})


class TopicTests(unittest.TestCase):
    def test_prefixes(self) -> None:
        self.assertEqual(topic_prefix("SF-GGS-CB"), "CB")
        self.assertEqual(topic_prefix("SF-GGS-LC"), "LC")
        self.assertEqual(topic_prefix("SF-GGS-PS10"), "PS")
        self.assertEqual(topic_prefix("UPS"), "CB")
        self.assertEqual(topic_prefix("SF-LIGHT"), "LC")

    def test_topic_strings(self) -> None:
        up, down = topics("cb", "aa:bb:cc:dd:ee:ff")
        self.assertEqual(up, "SF/GGS/CB/API/UP/AABBCCDDEEFF")
        self.assertEqual(down, "SF/GGS/CB/API/DOWN/AABBCCDDEEFF")

    def test_unknown_prefix(self) -> None:
        with self.assertRaises(SpiderFarmerError):
            topics("ZZ", "ABC")


class PublishTests(unittest.TestCase):
    def test_get_dev_sta_payload(self) -> None:
        payload = command("getDevSta", "AABBCCDDEEFF", None, now_ms=1234)
        self.assertEqual(
            payload,
            {"method": "getDevSta", "params": {"pid": "AABBCCDDEEFF"}, "msgId": "1234"},
        )

    def test_set_is_refused_until_writes_are_enabled(self) -> None:
        session = MqttSession(_session())

        class Fake:
            def publish(self, topic, payload, qos=0):  # noqa: ANN001
                self.topic = topic
                self.payload = payload
                return _Info()

        session._client = Fake()
        with self.assertRaises(SpiderFarmerError) as caught:
            session.publish("CB", "ABC", "setLight", now_ms=1)
        self.assertEqual(caught.exception.code, "write")
        down, payload = session.publish("CB", "ABC", "setLight", {"level": 10}, writes=True, now_ms=5)
        self.assertEqual(down, "SF/GGS/CB/API/DOWN/ABC")
        self.assertEqual(json.loads(session._client.payload)["method"], "setLight")
        self.assertEqual(payload["params"]["level"], 10)


class ClientIdTests(unittest.TestCase):
    def test_same_second_ids_differ_and_fit_mqtt_31(self) -> None:
        with patch("spiderfarmer.mqtt.time.time", return_value=1_700_000_000):
            first = client_id("42")
            second = client_id("42")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("42_"))
        self.assertIn("1700000000000", first)
        self.assertLessEqual(len(first), 23)
        self.assertLessEqual(len(second), 23)
        long_id = client_id("123456789012345678901234")
        self.assertLessEqual(len(long_id), 23)
        self.assertGreaterEqual(len(long_id), 1)


class _Info:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc


class _Reason:
    def __init__(self, failure: bool, label: str) -> None:
        self.is_failure = failure
        self.label = label

    def __str__(self) -> str:
        return self.label


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeClient:
    def __init__(self, *, callback_api_version, client_id, protocol, clean_session) -> None:  # noqa: ANN001
        self.callback_api_version = callback_api_version
        self.client_id = client_id
        self.protocol = protocol
        self.clean_session = clean_session
        self.on_connect = None
        self.on_message = None
        self.calls: list[str] = []
        self.subs: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.fire = "ok"
        self.publish_rc = 0
        self.host = None

    def tls_set_context(self, _context) -> None:  # noqa: ANN001
        self.calls.append("tls")

    def username_pw_set(self, username, password) -> None:  # noqa: ANN001
        self.username = username
        self.password = password

    def connect(self, host, port, keepalive) -> None:  # noqa: ANN001
        self.host = host
        self.port = port
        self.keepalive = keepalive
        self.calls.append("connect")

    def loop_start(self) -> None:
        self.calls.append("loop_start")
        if self.fire == "ok":
            self._accept()
        elif self.fire == "refuse":
            self.on_connect(self, None, {}, _Reason(True, "Not authorized"), None)

    def _accept(self) -> None:
        self.on_connect(self, None, {}, 0, None)

    def subscribe(self, topic, qos=0):  # noqa: ANN001
        self.subs.append(topic)
        return (0, 1)

    def publish(self, topic, payload, qos=0):  # noqa: ANN001
        self.published.append((topic, payload))
        return _Info(self.publish_rc)

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")


def _capturing(fire: str, holder: list[FakeClient]) -> type[FakeClient]:
    class Capturing(FakeClient):
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            super().__init__(**kwargs)
            self.fire = fire
            holder.append(self)

    return Capturing


class HandshakeTests(unittest.TestCase):
    def test_connect_waits_for_connack_and_wait_returns_up(self) -> None:
        session = MqttSessionDirect(_session())
        holder: list[FakeClient] = []
        waiting = threading.Event()
        real_wait = session._connected.wait

        def mark_wait(timeout: float | None = None) -> bool:
            waiting.set()
            return real_wait(timeout)

        session._connected.wait = mark_wait  # type: ignore[method-assign]
        done = threading.Event()
        error: list[BaseException] = []

        def run() -> None:
            try:
                session.connect(timeout=2)
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)
            finally:
                done.set()

        with patch("paho.mqtt.client.Client", _capturing("silent", holder)):
            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(waiting.wait(2))
            self.assertFalse(done.is_set())
            holder[0]._accept()
            self.assertTrue(done.wait(2))
        self.assertEqual(error, [])
        fake = holder[0]
        self.assertLessEqual(len(fake.client_id), 23)
        self.assertEqual(fake.username, "a@b.c")
        up, _down = session.subscribe("CB", "AABBCCDDEEFF")
        session.publish("CB", "AABBCCDDEEFF", "getDevSta", now_ms=9)
        fake.on_message(fake, None, _Message(up, b'{"temp":22}'))
        reading = session.wait(timeout=1)
        self.assertEqual(reading, {"topic": up, "payload": {"temp": 22}})
        session.close()
        self.assertEqual(fake.calls[-2:], ["disconnect", "loop_stop"])

    def test_bad_connack_and_timeout_fail(self) -> None:
        refused = MqttSessionDirect(_session())
        holder: list[FakeClient] = []
        with patch("paho.mqtt.client.Client", _capturing("refuse", holder)):
            with self.assertRaises(SpiderFarmerError) as caught:
                refused.connect(timeout=1)
        self.assertEqual(caught.exception.code, "mqtt")
        self.assertIn("Not authorized", caught.exception.msg)
        self.assertEqual(holder[0].calls[-2:], ["disconnect", "loop_stop"])
        self.assertIsNone(refused._client)

        silent = MqttSessionDirect(_session())
        holder.clear()
        with patch("paho.mqtt.client.Client", _capturing("silent", holder)):
            with self.assertRaises(SpiderFarmerError) as caught:
                silent.connect(timeout=0.05)
        self.assertEqual(caught.exception.msg, "connect timeout")
        self.assertEqual(holder[0].calls[-2:], ["disconnect", "loop_stop"])

    def test_publish_rc_and_resubscribe_and_topic_filter(self) -> None:
        session = MqttSessionDirect(_session())
        holder: list[FakeClient] = []
        with patch("paho.mqtt.client.Client", _capturing("ok", holder)):
            session.connect(timeout=1)
        fake = holder[0]
        up, _down = session.subscribe("CB", "ABC")
        fake.on_connect(fake, None, {}, 0, None)
        self.assertEqual(fake.subs, [up, up])
        fake.publish_rc = 4
        with self.assertRaises(SpiderFarmerError) as caught:
            session.publish("CB", "ABC", "getDevSta", now_ms=1)
        self.assertIn("publish failed (4)", caught.exception.msg)
        fake.publish_rc = 0
        other = "SF/GGS/CB/API/UP/OTHER"
        fake.on_message(fake, None, _Message(other, b'{"n":1}'))
        fake.on_message(fake, None, _Message(up, b'{"n":2}'))
        self.assertEqual(session.wait(timeout=1, topic=up)["payload"], {"n": 2})
        self.assertEqual(session.wait(timeout=1)["payload"], {"n": 1})
        with self.assertRaises(SpiderFarmerError) as caught:
            session.wait(timeout=0.01)
        self.assertEqual(caught.exception.msg, "no message")
        session.close()

    def test_missing_paho_names_the_extra(self) -> None:
        session = MqttSessionDirect(_session())
        with patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with self.assertRaises(SpiderFarmerError) as caught:
                session.connect(timeout=0.1)
        self.assertEqual(caught.exception.code, "mqtt")
        self.assertIn("spiderfarmer[mqtt]", caught.exception.msg)

    def test_package_exports_session_and_version(self) -> None:
        self.assertIs(MqttSession, MqttSessionDirect)
        self.assertEqual(__version__, "0.1.0")


class TlsTests(unittest.TestCase):
    def test_context_requires_the_pinned_ca_and_drops_strict(self) -> None:
        context = ssl_context(bundled_ca())
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict:
            self.assertEqual(context.verify_flags & strict, 0)
