from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wxbot import __version__
from wxbot.api.client import ILinkClient, ILinkError, encoded_client_version, random_wechat_uin


class ApiClientTests(unittest.TestCase):
    def test_send_text_file_uploads_and_sends_exact_protocol_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/getuploadurl"):
                return httpx.Response(
                    200,
                    json={"ret": 0, "upload_full_url": "https://novac2c.cdn.weixin.qq.com/upload"},
                )
            if request.url.path == "/upload":
                return httpx.Response(
                    200, headers={"x-encrypted-param": "download-param"},
                )
            return httpx.Response(200, json={"ret": 0})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.txt"
            content = "你好。".encode("utf-8")
            path.write_bytes(content)
            with ILinkClient(transport=httpx.MockTransport(handler)) as client:
                client.send_text_file(
                    to="user",
                    path=path,
                    file_name="notes.txt",
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                    context_token="ctx",
                    client_id="cid",
                )

        self.assertEqual(len(requests), 3)
        upload_request = json.loads(requests[0].content)
        self.assertEqual(upload_request["media_type"], 3)
        self.assertEqual(upload_request["to_user_id"], "user")
        self.assertEqual(upload_request["rawsize"], len(content))
        self.assertEqual(upload_request["filesize"], 16)
        self.assertTrue(upload_request["no_need_thumb"])
        self.assertRegex(upload_request["filekey"], r"^[0-9a-f]{32}$")
        self.assertRegex(upload_request["aeskey"], r"^[0-9a-f]{32}$")

        ciphertext = requests[1].content
        key = bytes.fromhex(upload_request["aeskey"])
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        self.assertEqual(padded[:-padded[-1]], content)
        self.assertEqual(requests[1].method, "POST")
        self.assertEqual(
            requests[1].headers["Content-Type"], "application/octet-stream",
        )

        send_body = json.loads(requests[2].content)
        self.assertEqual(send_body["msg"]["item_list"], [{
            "type": 4,
            "file_item": {
                "media": {
                    "encrypt_query_param": "download-param",
                    "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
                    "encrypt_type": 1,
                },
                "file_name": "notes.txt",
                "len": str(len(content)),
            },
        }])

    def test_send_text_file_does_not_retry_uncertain_cdn_upload(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/getuploadurl"):
                return httpx.Response(
                    200,
                    json={"upload_full_url": "https://novac2c.cdn.weixin.qq.com/upload"},
                )
            raise httpx.ReadTimeout("timeout", request=request)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.txt"
            content = b"test"
            path.write_bytes(content)
            with ILinkClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(ILinkError, "结果不确定"):
                    client.send_text_file(
                        to="user",
                        path=path,
                        file_name="notes.txt",
                        expected_sha256=hashlib.sha256(content).hexdigest(),
                        context_token="ctx",
                        client_id="cid",
                    )

        self.assertEqual(len(requests), 2)

    def test_random_wechat_uin_is_base64_decimal_uint32(self) -> None:
        values = {random_wechat_uin() for _ in range(20)}
        self.assertGreater(len(values), 1)
        for encoded in values:
            decoded = base64.b64decode(encoded).decode("utf-8")
            self.assertTrue(decoded.isdecimal())
            self.assertGreaterEqual(int(decoded), 0)
            self.assertLessEqual(int(decoded), 0xFFFFFFFF)

    def test_client_version_encoding(self) -> None:
        self.assertEqual(encoded_client_version("0.1.0"), 256)
        self.assertEqual(encoded_client_version("1.2.3"), 0x010203)

    def test_get_updates_fields_and_headers(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json={
                    "ret": 0,
                    "msgs": [
                        {
                            "message_id": 7,
                            "from_user_id": "user@im.wechat",
                            "to_user_id": "bot@im.bot",
                            "message_type": 1,
                            "context_token": "ctx",
                            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                        }
                    ],
                    "get_updates_buf": "next",
                    "longpolling_timeout_ms": 12000,
                },
            )

        with ILinkClient(
            base_url="https://example.test/base",
            token="secret-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            updates = client.get_updates("cursor")

        request = captured["request"]
        assert isinstance(request, httpx.Request)
        self.assertEqual(str(request.url), "https://example.test/base/ilink/bot/getupdates")
        self.assertEqual(request.headers["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(request.headers["Authorization"], "Bearer secret-token")
        self.assertEqual(request.headers["iLink-App-Id"], "bot")
        self.assertEqual(
            request.headers["iLink-App-ClientVersion"],
            str(encoded_client_version(__version__)),
        )
        body = json.loads(request.content)
        self.assertEqual(body["get_updates_buf"], "cursor")
        self.assertEqual(body["base_info"]["channel_version"], __version__)
        self.assertEqual(body["base_info"]["bot_agent"], f"wxbot/{__version__}")
        self.assertEqual(updates.cursor, "next")
        self.assertEqual(updates.timeout_ms, 12000)
        self.assertEqual(updates.messages[0].text, "你好")

    def test_send_text_exact_body(self) -> None:
        bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            client.send_text(to="user", text="reply", context_token="ctx", client_id="cid")

        self.assertEqual(
            bodies[0]["msg"],
            {
                "from_user_id": "",
                "to_user_id": "user",
                "client_id": "cid",
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": "reply"}}],
                "context_token": "ctx",
            },
        )

    def test_send_without_context_is_rejected_before_http(self) -> None:
        called = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ValueError):
                client.send_text(to="user", text="reply", context_token="", client_id="cid")
        self.assertFalse(called)

    def test_send_preserves_stable_client_id_for_server_deduplication(self) -> None:
        requests: list[dict[str, object]] = []
        delivered: set[str] = set()

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            requests.append(payload)
            delivered.add(payload["msg"]["client_id"])
            return httpx.Response(200, json={"ret": 0})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(2):
                client.send_text(
                    to="user",
                    text="reply",
                    context_token="ctx",
                    client_id="wxbot:auto:message-42",
                )

        self.assertEqual(len(requests), 2)
        self.assertEqual(
            [request["msg"]["client_id"] for request in requests],
            ["wxbot:auto:message-42"] * 2,
        )
        self.assertEqual(delivered, {"wxbot:auto:message-42"})

    def test_session_expired_preserves_error_code(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"ret": -14, "errmsg": "session timeout"})
        )
        with ILinkClient(transport=transport) as client:
            with self.assertRaises(ILinkError) as caught:
                client.get_updates("")
        self.assertEqual(caught.exception.code, -14)

    def test_get_updates_timeout_keeps_cursor_without_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("long poll timeout", request=request)

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            updates = client.get_updates("cursor", timeout_ms=100)

        self.assertEqual(updates.messages, ())
        self.assertEqual(updates.cursor, "cursor")
        self.assertEqual(updates.timeout_ms, 100)

    def test_get_updates_accepts_explicit_empty_cursor_reset(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"ret": 0, "msgs": [], "get_updates_buf": ""},
            )
        )
        with ILinkClient(transport=transport) as client:
            updates = client.get_updates("old-cursor")

        self.assertEqual(updates.cursor, "")

    def test_network_error_is_normalized_without_request_details(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("failed https://secret.example", request=request)

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ILinkError, "网络请求失败") as caught:
                client.get_updates("cursor")

        self.assertNotIn("secret.example", str(caught.exception))

    def test_http_5xx_is_reported_without_response_body(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(503, text="internal sensitive details")
        )
        with ILinkClient(transport=transport) as client:
            with self.assertRaisesRegex(ILinkError, "HTTP 503") as caught:
                client.get_updates("cursor")

        self.assertNotIn("sensitive", str(caught.exception))

    def test_invalid_json_is_reported_without_response_body(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, text="not-json-sensitive")
        )
        with ILinkClient(transport=transport) as client:
            with self.assertRaisesRegex(ILinkError, "无效 JSON") as caught:
                client.get_updates("cursor")

        self.assertNotIn("not-json-sensitive", str(caught.exception))

    def test_qrcode_request_uses_post_and_status_escapes_query(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "get_bot_qrcode" in str(request.url):
                return httpx.Response(200, json={"qrcode": "a+b", "qrcode_img_content": "url"})
            return httpx.Response(200, json={"status": "wait"})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            client.create_qrcode()
            client.qrcode_status("a+b", "12 34")
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(json.loads(requests[0].content), {"local_token_list": []})
        self.assertIn("qrcode=a%2Bb", str(requests[1].url))
        self.assertIn("verify_code=12%2034", str(requests[1].url))
        self.assertEqual(requests[1].headers["iLink-App-Id"], "bot")
        self.assertNotIn("AuthorizationType", requests[1].headers)
        self.assertNotIn("X-WECHAT-UIN", requests[1].headers)


if __name__ == "__main__":
    unittest.main()
