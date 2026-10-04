from __future__ import annotations

import msvcrt
from pathlib import Path
from typing import BinaryIO


class ProcessLockError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file: BinaryIO | None = None
        try:
            file = self.path.open("a+b")
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if file is not None:
                file.close()
            raise ProcessLockError("另一个 wxbot 进程正在运行") from exc
        self._file = file

    def release(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        try:
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
