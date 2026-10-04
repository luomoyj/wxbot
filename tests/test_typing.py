from __future__ import annotations

import json
import time
import unittest

import httpx

from wxbot.api.client import ILinkClient, ILinkError
from wxbot.message.typing import TYPING_START, TYPING_STOP, TypingController


class FakeTypingClient:
    def __init__(self, *, ticket: str = "ticket-1", fail_config: bool = False) -> None:
        self.config_calls: list[tuple[str, str | None]] = []
        self.typing_calls: list[tuple[str, str, int]] = []
        self.ticket = ticket
        self.fail_config = fail_config

    def get_config(self, user_id: str, context_token: str | None = None) -> dict[str, str]:
        self.config_calls.append((user_id, context_token))
        if self.fail_config:
            raise ILinkError("iLink error 500", code=500)
        if not self.ticket:
            return {}
        return {"typing_ticket": self.ticket}

    def send_typing(self, *, to: str, typing_ticket: str, status: int) -> None:
        self.typing_calls.append((to, typing_ticket, status))


class TypingProtocolTests(unittest.TestCase):
    def test_get_config_sends_exact_protocol_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ret": 0, "typing_ticket": "ticket-abc"})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            data = client.get_config("user-a", "ctx-1")

        self.assertEqual(data["typing_ticket"], "ticket-abc")
        self.assertTrue(requests[0].url.path.endswith("/ilink/bot/getconfig"))
        body = json.loads(requests[0].content)
        self.assertEqual(body["ilink_user_id"], "user-a")
        self.assertEqual(body["context_token"], "ctx-1")
        self.assertEqual(body["base_info"], client.base_info())
        self.assertEqual(requests[0].headers["AuthorizationType"], "ilink_bot_token")
        self.assertIn("x-wechat-uin", requests[0].headers)

    def test_get_config_omits_context_token_when_missing(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ret": 0, "typing_ticket": "t"})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            client.get_config("user-a")

        body = json.loads(requests[0].content)
        self.assertNotIn("context_token", body)

    def test_send_typing_sends_exact_protocol_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ret": 0})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            client.send_typing(to="user-a", typing_ticket="ticket-abc", status=1)

        self.assertTrue(requests[0].url.path.endswith("/ilink/bot/sendtyping"))
        body = json.loads(requests[0].content)
        self.assertEqual(body["ilink_user_id"], "user-a")
        self.assertEqual(body["typing_ticket"], "ticket-abc")
        self.assertEqual(body["status"], 1)
        self.assertEqual(body["base_info"], client.base_info())

    def test_send_typing_raises_on_error_code(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ret": -2, "errmsg": "rate limited"})

        with ILinkClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ILinkError) as context:
                client.send_typing(to="user-a", typing_ticket="t", status=2)
        self.assertEqual(context.exception.code, -2)


class TypingControllerTests(unittest.TestCase):
    def test_start_fetches_ticket_once_and_sends_typing_start(self) -> None:
        client = FakeTypingClient()
        controller = TypingController(client)
        self.addCleanup(controller.close)
        controller.start("user-a", "ctx-1")

        self.assertEqual(client.config_calls, [("user-a", "ctx-1")])
        self.assertEqual(
            client.typing_calls, [("user-a", "ticket-1", TYPING_START)],
        )

        controller.start("user-a", "ctx-2")
        self.assertEqual(len(client.config_calls), 1)
        self.assertEqual(client.typing_calls, [
            ("user-a", "ticket-1", TYPING_START),
            ("user-a", "ticket-1", TYPING_START),
        ])
        self.assertEqual(client.config_calls[0][1], "ctx-1")

    def test_ticket_refetched_after_ttl_expiry(self) -> None:
        client = FakeTypingClient()
        now = time.monotonic()
        clock_values = iter([now, now + 601.0, now + 601.0])
        controller = TypingController(client, clock=lambda: next(clock_values))
        self.addCleanup(controller.close)

        controller.start("user-a", "ctx-1")
        controller.stop("user-a")
        controller.start("user-a", "ctx-1")

        self.assertEqual(len(client.config_calls), 2)

    def test_missing_ticket_is_silently_skipped(self) -> None:
        client = FakeTypingClient(ticket="")
        controller = TypingController(client)
        self.addCleanup(controller.close)

        controller.start("user-a", "ctx-1")

        self.assertEqual(client.typing_calls, [])
        self.assertEqual(controller.last_error, "TypingTicketMissing")

    def test_config_error_is_swallowed(self) -> None:
        client = FakeTypingClient(fail_config=True)
        controller = TypingController(client)
        self.addCleanup(controller.close)

        controller.start("user-a", "ctx-1")

        self.assertEqual(client.typing_calls, [])
        self.assertEqual(controller.last_error, "ILinkError")

    def test_stop_sends_typing_stop_and_clears_active(self) -> None:
        client = FakeTypingClient()
        controller = TypingController(client)
        self.addCleanup(controller.close)

        controller.start("user-a", "ctx-1")
        controller.stop("user-a")

        self.assertEqual(client.typing_calls[-1], ("user-a", "ticket-1", TYPING_STOP))
        self.assertFalse(controller.is_active("user-a"))

    def test_stop_without_start_does_not_send(self) -> None:
        client = FakeTypingClient()
        controller = TypingController(client)
        self.addCleanup(controller.close)

        controller.stop("user-a")

        self.assertEqual(client.typing_calls, [])

    def test_refresh_loop_keeps_typing_alive_until_stop(self) -> None:
        client = FakeTypingClient()
        controller = TypingController(client, refresh_interval=0.05)
        try:
            controller.start("user-a", "ctx-1")
            deadline = time.monotonic() + 2.0
            while len(client.typing_calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            starts = len(client.typing_calls)
            self.assertGreaterEqual(starts, 3)

            controller.stop("user-a")
            time.sleep(0.25)
            stable_count = len(client.typing_calls)
            time.sleep(0.25)
            self.assertEqual(len(client.typing_calls), stable_count)
            self.assertTrue(
                any(call[2] == TYPING_STOP for call in client.typing_calls),
            )
        finally:
            controller.close()

    def test_overlapping_lifecycles_stop_only_after_last_owner(self) -> None:
        client = FakeTypingClient()
        controller = TypingController(client)
        self.addCleanup(controller.close)

        controller.start("user-a", "ctx-1")
        controller.start("user-a", "ctx-1")
        controller.stop("user-a")

        self.assertTrue(controller.is_active("user-a"))
        self.assertFalse(any(call[2] == TYPING_STOP for call in client.typing_calls))

        controller.stop("user-a")
        self.assertFalse(controller.is_active("user-a"))
        self.assertEqual(client.typing_calls[-1][2], TYPING_STOP)

    def test_close_stops_refresh_thread(self) -> None:
        client = FakeTypingClient()
        controller = TypingController(client, refresh_interval=0.05)
        controller.start("user-a", "ctx-1")
        controller.close()

        self.assertFalse(controller.is_active("user-a"))


if __name__ == "__main__":
    unittest.main()
