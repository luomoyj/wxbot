from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    output: str
    elapsed_seconds: float


class SafeCommandRunner:
    def __init__(self, runner: Runner = subprocess.run) -> None:
        self.runner = runner

    def select(self, project: Path, request: str) -> list[str] | None:
        lowered = request.lower()
        if "git" in lowered and any(word in request for word in ("状态", "status")):
            return ["git", "status", "--short", "--branch"]
        if "git" in lowered and (
            any(word in request for word in ("差异", "diff"))
            or (
                "修改" in request
                and any(word in request for word in ("查看", "看看", "显示", "有哪些"))
            )
        ):
            return ["git", "diff", "--stat"]
        if "git" in lowered and (
            any(word in request for word in ("记录", "日志", "log"))
            or (
                "提交" in request
                and any(word in request for word in ("查看", "看看", "显示", "最近", "有哪些"))
            )
        ):
            return ["git", "log", "-5", "--oneline"]
        if (
            any(word in request for word in ("运行测试", "跑测试", "执行测试"))
            or re.search(r"\brun\s+tests?\b", lowered)
            or lowered.strip() in {"test", "tests"}
        ):
            return self.validation_command(project)
        if any(word in lowered for word in ("lint", "代码检查")):
            return self.package_script(project, "lint")
        if any(word in lowered for word in ("构建", "build")):
            return self.package_script(project, "build")
        return None

    def validation_command(self, project: Path) -> list[str] | None:
        if (project / "tests").is_dir():
            python = project / ".venv" / "Scripts" / "python.exe"
            executable = str(python) if python.exists() else sys.executable
            return [executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        return self.package_script(project, "test")

    @staticmethod
    def package_script(project: Path, name: str) -> list[str] | None:
        try:
            package = json.loads((project / "package.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if not isinstance(scripts, dict) or not isinstance(scripts.get(name), str):
            return None
        return ["npm.cmd" if sys.platform == "win32" else "npm", "run", name]

    def run(self, project: Path, command: list[str], timeout: float = 300.0) -> CommandResult:
        started = time.monotonic()
        try:
            result = self.runner(
                command, cwd=project, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(command, 124, type(exc).__name__, time.monotonic() - started)
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        return CommandResult(command, result.returncode, output[-2400:], time.monotonic() - started)
