from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeneratedReply:
    should_reply: bool
    reply: str
    outbound_file: Path | None = None
    outbound_name: str = ""
    outbound_sha256: str = ""
    deferred: bool = False


class CodexReplyError(RuntimeError):
    pass
