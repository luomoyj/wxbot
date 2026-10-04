from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from wxbot.api.client import ILinkClient


TYPING_START = 1
TYPING_STOP = 2
TICKET_TTL_SECONDS = 600.0
REFRESH_INTERVAL_SECONDS = 5.0


@dataclass
class _UserState:
    ticket: str
    expires_at: float
    context_token: str


class TypingController:
    """Best-effort 微信打字指示器。

    `start` 后由单个后台线程按固定周期重发 `status=1` 维持“对方正在输入…”，
    `stop` 后通过代次计数保证不再续期。所有 iLink 异常只记录到 `last_error`，
    不重试、不影响回复链路。
    """

    def __init__(
        self, client: ILinkClient, *,
        refresh_interval: float = REFRESH_INTERVAL_SECONDS,
        ticket_ttl: float = TICKET_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._refresh_interval = refresh_interval
        self._ticket_ttl = ticket_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, _UserState] = {}
        self._epochs: dict[str, int] = {}
        self._active: set[str] = set()
        self._active_counts: dict[str, int] = {}
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self, user_id: str, context_token: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._epochs[user_id] = self._epochs.get(user_id, 0) + 1
            epoch = self._epochs[user_id]
            self._active_counts[user_id] = self._active_counts.get(user_id, 0) + 1
            self._active.add(user_id)
            state = self._states.get(user_id)
            if state is not None and context_token:
                state.context_token = context_token
        self._ensure_thread()
        if state is None or state.expires_at <= self._clock():
            state = self._fetch_ticket(user_id, context_token)
            if state is not None:
                with self._lock:
                    self._states[user_id] = state
        if state is not None and self._current(user_id, epoch):
            self._send(user_id, TYPING_START, state.ticket)
        self._wake.set()

    def stop(self, user_id: str) -> None:
        with self._lock:
            count = self._active_counts.get(user_id, 0)
            if count <= 0:
                return
            if count > 1:
                self._active_counts[user_id] = count - 1
                return
            self._active_counts.pop(user_id, None)
            self._epochs[user_id] = self._epochs.get(user_id, 0) + 1
            self._active.discard(user_id)
            state = self._states.get(user_id)
        self._wake.set()
        if state is not None:
            self._send(user_id, TYPING_STOP, state.ticket)

    def is_active(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._active

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._active.clear()
            self._active_counts.clear()
            thread = self._thread
        self._wake.set()
        if thread is not None:
            thread.join(timeout=2.0)

    def _current(self, user_id: str, epoch: int) -> bool:
        with self._lock:
            return (
                not self._closed
                and user_id in self._active
                and self._epochs.get(user_id) == epoch
            )

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._run_refresh, name="wxbot-typing", daemon=True,
            )
            self._thread.start()

    def _run_refresh(self) -> None:
        while True:
            self._wake.wait(self._refresh_interval)
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
                snapshot = [(user, self._epochs.get(user, 0)) for user in self._active]
            for user_id, epoch in snapshot:
                if not self._current(user_id, epoch):
                    continue
                state = self._ensure_ticket(user_id)
                if state is None or not self._current(user_id, epoch):
                    continue
                self._send(user_id, TYPING_START, state.ticket)

    def _ensure_ticket(self, user_id: str) -> _UserState | None:
        now = self._clock()
        with self._lock:
            state = self._states.get(user_id)
        if state is not None and state.expires_at > now:
            return state
        fresh = self._fetch_ticket(user_id, state.context_token if state else "")
        if fresh is None:
            return state
        with self._lock:
            existing = self._states.get(user_id)
            if existing is not None:
                fresh.context_token = existing.context_token or fresh.context_token
            self._states[user_id] = fresh
        return fresh

    def _fetch_ticket(self, user_id: str, context_token: str) -> _UserState | None:
        try:
            data = self._client.get_config(user_id, context_token or None)
        except Exception as exc:
            self.last_error = type(exc).__name__
            return None
        ticket = str(data.get("typing_ticket") or "").strip()
        if not ticket:
            self.last_error = "TypingTicketMissing"
            return None
        return _UserState(
            ticket=ticket,
            expires_at=self._clock() + self._ticket_ttl,
            context_token=context_token,
        )

    def _send(self, user_id: str, status: int, ticket: str) -> None:
        try:
            self._client.send_typing(to=user_id, typing_ticket=ticket, status=status)
            self.last_error = None
        except Exception as exc:
            self.last_error = type(exc).__name__
