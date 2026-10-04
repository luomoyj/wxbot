from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from wxbot.storage.process_lock import ProcessLock, ProcessLockError


class ThreadSessionStateError(RuntimeError):
    pass


class ThreadSessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self._thread_lock = threading.RLock()

    def load(self) -> dict[str, str]:
        with self._exclusive():
            sessions, _visible = self._load_unlocked()
            return sessions

    def get(self, key: str) -> str | None:
        return self.load().get(key)

    def is_client_visible(self, key: str) -> bool:
        with self._exclusive():
            _sessions, visible = self._load_unlocked()
            return key in visible

    def set(
        self, key: str, thread_id: str, *, client_visible: bool | None = None,
    ) -> None:
        if not key or not thread_id:
            raise ThreadSessionStateError("Thread映射字段无效")
        with self._exclusive():
            sessions, visible = self._load_unlocked()
            sessions[key] = thread_id
            if client_visible is True:
                visible.add(key)
            elif client_visible is False:
                visible.discard(key)
            self._save_unlocked(sessions, visible)

    def remove(self, key: str) -> None:
        with self._exclusive():
            sessions, visible = self._load_unlocked()
            if key not in sessions:
                return
            sessions.pop(key)
            visible.discard(key)
            self._save_unlocked(sessions, visible)

    def clear_all(self) -> None:
        with self._exclusive():
            self._save_unlocked({}, set())

    def _load_unlocked(self) -> tuple[dict[str, str], set[str]]:
        if not self.path.exists():
            return {}, set()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ThreadSessionStateError("Thread映射文件已损坏，未覆盖原文件") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ThreadSessionStateError("Thread映射格式无效，未覆盖原文件")
        raw_sessions = value.get("sessions")
        if not isinstance(raw_sessions, dict):
            raise ThreadSessionStateError("Thread映射格式无效，未覆盖原文件")
        sessions: dict[str, str] = {}
        visible: set[str] = set()
        for key, item in raw_sessions.items():
            if (
                not isinstance(key, str)
                or not isinstance(item, dict)
                or not isinstance(item.get("thread_id"), str)
                or not item["thread_id"]
                or not isinstance(item.get("updated_at"), (int, float))
                or (
                    "client_visible" in item
                    and not isinstance(item.get("client_visible"), bool)
                )
            ):
                raise ThreadSessionStateError("Thread映射记录无效，未覆盖原文件")
            sessions[key] = item["thread_id"]
            if item.get("client_visible") is True:
                visible.add(key)
        return sessions, visible

    def _save_unlocked(self, sessions: dict[str, str], visible: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        value = {
            "version": 1,
            "sessions": {
                key: {
                    "thread_id": thread_id,
                    "updated_at": now,
                    "client_visible": key in visible,
                }
                for key, thread_id in sorted(sessions.items())
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    @contextmanager
    def _exclusive(self, timeout: float = 2.0) -> Iterator[None]:
        with self._thread_lock:
            lock = ProcessLock(self.lock_path)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    lock.acquire()
                    break
                except ProcessLockError as exc:
                    if time.monotonic() >= deadline:
                        raise ThreadSessionStateError("Thread映射正被其他进程使用") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                lock.release()
