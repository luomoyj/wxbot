from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "wxbot.runtime"
MAX_LOG_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 3

_lock = threading.Lock()
_configured_paths: set[Path] = set()


def setup_runtime_log(data_dir: Path) -> None:
    """初始化后台运行日志；只记录脱敏事件，重复调用安全。"""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    log_dir = data_dir / "logs"
    with _lock:
        if log_dir in _configured_paths:
            return
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "wxbot.log",
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        _configured_paths.add(log_dir)


def log_event(event: str, detail: str = "") -> None:
    """记录一条脱敏运行事件；detail 必须由调用方完成脱敏。"""
    logging.getLogger(_LOGGER_NAME).info(
        f"{event} {detail}".rstrip()
    )
