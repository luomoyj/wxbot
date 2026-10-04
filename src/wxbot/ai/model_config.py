from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ALLOWED_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


@dataclass(frozen=True)
class ModelConfig:
    model: str | None
    reasoning_effort: str | None

    @classmethod
    def load(cls, path: Path) -> "ModelConfig":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(model=None, reasoning_effort=None)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("模型配置文件无法读取或不是有效 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"model", "reasoning_effort"}:
            raise ValueError("模型配置只允许 model 和 reasoning_effort")
        model = payload.get("model")
        effort = payload.get("reasoning_effort")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model 必须是非空字符串")
        if not isinstance(effort, str) or effort not in ALLOWED_EFFORTS:
            raise ValueError("reasoning_effort 不是受支持的值")
        return cls(model=model.strip(), reasoning_effort=effort)
