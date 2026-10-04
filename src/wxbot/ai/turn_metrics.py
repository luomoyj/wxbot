from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class TurnMetricsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(
        self, *, request_type: str, stage: str, input_length: int,
        model: str, reasoning_effort: str, result: str,
        model_seconds: float, total_seconds: float,
    ) -> None:
        entry = {
            "request_id": uuid.uuid4().hex[:12],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "request_type": request_type,
            "stage": stage,
            "input_size_bucket": self._input_size_bucket(input_length),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "result": result,
            "model_seconds": round(model_seconds, 3),
            "total_seconds": round(total_seconds, 3),
        }
        with self._lock:
            payload = self._load()
            records = payload.setdefault("records", [])
            records.append(entry)
            payload["records"] = records[-100:]
            self._write(payload)

    @staticmethod
    def _input_size_bucket(length: int) -> str:
        if length <= 100:
            return "0-100"
        if length <= 500:
            return "101-500"
        if length <= 2000:
            return "501-2000"
        return "2001+"

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return {"version": 1, "records": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "records": []}
        return payload if isinstance(payload, dict) else {"version": 1, "records": []}

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(temporary, self.path)
