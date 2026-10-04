from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from wxbot.api.client import ILinkError
from wxbot.api.models import MessageItem, Updates, WeixinMessage
from wxbot.message.poller import (
    MessagePoller,
    RecentMessages,
    message_key,
    outbound_client_id,
)
from wxbot.storage.process_lock import ProcessLock, ProcessLockError
from wxbot.storage.session_store import (
    SessionState,
    SessionStateError,
    SessionStore,
)


def sample_message(message_id: int | None = 1) -> WeixinMessage:
    return WeixinMessage(
        from_user_id="user",
        to_user_id="bot",
        context_token="ctx",
        message_type=1,
        message_id=message_id,
        client_id="client",
        create_time_ms=123,
        items=(MessageItem(type=1, msg_id="item", text="hello"),),
    )


class StorageAndPollerTests(unittest.TestCase):
    def test_session_round_trip_and_cursor_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "session.json"
            store = SessionStore(path)
            state = SessionState.create(
                bot_token="token",
                bot_id="bot",
                base_url="https://example.test",
            )
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded, state)
            assert loaded is not None
            store.save(loaded.with_cursor("next"))
            self.assertEqual(store.load().get_updates_buf, "next")  # type: ignore[union-attr]
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_session_load_rejects_invalid_json_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text("not-json{", encoding="utf-8")
            with self.assertRaises(SessionStateError) as caught:
                SessionStore(path).load()
            self.assertIn("wxbot login", str(caught.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), "not-json{")

    def test_session_load_rejects_missing_fields_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text('{"bot_id": "bot"}', encoding="utf-8")
            with self.assertRaises(SessionStateError) as caught:
                SessionStore(path).load()
            self.assertIn("缺少有效字段", str(caught.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), '{"bot_id": "bot"}')

    def test_session_load_tolerates_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(
                '{"bot_token": "t", "bot_id": "b", "base_url": "u",'
                ' "get_updates_buf": "", "saved_at": "now", "extra": 1}',
                encoding="utf-8",
            )
            loaded = SessionStore(path).load()
            assert loaded is not None
            self.assertEqual(loaded.bot_id, "b")

    def test_recent_messages_has_five_minute_window(self) -> None:
        recent = RecentMessages(ttl_seconds=300)
        self.assertTrue(recent.add_if_new("key", now=0))
        self.assertFalse(recent.add_if_new("key", now=299))
        self.assertTrue(recent.add_if_new("key", now=301))

    def test_message_key_prefers_message_id(self) -> None:
        self.assertEqual(message_key(sample_message(42)), "message:42")
        self.assertEqual(message_key(sample_message(None)), message_key(sample_message(None)))

    def test_outbound_client_id_is_stable_across_reconstructed_messages(self) -> None:
        original = sample_message(42)
        reconstructed = sample_message(42)

        self.assertEqual(
            outbound_client_id(original, "auto"),
            outbound_client_id(reconstructed, "auto"),
        )
        self.assertEqual(outbound_client_id(original, "auto"), "wxbot:auto:message-42")
        self.assertNotEqual(
            outbound_client_id(original, "auto"),
            outbound_client_id(sample_message(43), "auto"),
        )
        self.assertNotEqual(
            outbound_client_id(original, "auto"),
            outbound_client_id(original, "clear"),
        )

    def test_outbound_client_id_without_message_id_is_stable(self) -> None:
        self.assertEqual(
            outbound_client_id(sample_message(None), "auto"),
            outbound_client_id(sample_message(None), "auto"),
        )

    def test_poller_delivers_message_then_persists_cursor(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_updates(self, cursor: str, _timeout_ms: int) -> Updates:
                self.calls += 1
                if self.calls == 1:
                    return Updates(messages=(sample_message(),), cursor="next", timeout_ms=100)
                stopped.wait(1)
                return Updates(messages=(), cursor=cursor, timeout_ms=100)

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            state = SessionState.create(bot_token="token", bot_id="bot", base_url="https://example")
            received: list[WeixinMessage] = []
            stopped = threading.Event()
            client = FakeClient()
            poller: MessagePoller

            def on_message(message: WeixinMessage) -> None:
                received.append(message)
                stopped.set()
                poller.stop()

            poller = MessagePoller(
                client=client,  # type: ignore[arg-type]
                store=store,
                state=state,
                on_message=on_message,
                on_error=lambda error, _failures, _retry: self.fail(str(error)),
            )
            poller.run()
            self.assertEqual(received[0].text, "hello")
            loaded = store.load()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.get_updates_buf, "next")  # type: ignore[union-attr]

    def test_process_lock_rejects_second_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wxbot.lock"
            first = ProcessLock(path)
            second = ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaises(ProcessLockError):
                    second.acquire()
            finally:
                first.release()

    def test_process_lock_can_lock_empty_file_without_leaking_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.lock"
            path.touch()
            lock = ProcessLock(path)
            lock.acquire()
            lock.release()
            path.unlink()
            self.assertFalse(path.exists())

    def test_stopped_poller_suppresses_cancel_error(self) -> None:
        class CancelledClient:
            def get_updates(self, _cursor: str, _timeout_ms: int) -> Updates:
                poller.stop()
                raise OSError("connection closed during shutdown")

        with tempfile.TemporaryDirectory() as directory:
            errors: list[Exception] = []
            store = SessionStore(Path(directory) / "session.json")
            state = SessionState.create(bot_token="token", bot_id="bot", base_url="https://example")
            poller = MessagePoller(
                client=CancelledClient(),  # type: ignore[arg-type]
                store=store,
                state=state,
                on_message=lambda _message: None,
                on_error=lambda error, _failures, _retry: errors.append(error),
            )
            poller.run()
            self.assertEqual(errors, [])

    def test_poller_recovers_from_transient_errors_with_original_cursor(self) -> None:
        class RecoveringClient:
            def __init__(self) -> None:
                self.cursors: list[str] = []
                self.failures = [
                    ILinkError("iLink 网络请求失败"),
                    ILinkError("iLink HTTP 503"),
                    ILinkError("iLink 返回了无效 JSON"),
                ]

            def get_updates(self, cursor: str, _timeout_ms: int) -> Updates:
                self.cursors.append(cursor)
                if self.failures:
                    raise self.failures.pop(0)
                return Updates(messages=(sample_message(),), cursor="next", timeout_ms=100)

        class ImmediateWaitEvent:
            def __init__(self) -> None:
                self.stopped = False
                self.waits: list[float] = []

            def is_set(self) -> bool:
                return self.stopped

            def set(self) -> None:
                self.stopped = True

            def wait(self, timeout: float) -> bool:
                self.waits.append(timeout)
                return self.stopped

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            state = SessionState.create(bot_token="token", bot_id="bot", base_url="https://example")
            errors: list[Exception] = []
            retry_events: list[tuple[int, float]] = []
            client = RecoveringClient()
            poller: MessagePoller

            def on_message(_message: WeixinMessage) -> None:
                poller.stop()

            poller = MessagePoller(
                client=client,  # type: ignore[arg-type]
                store=store, state=state, on_message=on_message,
                on_error=lambda error, failures, retry: (
                    errors.append(error), retry_events.append((failures, retry))
                ),
            )
            event = ImmediateWaitEvent()
            poller.stop_event = event  # type: ignore[assignment]
            poller.run()

            self.assertEqual(client.cursors, ["", "", "", ""])
            self.assertEqual(event.waits, [2, 4, 8])
            self.assertEqual(retry_events, [(1, 2), (2, 4), (3, 8)])
            self.assertEqual([str(error) for error in errors], [
                "iLink 网络请求失败", "iLink HTTP 503", "iLink 返回了无效 JSON",
            ])
            self.assertEqual(store.load().get_updates_buf, "next")  # type: ignore[union-attr]

    def test_session_expired_stops_without_retry(self) -> None:
        class ExpiredClient:
            def get_updates(self, _cursor: str, _timeout_ms: int) -> Updates:
                raise ILinkError("expired", code=-14)

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            state = SessionState.create(bot_token="token", bot_id="bot", base_url="https://example")
            events: list[tuple[str, int, float]] = []
            poller = MessagePoller(
                client=ExpiredClient(),  # type: ignore[arg-type]
                store=store,
                state=state,
                on_message=lambda _message: None,
                on_error=lambda error, failures, retry: events.append(
                    (str(error), failures, retry)
                ),
            )
            poller.run()
            self.assertEqual(events, [("expired", 1, 0)])

    def test_message_handler_failure_retries_same_message_and_cursor(self) -> None:
        class RepeatingClient:
            def __init__(self) -> None:
                self.cursors: list[str] = []

            def get_updates(self, cursor: str, _timeout_ms: int) -> Updates:
                self.cursors.append(cursor)
                return Updates(messages=(sample_message(),), cursor="next", timeout_ms=100)

        class ImmediateWaitEvent:
            def __init__(self) -> None:
                self.stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def set(self) -> None:
                self.stopped = True

            def wait(self, _timeout: float) -> bool:
                return self.stopped

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            state = SessionState.create(bot_token="token", bot_id="bot", base_url="https://example")
            client = RepeatingClient()
            attempts = 0
            errors: list[Exception] = []
            poller: MessagePoller

            def on_message(_message: WeixinMessage) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("temporary handler failure")
                poller.stop()

            poller = MessagePoller(
                client=client,  # type: ignore[arg-type]
                store=store, state=state, on_message=on_message,
                on_error=lambda error, _failures, _retry: errors.append(error),
            )
            poller.stop_event = ImmediateWaitEvent()  # type: ignore[assignment]
            poller.run()

            self.assertEqual(attempts, 2)
            self.assertEqual(client.cursors, ["", ""])
            self.assertEqual(len(errors), 1)
            self.assertEqual(store.load().get_updates_buf, "next")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
