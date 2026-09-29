import json
import unittest
import uuid
from unittest.mock import patch

import requests

from spiderfarmer import Client, SpiderFarmerError
from spiderfarmer.envelope import decode_response, decrypt_body, encrypt_body
from spiderfarmer.models import Session, device_from, room_from


def _response(text: str, status: int = 200):
    class Response:
        def __init__(self) -> None:
            self.text = text
            self.status_code = status

    return Response()


def _session() -> Session:
    return Session(token="rest-token", mqtt_name="a@b.c", mqtt_pwd="broker-secret", user_id="42", raw={})


class LoginTests(unittest.TestCase):
    def test_posts_encrypted_ios_v2_body(self) -> None:
        seen: dict[str, object] = {}

        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            seen["url"] = url
            seen["data"] = data
            seen["headers"] = headers
            seen["timeout"] = timeout
            return _response(
                encrypt_body(
                    {
                        "code": "000",
                        "msg": "success",
                        "data": {
                            "mqttName": "a@b.c",
                            "mqttPwd": "broker-secret",
                            "userId": 42,
                            "token": "rest-token",
                        },
                    }
                )
            )

        session = Client(post=post).login("a@b.c", "secret", now_s=1_700_000_000)
        self.assertEqual(seen["url"], "https://api.spider-farmer.com/api/ios/ulogin/mailLogin/v2")
        self.assertEqual(seen["timeout"], 20)
        headers = seen["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(headers["User-Agent"], "Dart/3.5 (dart:io)")
        system = json.loads(headers["systemdata"])
        self.assertEqual(system["appVersion"], "2.5.2")
        self.assertEqual(system["osType"], "iOS")
        self.assertEqual(system["timestamp"], 1_700_000_000)
        self.assertEqual(system["reqId"], 1_700_000_000_000)
        self.assertNotIn("token", system)
        self.assertEqual(
            decrypt_body(seen["data"]),
            {"email": "a@b.c", "loginMethod": 1, "password": "secret"},
        )
        self.assertIsInstance(decrypt_body(seen["data"])["loginMethod"], int)
        self.assertEqual(session.user_id, "42")
        self.assertEqual(session.mqtt_name, "a@b.c")
        self.assertEqual(session.token, "rest-token")

    def test_bad_password_does_not_echo_the_password(self) -> None:
        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            return _response(json.dumps({"code": "100", "msg": "The password entered is incorrect.", "data": None}))

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=post).login("a@b.c", "secret-value", now_s=1)
        self.assertEqual(caught.exception.code, "100")
        self.assertNotIn("secret-value", str(caught.exception))

    def test_missing_user_id_is_an_error(self) -> None:
        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            return _response(
                encrypt_body(
                    {
                        "code": "000",
                        "data": {"mqttName": "a@b.c", "mqttPwd": "broker-secret", "token": "rest-token"},
                    }
                )
            )

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=post).login("a@b.c", "secret", now_s=1)
        self.assertEqual(caught.exception.msg, "missing userId")


class InventoryTests(unittest.TestCase):
    def test_back_to_back_calls_do_not_reuse_req_id(self) -> None:
        seen: list[int] = []

        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            seen.append(json.loads(headers["systemdata"])["reqId"])
            return _response(encrypt_body({"code": "000", "data": []}))

        client = Client(post=post)
        with patch("spiderfarmer.client._request_clock_ms", return_value=5_000):
            client.rooms(_session())
            client.rooms(_session())
        self.assertEqual(seen, [5_000, 5_001])


    def test_rooms_use_an_empty_body_and_read_the_name(self) -> None:
        seen: dict[str, object] = {}

        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            seen["url"] = url
            seen["data"] = data
            seen["headers"] = headers
            return _response(
                encrypt_body(
                    {
                        "code": "000",
                        "data": [{"id": 7, "userId": 42, "roomName": "Tent", "isDelete": 0}],
                    }
                )
            )

        rooms = Client(post=post).rooms(_session(), now_s=50)
        self.assertIsNone(seen["data"])
        self.assertIn("token", json.loads(seen["headers"]["systemdata"]))
        self.assertEqual(rooms[0].id, 7)
        self.assertEqual(rooms[0].name, "Tent")
        self.assertTrue(seen["url"].endswith("/api/ios/udm/getUserRooms/v2"))

    def test_devices_send_room_and_user_ids(self) -> None:
        seen: dict[str, object] = {}

        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            seen["data"] = data
            return _response(
                encrypt_body(
                    {
                        "code": "000",
                        "data": [
                            {
                                "deviceSerialnum": "AABBCCDDEEFF",
                                "deviceName": "Control",
                                "productType": "SF-GGS-CB",
                                "belonginRroom": 7,
                                "connectStatus": 1,
                            }
                        ],
                    }
                )
            )

        devices = Client(post=post).devices(_session(), 7, now_s=50)
        self.assertEqual(decrypt_body(seen["data"]), {"belonginRoomId": 7, "userId": 42})
        self.assertEqual(devices[0].serial, "AABBCCDDEEFF")
        self.assertEqual(devices[0].prefix, "CB")
        self.assertEqual(devices[0].room_id, 7)
        self.assertTrue(devices[0].online)


class RecordTests(unittest.TestCase):
    def test_string_room_id_and_alternate_device_keys(self) -> None:
        room = room_from({"id": "7", "roomName": "Tent"})
        self.assertEqual(room.id, 7)
        device = device_from(
            {
                "deviceSerialnum": "ABC",
                "productType": "SF-GGS-CB",
                "belonginRoom": "7",
                "connectStatus": "1",
            }
        )
        self.assertEqual(device.room_id, 7)
        self.assertTrue(device.online)
        spelled = device_from(
            {
                "deviceSerialnum": "ABC",
                "productType": "SF-GGS-CB",
                "belongInRoom": 4,
            }
        )
        self.assertEqual(spelled.room_id, 4)
        self.assertIsNone(spelled.online)

    def test_cloud_room_typo_wins(self) -> None:
        device = device_from(
            {
                "deviceSerialnum": "ABC",
                "productType": "SF-GGS-CB",
                "belonginRroom": 3,
                "belonginRoom": 9,
            }
        )
        self.assertEqual(device.room_id, 3)

    def test_bad_room_id_is_parse(self) -> None:
        with self.assertRaises(SpiderFarmerError) as caught:
            room_from({"id": True, "roomName": "Tent"})
        self.assertEqual(caught.exception.code, "parse")
        self.assertEqual(caught.exception.msg, "room missing id")


class TransportTests(unittest.TestCase):
    def test_timeout_tls_dns_and_http_message(self) -> None:
        def timeout(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            raise requests.Timeout("slow")

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=timeout).login("a@b.c", "secret", now_s=1)
        self.assertEqual(caught.exception.code, "timeout")

        def tls(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            raise requests.exceptions.SSLError("bad cert")

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=tls).login("a@b.c", "secret", now_s=1)
        self.assertEqual(caught.exception.code, "tls")

        def dns(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            raise requests.ConnectionError("HTTPSConnectionPool: getaddrinfo failed")

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=dns).login("a@b.c", "secret", now_s=1)
        self.assertEqual(caught.exception.code, "dns")

        def refused(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            raise requests.ConnectionError("connection refused")

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=refused).login("a@b.c", "secret", now_s=1)
        self.assertEqual((caught.exception.code, caught.exception.msg), ("network", "unreachable"))

        def locked(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            return _response(json.dumps({"code": "100", "msg": "locked"}), status=400)

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=locked).login("a@b.c", "secret", now_s=1)
        self.assertEqual((caught.exception.code, caught.exception.msg), ("400", "locked"))

        def html(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            return _response("<html>nope</html>", status=502)

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=html).login("a@b.c", "secret", now_s=1)
        self.assertEqual((caught.exception.code, caught.exception.msg), ("502", "http"))

    def test_device_id_defaults_to_a_uuid(self) -> None:
        seen: dict[str, object] = {}

        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            seen["headers"] = headers
            return _response(
                encrypt_body(
                    {
                        "code": "000",
                        "data": {
                            "mqttName": "a@b.c",
                            "mqttPwd": "broker-secret",
                            "userId": 42,
                            "token": "rest-token",
                        },
                    }
                )
            )

        client = Client(post=post)
        client.login("a@b.c", "secret", now_s=1)
        system = json.loads(seen["headers"]["systemdata"])
        self.assertEqual(system["deviceId"], client.device_id)
        self.assertNotEqual(client.device_id, "spiderfarmer")
        uuid.UUID(client.device_id)
        pinned = Client(device_id="phone-1", post=post)
        pinned.login("a@b.c", "secret", now_s=1)
        self.assertEqual(json.loads(seen["headers"]["systemdata"])["deviceId"], "phone-1")

    def test_non_integer_user_id_is_parse(self) -> None:
        session = Session(token="rest-token", mqtt_name="a@b.c", mqtt_pwd="broker-secret", user_id="nope", raw={})

        def post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
            raise AssertionError("posted")

        with self.assertRaises(SpiderFarmerError) as caught:
            Client(post=post).devices(session, 7)
        self.assertEqual(caught.exception.code, "parse")


class EnvelopeTests(unittest.TestCase):
    def test_response_shapes(self) -> None:
        body = {"code": "000", "msg": "ok"}
        self.assertEqual(decode_response(json.dumps(body)), body)
        cipher = encrypt_body(body)
        self.assertEqual(decode_response(cipher), body)
        self.assertEqual(decode_response(json.dumps(cipher)), body)

    def test_block_aligned_plaintext_still_roundtrips(self) -> None:
        body = {"email": "abcd@ef.gh", "loginMethod": 1, "password": "0123456789abcdef0123456789ab"}
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.assertEqual(len(raw) % 16, 0)
        self.assertEqual(decrypt_body(encrypt_body(body)), body)
