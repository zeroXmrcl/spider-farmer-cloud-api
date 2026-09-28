from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from spiderfarmer.errors import SpiderFarmerError

_TOKEN = re.compile(r"[A-Z0-9]+")
_STRIPS = {"PS10", "AC10", "PS5", "AC5", "PS"}
_ROOM_KEYS = ("belonginRroom", "belonginRoom", "belongInRoom")


@dataclass(frozen=True)
class Session:
    """Credentials returned by mail login. ``mqtt_pwd`` is the broker password, not the account password."""

    token: str
    mqtt_name: str
    mqtt_pwd: str
    user_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Room:
    id: int
    name: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Device:
    serial: str
    name: str
    product_type: str
    prefix: str
    room_id: int | None
    online: bool | None
    raw: dict[str, Any]


def topic_prefix(product_type: str) -> str:
    """MQTT topic prefix for a ``productType`` string.

    Light controllers are ``LC``, strips ``PS``, everything else ``CB``.
    Matching uses tokens, so a name like ``UPS`` is not treated as a strip.
    S-Station and Display Panel types are not mapped yet, so they stay ``CB``.
    """
    tokens = _TOKEN.findall(product_type.upper())
    if "LC" in tokens or any("LIGHT" in token for token in tokens):
        return "LC"
    if any(token in _STRIPS for token in tokens):
        return "PS"
    return "CB"


def session_from_login(payload: dict[str, Any]) -> Session:
    data = _data_object(payload)
    mqtt_name = str(data.get("mqttName") or "").strip()
    mqtt_pwd = str(data.get("mqttPwd") or "").strip()
    token = str(data.get("token") or "").strip()
    user_id = str(data.get("userId") or "").strip()
    if not mqtt_name or not mqtt_pwd:
        raise _missing("missing mqtt credentials")
    if not token:
        raise _missing("missing token")
    if not user_id:
        raise _missing("missing userId")
    return Session(
        token=token,
        mqtt_name=mqtt_name,
        mqtt_pwd=mqtt_pwd,
        user_id=user_id,
        raw=data,
    )


def room_from(record: dict[str, Any]) -> Room:
    room_id = _as_int(record.get("id"))
    if room_id is None:
        raise _missing("room missing id")
    return Room(id=room_id, name=str(record.get("roomName") or ""), raw=record)


def device_from(record: dict[str, Any]) -> Device:
    serial = str(record.get("deviceSerialnum") or "").replace(":", "").upper()
    product_type = str(record.get("productType") or "")
    if not serial or not product_type:
        raise _missing("device missing serial or productType")
    status = record.get("connectStatus")
    return Device(
        serial=serial,
        name=str(record.get("deviceName") or serial),
        product_type=product_type,
        prefix=topic_prefix(product_type),
        room_id=_room_id(record),
        online=_online(status),
        raw=record,
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text.startswith("-") else text
        if digits.isdigit():
            return int(text)
    return None


def _room_id(record: dict[str, Any]) -> int | None:
    for key in _ROOM_KEYS:
        if key in record:
            return _as_int(record.get(key))
    return None


def _online(status: Any) -> bool | None:
    if status is None:
        return None
    if isinstance(status, bool):
        return status
    if isinstance(status, int):
        return status == 1
    if isinstance(status, str):
        text = status.strip()
        digits = text[1:] if text.startswith("-") else text
        if digits.isdigit():
            return int(text) == 1
    return None


def _data_object(payload: dict[str, Any]) -> dict[str, Any]:
    code = "" if payload.get("code") is None else str(payload.get("code"))
    if code != "000":
        msg = payload.get("msg")
        raise SpiderFarmerError(code or "?", str(msg) if msg is not None else "request failed")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SpiderFarmerError("parse", "missing data")
    return data


def _missing(msg: str) -> SpiderFarmerError:
    return SpiderFarmerError("parse", msg)
