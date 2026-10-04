from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from wxbot.api.client import ILinkError
from wxbot.auth.qr_login import display_qrcode, login
from wxbot.storage.session_store import SessionState


class FakeClient:
    instances: list["FakeClient"] = []
    statuses: list[dict[str, object]] = []
    local_tokens: list[list[str]] = []
    qrcode_count = 0

    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def create_qrcode(self, local_tokens: list[str] | None = None) -> dict[str, str]:
        self.__class__.local_tokens.append(local_tokens or [])
        self.__class__.qrcode_count += 1
        number = self.__class__.qrcode_count
        return {"qrcode": f"qr-id-{number}", "qrcode_img_content": f"qr-content-{number}"}

    def qrcode_status(self, _qrcode: str, _verify_code: str | None = None) -> dict[str, object]:
        return self.__class__.statuses.pop(0)


class LoginTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instances = []
        FakeClient.statuses = []
        FakeClient.local_tokens = []
        FakeClient.qrcode_count = 0

    def test_display_qrcode_does_not_print_raw_qrcode_content(self) -> None:
        class FakeQr:
            def add_data(self, _content: str) -> None:
                pass

            def make(self, *, fit: bool) -> None:
                self.fit = fit

            def print_ascii(self, *, invert: bool) -> None:
                print("[QR]")

        output = io.StringIO()
        with patch(
            "wxbot.auth.qr_login.qrcode.QRCode", return_value=FakeQr()
        ), contextlib.redirect_stdout(output):
            display_qrcode("raw-qrcode-secret")

        self.assertIn("[QR]", output.getvalue())
        self.assertNotIn("raw-qrcode-secret", output.getvalue())

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_confirmed_login(self) -> None:
        FakeClient.statuses = [
            {"status": "wait"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "bot_token": "token",
                "ilink_bot_id": "bot-id",
                "ilink_user_id": "scanner",
                "baseurl": "https://api.example.test",
            },
        ]
        shown: list[str] = []
        state = login(display=shown.append)
        self.assertEqual(shown, ["qr-content-1"])
        self.assertEqual(state.bot_token, "token")
        self.assertEqual(state.base_url, "https://api.example.test")

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_redirect_and_verify_code(self) -> None:
        FakeClient.statuses = [
            {"status": "scaned_but_redirect", "redirect_host": "redirect.example.test"},
            {"status": "need_verifycode"},
            {"status": "confirmed", "bot_token": "token", "ilink_bot_id": "bot", "baseurl": "https://final"},
        ]
        state = login(display=lambda _value: None, read_verify_code=lambda _prompt: "123456")
        self.assertEqual(FakeClient.instances[0].base_url, "https://redirect.example.test")
        self.assertEqual(state.base_url, "https://final")

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_expired_login_fails(self) -> None:
        FakeClient.statuses = [
            {"status": "expired"},
            {"status": "expired"},
            {"status": "expired"},
        ]
        with self.assertRaises(ILinkError):
            login(display=lambda _value: None)

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_expired_qrcode_refreshes_before_confirmed_login(self) -> None:
        FakeClient.statuses = [
            {"status": "expired"},
            {
                "status": "confirmed", "bot_token": "new-token",
                "ilink_bot_id": "bot", "baseurl": "https://final",
            },
        ]
        shown: list[str] = []

        state = login(display=shown.append)

        self.assertEqual(shown, ["qr-content-1", "qr-content-2"])
        self.assertEqual(state.bot_token, "new-token")

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_existing_session_is_preserved_for_binded_redirect(self) -> None:
        existing = SessionState.create(
            bot_token="old-token", bot_id="old-bot", base_url="https://old",
        )
        FakeClient.statuses = [{"status": "binded_redirect"}]

        state = login(display=lambda _value: None, existing_state=existing)

        self.assertEqual(state, existing)
        self.assertEqual(FakeClient.local_tokens, [["old-token"]])

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_binded_redirect_without_local_session_fails_immediately(self) -> None:
        FakeClient.statuses = [{"status": "binded_redirect"}]

        with self.assertRaisesRegex(ILinkError, "没有可复用"):
            login(display=lambda _value: None)

    @patch("wxbot.auth.qr_login.ILinkClient", FakeClient)
    def test_redirect_rejects_non_https_scheme(self) -> None:
        FakeClient.statuses = [
            {"status": "scaned_but_redirect", "redirect_host": "http://unsafe.example"},
        ]

        with self.assertRaisesRegex(ILinkError, "不是 HTTPS"):
            login(display=lambda _value: None)


if __name__ == "__main__":
    unittest.main()
