from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable


Runner = Callable[..., subprocess.CompletedProcess[str]]
DENIED_PARTS = {
    ".git", ".venv", "venv", "data", "tmp", "node_modules", "__pycache__",
    ".codex", ".github", ".idea", ".vscode",
}
DENIED_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "session.json"}
DENIED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
FIXED_FILES = (
    "AGENTS.md", "README.md", "pyproject.toml", "package.json",
    "ROADMAP.md", "docs/TECHNICAL_DESIGN.md",
)


class ProjectWorkspace:
    def __init__(
        self,
        *,
        projects_root: Path,
        workspace_root: Path,
        runner: Runner = subprocess.run,
    ) -> None:
        self.projects_root = projects_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.runner = runner

    def refresh(self, project: str, query: str = "") -> Path:
        source = self._project_path(project)
        target = self.workspace_root / project
        target.mkdir(parents=True, exist_ok=True)
        selected = self._select(source)
        current: set[str] = set()
        for path in selected:
            relative = path.relative_to(source)
            current.add(relative.as_posix())
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        current.add("_PROJECT_STATUS.md")
        (target / "_PROJECT_STATUS.md").write_text(self._git_summary(source), encoding="utf-8")
        self._remove_stale(target, current)
        (target / ".wxbot-files.json").write_text(
            json.dumps(sorted(current), ensure_ascii=False), encoding="utf-8"
        )
        return target

    def _select(self, source: Path) -> list[Path]:
        fixed = {Path(name) for name in FIXED_FILES}
        candidates = [source / relative for relative in fixed if (source / relative).is_file()]
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if (
                relative in fixed
                or self.denied_path(relative)
                or path.stat().st_size > 256_000
                or not self._is_text(path)
            ):
                continue
            candidates.append(path)
        return sorted(set(candidates))

    def _remove_stale(self, target: Path, current: set[str]) -> None:
        manifest = target / ".wxbot-files.json"
        if not manifest.exists():
            return
        try:
            previous = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(previous, list):
            return
        for name in previous:
            if not isinstance(name, str) or name in current:
                continue
            path = (target / name).resolve()
            if path != target and target in path.parents and path.is_file():
                path.unlink()

    def _project_path(self, project: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
            raise ValueError("项目名称无效")
        path = (self.projects_root / project).resolve()
        if path.parent != self.projects_root or not path.is_dir() or not (path / ".git").exists():
            raise ValueError("项目不存在或不在允许范围内")
        return path

    def _git_summary(self, source: Path) -> str:
        status = self.runner(
            ["git", "status", "--short", "--branch"], cwd=source,
            capture_output=True, text=True, encoding="utf-8", check=False,
        ).stdout
        safe_status = []
        for line in status.splitlines():
            candidate = line[3:].strip().split(" -> ")[-1] if len(line) > 3 else ""
            if not candidate or not self.denied_path(Path(candidate)):
                safe_status.append(line)
        log = self.runner(
            ["git", "log", "-5", "--oneline"], cwd=source,
            capture_output=True, text=True, encoding="utf-8", check=False,
        ).stdout
        return f"# Git status\n{'\n'.join(safe_status)}\n# Recent commits\n{log}"

    @staticmethod
    def _is_text(path: Path) -> bool:
        try:
            sample = path.read_bytes()[:4096]
        except OSError:
            return False
        return b"\0" not in sample

    @staticmethod
    def denied_path(path: Path) -> bool:
        lowered = {part.lower() for part in path.parts}
        return (
            bool(lowered & DENIED_PARTS)
            or path.name.lower() in DENIED_NAMES
            or path.suffix.lower() in DENIED_SUFFIXES
            or "secret" in path.name.lower()
            or "credential" in path.name.lower()
        )


class TaskWorkspace:
    def __init__(
        self,
        *,
        projects_root: Path,
        task_root: Path,
        runner: Runner = subprocess.run,
    ) -> None:
        self.projects_root = projects_root.resolve()
        self.task_root = task_root.resolve()
        self.runner = runner
        self.source = ProjectWorkspace(
            projects_root=self.projects_root,
            workspace_root=self.task_root,
            runner=runner,
        )

    def create(self, project: str, task_id: str) -> Path:
        source = self.source._project_path(project)
        workspace = self.task_root / task_id / "workspace"
        if workspace.exists():
            raise ValueError("任务工作区已存在")
        workspace.mkdir(parents=True)
        for path in self.source._select(source):
            relative = path.relative_to(source)
            destination = workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        self._git(["init", "-q"], workspace)
        self._git(["add", "--all"], workspace)
        self._git(
            ["-c", "user.name=wxbot", "-c", "user.email=wxbot@localhost", "commit", "-q", "-m", "baseline"],
            workspace,
        )
        return workspace

    def diff(self, workspace: Path) -> str:
        self._git(["add", "--intent-to-add", "--all"], workspace)
        result = self._git(
            ["diff", "--no-ext-diff", "--binary", "--src-prefix=a/", "--dst-prefix=b/", "HEAD"],
            workspace,
        )
        patch = result.stdout
        if not patch.strip():
            raise ValueError("任务没有产生文件修改")
        return patch if patch.endswith("\n") else patch + "\n"

    def _git(self, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            ["git", *arguments], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            raise ValueError("任务工作区 Git操作失败")
        return result
