from __future__ import annotations

from datetime import datetime

from wxbot.api.models import WeixinMessage


def inbound_message_notice(_message: WeixinMessage, number: int | None = None) -> str:
    return f"[{number}] 收到一条微信消息" if number is not None else "收到一条微信消息"


def timed_terminal_notice(message: str, now: datetime | None = None) -> str:
    current = now or datetime.now()
    return f"[{current:%H:%M:%S}] {message}"
