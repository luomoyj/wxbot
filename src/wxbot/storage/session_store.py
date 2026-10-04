from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path


class SessionStateError(ValueError):
    """登录会话文件损坏或字段异常；不自动覆盖原文件。"""


@dataclass(frozen=True)
class SessionState:
    bot_token: str
    bot_id: str
    base_url: str
    get_updates_buf: str
    saved_at: str
    ilink_user_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        bot_token: str,
        bot_id: str,
        base_url: str,
        ilink_user_id: str | None = None,
    ) -> "SessionState":
        return cls(
            bot_token=bot_token,
            bot_id=bot_id,
            base_url=base_url,
            get_updates_buf="",
            saved_at=datetime.now(timezone.utc).isoformat(),
            ilink_user_id=ilink_user_id,
        )

    def with_cursor(self, cursor: str) -> "SessionState":
        return replace(
            self,
            get_updates_buf=cursor,
            saved_at=datetime.now(timezone.utc).isoformat(),
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SessionState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionStateError(
                "登录会话文件无法读取；请运行 wxbot login 重新扫码，原文件未修改。"
            ) from exc
        if not isinstance(data, dict):
            raise SessionStateError(
                "登录会话文件结构无效；请运行 wxbot login 重新扫码，原文件未修改。"
            )
        known = {field.name for field in fields(SessionState)}
        kwargs = {key: value for key, value in data.items() if key in known}
        missing = sorted(
            name
            for name in ("bot_token", "bot_id", "base_url", "get_updates_buf", "saved_at")
            if not isinstance(kwargs.get(name), str)
        )
        if missing:
            names = "、".join(missing)
            raise SessionStateError(
                f"登录会话文件缺少有效字段：{names}；"
                "请运行 wxbot login 重新扫码，原文件未修改。"
            )
        return SessionState(**kwargs)

    def save(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        payload = json.dumps(asdict(state), ensure_ascii=False, indent=2)
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
