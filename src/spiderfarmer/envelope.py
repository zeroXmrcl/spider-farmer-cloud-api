from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from spiderfarmer.errors import SpiderFarmerError

KEY = b"Meizhi1234567890"
IV = b"1234567890123456"
USER_AGENT = "Dart/3.5 (dart:io)"


def _pkcs7(raw: bytes) -> bytes:
    pad = 16 - (len(raw) % 16)
    return raw + bytes([pad]) * pad


def _unpad(raw: bytes) -> bytes:
    if not raw:
        raise SpiderFarmerError("decrypt", "empty")
    pad = raw[-1]
    if pad < 1 or pad > 16 or raw[-pad:] != bytes([pad]) * pad:
        raise SpiderFarmerError("decrypt", "bad padding")
    return raw[:-pad]


def encrypt_body(obj: dict[str, Any]) -> str:
    raw = _pkcs7(json.dumps(obj, separators=(",", ":")).encode())
    enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return base64.b64encode(enc.update(raw) + enc.finalize()).decode()


def decrypt_body(text: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except Exception as exc:
        raise SpiderFarmerError("decrypt", "not base64") from exc
    dec = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    try:
        plain = _unpad(dec.update(raw) + dec.finalize())
        parsed = json.loads(plain.decode())
    except SpiderFarmerError:
        raise
    except Exception as exc:
        raise SpiderFarmerError("decrypt", "bad ciphertext") from exc
    if not isinstance(parsed, dict):
        raise SpiderFarmerError("decrypt", "not an object")
    return parsed


def decode_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SpiderFarmerError("decrypt", "bad json") from exc
        if not isinstance(parsed, dict):
            raise SpiderFarmerError("decrypt", "not an object")
        return parsed
    return decrypt_body(stripped.strip('"'))


def systemdata(
    now_s: int,
    *,
    token: str | None = None,
    device_id: str = "spiderfarmer",
    app_version: str = "2.5.2",
    timezone: str = "Europe/Berlin",
    req_id: int | None = None,
) -> str:
    header: dict[str, Any] = {
        "reqId": now_s * 1000 if req_id is None else req_id,
        "appVersion": app_version,
        "osType": "iOS",
        "osVersion": "27.0",
        "deviceType": "iPhone",
        "deviceId": device_id,
        "netType": "wifi",
        "timestamp": now_s,
        "wifiName": "unknown_data",
    }
    if token:
        header["token"] = token
    header["timezone"] = timezone
    header["language"] = "English"
    return json.dumps(header, separators=(",", ":"))
