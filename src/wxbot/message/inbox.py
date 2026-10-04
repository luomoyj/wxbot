from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from wxbot.api.models import (
    CDNMedia, FileItem, ImageItem, MessageItem, VoiceItem, WeixinMessage,
)
from wxbot.message.poller import message_key
from wxbot.storage.process_lock import ProcessLock, ProcessLockError


class InboxStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class InboxRecord:
    key: str
    status: str
    message: WeixinMessage | None
    created_at: float
    updated_at: float


class InboxStore:
    VALID_STATUSES = {"queued", "processing", "completed", "uncertain"}

    def __init__(
        self, path: Path, max_records: int = 1000,
        retention_seconds: float = 7 * 24 * 60 * 60,
    ) -> None:
        self.path = path
        self.max_records = max_records
        self.retention_seconds = retention_seconds
        self.lock_path = path.with_suffix(".lock")
        self._thread_lock = threading.RLock()

    def enqueue(self, message: WeixinMessage) -> bool:
        with self._exclusive():
            records = self._prune(self._load_unlocked())
            key = message_key(message)
            if any(record.key == key for record in records):
                return False
            now = time.time()
            records.append(InboxRecord(key, "queued", message, now, now))
            self._save_unlocked(records)
            return True

    def start(self, key: str) -> None:
        self._set_status(key, "processing")

    def complete(self, key: str) -> None:
        self._set_status(key, "completed")

    def uncertain(self, key: str) -> None:
        self._set_status(key, "uncertain")

    def recover(self) -> tuple[list[WeixinMessage], int]:
        with self._exclusive():
            records = self._load_unlocked()
            recovered: list[InboxRecord] = []
            for record in records:
                if record.status == "processing":
                    record = InboxRecord(
                        record.key, "uncertain", None, record.created_at, time.time(),
                    )
                recovered.append(record)
            self._save_unlocked(recovered)
            queued = [
                record.message for record in recovered
                if record.status == "queued" and record.message is not None
            ]
            uncertain_count = sum(record.status == "uncertain" for record in recovered)
            return queued, uncertain_count

    def records(self) -> list[InboxRecord]:
        with self._exclusive():
            return self._prune(self._load_unlocked())

    def counts(self) -> dict[str, int]:
        result = {status: 0 for status in self.VALID_STATUSES}
        for record in self.records():
            result[record.status] += 1
        return result

    def _set_status(self, key: str, status: str) -> None:
        with self._exclusive():
            records = self._load_unlocked()
            updated = False
            result: list[InboxRecord] = []
            for record in records:
                if record.key == key:
                    message = None if status in {"completed", "uncertain"} else record.message
                    record = InboxRecord(
                        record.key, status, message, record.created_at, time.time(),
                    )
                    updated = True
                result.append(record)
            if not updated:
                raise InboxStateError("入站任务不存在")
            self._save_unlocked(result)

    def _load_unlocked(self) -> list[InboxRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InboxStateError("入站任务状态文件已损坏，未覆盖原文件") from exc
        if not isinstance(data, list):
            raise InboxStateError("入站任务状态格式无效，未覆盖原文件")
        return [self._parse_record(item) for item in data]

    def _save_unlocked(self, records: list[InboxRecord]) -> None:
        records = self._prune(records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([self._record_dict(record) for record in records], ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @classmethod
    def _parse_record(cls, value: object) -> InboxRecord:
        if not isinstance(value, dict):
            raise InboxStateError("入站任务记录格式无效，未覆盖原文件")
        key = value.get("key")
        status = value.get("status")
        message = value.get("message")
        created_at = value.get("created_at", time.time())
        updated_at = value.get("updated_at", created_at)
        if (
            not isinstance(key, str)
            or status not in cls.VALID_STATUSES
            or not isinstance(created_at, (int, float))
            or not isinstance(updated_at, (int, float))
        ):
            raise InboxStateError("入站任务记录格式无效，未覆盖原文件")
        if status in {"completed", "uncertain"}:
            return InboxRecord(key, status, None, float(created_at), float(updated_at))
        if not isinstance(message, dict):
            raise InboxStateError("入站任务记录格式无效，未覆盖原文件")
        try:
            items = []
            for item in message.get("items", []):
                image = item.get("image")
                if isinstance(image, dict):
                    media = image.get("media")
                    image = ImageItem(
                        **{
                            **image,
                            "media": CDNMedia(**media) if isinstance(media, dict) else None,
                        }
                    )
                file = item.get("file")
                if isinstance(file, dict):
                    media = file.get("media")
                    file = FileItem(
                        **{
                            **file,
                            "media": CDNMedia(**media) if isinstance(media, dict) else None,
                        }
                    )
                voice = item.get("voice")
                if isinstance(voice, dict):
                    media = voice.get("media")
                    voice = VoiceItem(
                        **{
                            **voice,
                            "media": CDNMedia(**media) if isinstance(media, dict) else None,
                        }
                    )
                items.append(MessageItem(
                    **{**item, "image": image, "file": file, "voice": voice}
                ))
            items = tuple(items)
            parsed = WeixinMessage(**{**message, "items": items})
        except (TypeError, ValueError) as exc:
            raise InboxStateError("入站消息格式无效，未覆盖原文件") from exc
        return InboxRecord(key, status, parsed, float(created_at), float(updated_at))

    @staticmethod
    def _record_dict(record: InboxRecord) -> dict[str, object]:
        result: dict[str, object] = {
            "key": record.key,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        if record.message is not None:
            result["message"] = asdict(record.message)
        return result

    def _prune(self, records: list[InboxRecord]) -> list[InboxRecord]:
        cutoff = time.time() - self.retention_seconds
        terminal = [
            record for record in records
            if record.status in {"completed", "uncertain"} and record.updated_at >= cutoff
        ]
        active = [
            record for record in records
            if record.status in {"queued", "processing"}
        ]
        return terminal[-self.max_records :] + active

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
                        raise InboxStateError("入站任务状态正被其他进程使用") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                lock.release()
