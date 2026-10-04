from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from wxbot.api.client import ILinkClient, ILinkError
from wxbot.api.models import WeixinMessage
from wxbot.storage.session_store import SessionState, SessionStore


def message_key(message: WeixinMessage) -> str:
    if message.message_id is not None:
        return f"message:{message.message_id}"
    item_ids = ",".join(item.msg_id or "" for item in message.items)
    raw = "\x1f".join(
        [
            message.from_user_id,
            message.client_id or "",
            str(message.create_time_ms or ""),
            item_ids,
            message.text or "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def outbound_client_id(message: WeixinMessage, action: str) -> str:
    key = message_key(message).replace(":", "-")
    return f"wxbot:{action}:{key}"


class RecentMessages:
    def __init__(self, ttl_seconds: float = 300, max_items: int = 1000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: OrderedDict[str, float] = OrderedDict()

    def add_if_new(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        while self._items:
            first_key, created = next(iter(self._items.items()))
            if current - created <= self.ttl_seconds:
                break
            self._items.pop(first_key)
        if key in self._items:
            return False
        self._items[key] = current
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)
        return True

    def discard(self, key: str) -> None:
        self._items.pop(key, None)


class MessagePoller:
    def __init__(
        self,
        *,
        client: ILinkClient,
        store: SessionStore,
        state: SessionState,
        on_message: Callable[[WeixinMessage], None],
        on_error: Callable[[Exception, int, float], None],
        on_poll_success: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.state = state
        self.on_message = on_message
        self.on_error = on_error
        self.on_poll_success = on_poll_success
        self.stop_event = threading.Event()
        self.recent = RecentMessages()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        cursor = self.state.get_updates_buf
        timeout_ms = 35_000
        failures = 0
        while not self.stop_event.is_set():
            try:
                updates = self.client.get_updates(cursor, timeout_ms)
                if self.on_poll_success is not None:
                    self.on_poll_success()
                for message in updates.messages:
                    if (
                        message.message_type != 1
                        or (not message.text and not message.images and not message.files)
                    ):
                        continue
                    key = message_key(message)
                    if self.recent.add_if_new(key):
                        try:
                            self.on_message(message)
                        except Exception:
                            self.recent.discard(key)
                            raise
                if updates.cursor != cursor:
                    cursor = updates.cursor
                    self.state = self.state.with_cursor(cursor)
                    self.store.save(self.state)
                timeout_ms = updates.timeout_ms
                failures = 0
            except ILinkError as exc:
                if self.stop_event.is_set():
                    return
                failures += 1
                if exc.code == -14:
                    self.on_error(exc, failures, 0)
                    return
                retry_in = min(2**failures, 30)
                self.on_error(exc, failures, retry_in)
                self.stop_event.wait(retry_in)
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                failures += 1
                retry_in = min(2**failures, 30)
                self.on_error(exc, failures, retry_in)
                self.stop_event.wait(retry_in)
