from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path


SKIP_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".github", "data", "tmp", "dist", "build", ".next",
}
SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", "credentials.json", "session.json",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 50_000_000


class CheckpointError(RuntimeError):
    pass


class TaskCheckpointStore:
    def __init__(self, root: Path, *, max_count: int = 20, max_age_days: int = 30) -> None:
        self.root = root
        self.max_count = max_count
        self.max_age_seconds = max_age_days * 86400
        self._lock = threading.RLock()

    def begin(self, task_id: str, project: str, project_root: Path) -> None:
        with self._lock:
            directory = self._directory(task_id)
            if directory.exists():
                raise CheckpointError("任务 checkpoint 已存在")
            directory.mkdir(parents=True)
            files, skipped = self._capture(project_root, directory / "before")
            self._write_manifest(directory, {
                "task_id": task_id,
                "project": project,
                "project_root": str(project_root.resolve()),
                "created_at": time.time(),
                "completed_at": 0.0,
                "before": files,
                "after": {},
                "changes": [],
                "skipped": skipped,
                "restore_pending": False,
                "restored_at": 0.0,
            })

    def finish(self, task_id: str) -> None:
        with self._lock:
            directory, manifest = self._load(task_id)
            project_root = Path(str(manifest["project_root"]))
            after, skipped = self._capture(project_root, directory / "after")
            before = self._file_map(manifest.get("before"))
            changes = []
            for relative in sorted(set(before) | set(after)):
                old = before.get(relative)
                new = after.get(relative)
                if old == new:
                    continue
                kind = "modified" if old and new else "added" if new else "deleted"
                changes.append({"path": relative, "kind": kind})
            manifest.update({
                "completed_at": time.time(),
                "after": after,
                "changes": changes,
                "skipped": sorted(set(self._string_list(manifest.get("skipped"))) | set(skipped)),
                "restore_pending": False,
            })
            self._write_manifest(directory, manifest)
            self.cleanup()

    def latest(self, project: str | None = None) -> dict[str, object] | None:
        with self._lock:
            candidates = []
            if not self.root.exists():
                return None
            for path in self.root.iterdir():
                if not path.is_dir() or not (path / "manifest.json").exists():
                    continue
                try:
                    _, manifest = self._load(path.name)
                except CheckpointError:
                    continue
                if project is None or manifest.get("project") == project:
                    candidates.append(manifest)
            return max(candidates, key=lambda item: float(item.get("created_at", 0)), default=None)

    def available(self, project: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            values = []
            if not self.root.exists():
                return []
            for path in self.root.iterdir():
                if not path.is_dir() or not (path / "manifest.json").exists():
                    continue
                try:
                    _, manifest = self._load(path.name)
                except CheckpointError:
                    continue
                if (project is None or manifest.get("project") == project) and self._changes(manifest):
                    values.append(manifest)
            return sorted(values, key=lambda item: float(item.get("created_at", 0)), reverse=True)

    def list_available(self, project: str | None = None) -> str:
        values = self.available(project)
        if not values:
            return "当前没有可查看的任务 checkpoint。"
        lines = ["可查看的任务修改："]
        for item in values[:10]:
            restored = "，已恢复" if float(item.get("restored_at", 0)) else ""
            lines.append(
                f"- {item['project']}｜{item['task_id']}｜{len(self._changes(item))} 个文件{restored}"
            )
        return "\n".join(lines)

    def summary(self, task_id: str) -> str:
        directory, manifest = self._load(task_id)
        changes = self._changes(manifest)
        if not changes:
            return "刚才的任务没有检测到可记录的文件变化。"
        lines = [f"刚才的修改｜{manifest['project']}｜{task_id}", f"涉及 {len(changes)} 个文件："]
        for index, change in enumerate(changes[:10], 1):
            label = {"added": "新增", "modified": "修改", "deleted": "删除"}[str(change["kind"])]
            added, deleted = self._line_counts(directory, change)
            detail = f"，新增 {added} 行，删除 {deleted} 行" if added or deleted else ""
            lines.append(f"{index}. {change['path']}：{label}{detail}")
        if len(changes) > 10:
            lines.append(f"其余 {len(changes) - 10} 个文件未展开。")
        lines.append("可继续说“查看 文件名 的修改”或“查看原始 diff”。")
        return "\n".join(lines)

    def file_detail(self, task_id: str, filename: str) -> str:
        directory, manifest = self._load(task_id)
        normalized = filename.replace("\\", "/").lstrip("./")
        matches = [
            item for item in self._changes(manifest)
            if item["path"] == normalized or str(item["path"]).endswith("/" + normalized)
        ]
        if len(matches) != 1:
            raise CheckpointError("没有找到唯一匹配的修改文件")
        change = matches[0]
        before = self._read_text(directory / "before" / str(change["path"]))
        after = self._read_text(directory / "after" / str(change["path"]))
        if before is None or after is None:
            label = {"added": "新增文件", "deleted": "删除文件", "modified": "二进制或超限文件"}[
                str(change["kind"])
            ]
            return f"文件修改｜{change['path']}\n{label}，无法展示文本改前／改后。"
        removed, added = self._changed_lines(before, after)
        visible_removed = [line for line in removed if line.strip()]
        visible_added = [line for line in added if line.strip()]
        sections = [f"文件修改｜{change['path']}"]
        if visible_removed and visible_added:
            sections.append("改前：\n" + "\n".join(visible_removed[:12]))
            sections.append("改后：\n" + "\n".join(visible_added[:12]))
        elif visible_added:
            sections.append("新增内容：\n" + "\n".join(visible_added[:12]))
        elif visible_removed:
            sections.append("删除内容：\n" + "\n".join(visible_removed[:12]))
        elif added or removed:
            sections.append(f"仅调整空白行：新增 {len(added)} 行，删除 {len(removed)} 行。")
        if len(visible_removed) > 12 or len(visible_added) > 12:
            sections.append("……其余内容未展开，可查看原始 diff。")
        return "\n\n".join(sections)

    def raw_diff(self, task_id: str, *, max_chars: int = 3000) -> str:
        directory, manifest = self._load(task_id)
        blocks = []
        for change in self._changes(manifest):
            relative = str(change["path"])
            before = self._read_text(directory / "before" / relative)
            after = self._read_text(directory / "after" / relative)
            if before is None or after is None:
                blocks.append(f"{relative}：无法展示文本 diff")
                continue
            blocks.extend(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm="",
            ))
        text = "\n".join(blocks) or "没有可展示的文本 diff。"
        suffix = "\n……原始 diff 已截断" if len(text) > max_chars else ""
        return f"原始 diff｜{task_id}\n```diff\n{text[:max_chars]}{suffix}\n```"

    def prepare_restore(self, task_id: str) -> str:
        with self._lock:
            directory, manifest = self._load(task_id)
            changes = self._changes(manifest)
            if not changes:
                raise CheckpointError("该任务没有可恢复的文件变化")
            plan = self._restore_plan(directory, manifest)
            manifest["restore_pending"] = True
            self._write_manifest(directory, manifest)
            direct = [item for item in plan if item["action"] == "direct"]
            merged = [item for item in plan if item["action"] == "merge"]
            conflicts = [item for item in plan if item["action"] == "conflict"]
            already = [item for item in plan if item["action"] == "already"]
            lines = [f"恢复预览｜{manifest['project']}｜{task_id}"]
            lines.extend(f"- {item['path']}：可直接恢复" for item in direct)
            lines.extend(f"- {item['path']}：可撤销本任务内容并保留后续修改" for item in merged)
            lines.extend(f"- {item['path']}：存在冲突，将跳过" for item in conflicts)
            lines.extend(f"- {item['path']}：已经处于任务前状态" for item in already)
            restorable = len(direct) + len(merged)
            lines.append(f"确认后将恢复 {restorable} 个文件，跳过 {len(conflicts)} 个冲突文件。")
            lines.append("回复“确认恢复刚才的修改”执行。")
            return "\n".join(lines)

    def restore(self, task_id: str) -> str:
        with self._lock:
            directory, manifest = self._load(task_id)
            if not manifest.get("restore_pending"):
                raise CheckpointError("请先发送“撤销刚才的修改”查看恢复范围")
            plan = self._restore_plan(directory, manifest)
            project_root = Path(str(manifest["project_root"]))
            backup = directory / f"restore-backup-{int(time.time())}"
            backup.mkdir(exist_ok=True)
            restored = []
            merged = []
            conflicts = []
            for item in plan:
                relative = str(item["path"])
                action = str(item["action"])
                if action == "conflict":
                    conflicts.append(relative)
                    continue
                if action == "already":
                    continue
                target = (project_root / relative).resolve()
                if not target.is_relative_to(project_root.resolve()):
                    raise CheckpointError("checkpoint路径越界，已停止恢复")
                if target.exists() and target.is_file():
                    destination = backup / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, destination)
                if action == "merge":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(str(item["content"]).encode("utf-8"))
                    merged.append(relative)
                    continue
                before = directory / "before" / relative
                if before.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(before, target)
                elif target.exists():
                    target.unlink()
                restored.append(relative)
            manifest["restore_pending"] = False
            manifest["restored_at"] = time.time()
            manifest["restore_result"] = {
                "restored": restored, "merged": merged, "conflicts": conflicts,
            }
            self._write_manifest(directory, manifest)
            lines = ["恢复完成："]
            if restored:
                lines.append("- 已直接恢复：" + "、".join(restored))
            if merged:
                lines.append("- 已合并恢复：" + "、".join(merged))
            if conflicts:
                lines.append("- 未恢复（存在冲突）：" + "、".join(conflicts))
            if not restored and not merged:
                lines.append("- 没有文件被修改。")
            lines.append("恢复前内容已保存在本机 checkpoint。")
            return "\n".join(lines)

    def cleanup(self) -> None:
        if not self.root.exists():
            return
        entries = []
        now = time.time()
        for path in self.root.iterdir():
            if not path.is_dir() or not (path / "manifest.json").exists():
                continue
            try:
                _, manifest = self._load(path.name)
                entries.append((float(manifest.get("created_at", 0)), path))
            except CheckpointError:
                continue
        entries.sort(reverse=True)
        for index, (created_at, path) in enumerate(entries):
            if index >= self.max_count or now - created_at > self.max_age_seconds:
                shutil.rmtree(path)

    def _capture(self, project_root: Path, destination: Path) -> tuple[dict[str, str], list[str]]:
        destination.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        skipped: list[str] = []
        total = 0
        for current, directories, filenames in os.walk(project_root):
            current_path = Path(current)
            directories[:] = [
                name for name in directories
                if name.lower() not in SKIP_PARTS and not (current_path / name).is_symlink()
            ]
            for filename in filenames:
                path = current_path / filename
                if path.is_symlink():
                    continue
                relative = path.relative_to(project_root)
                if self._denied(relative):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    skipped.append(relative.as_posix())
                    continue
                if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                    skipped.append(relative.as_posix())
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(path, target)
                    digest = self._hash(path)
                except OSError:
                    skipped.append(relative.as_posix())
                    continue
                files[relative.as_posix()] = digest
                total += size
        return files, skipped

    def _restore_plan(
        self, directory: Path, manifest: dict[str, object],
    ) -> list[dict[str, object]]:
        root = Path(str(manifest["project_root"]))
        before_map = self._file_map(manifest.get("before"))
        after = self._file_map(manifest.get("after"))
        plan = []
        for change in self._changes(manifest):
            relative = str(change["path"])
            current = root / relative
            expected = after.get(relative)
            actual = self._hash(current) if current.exists() and current.is_file() else None
            before_hash = before_map.get(relative)
            if actual == before_hash:
                plan.append({"path": relative, "action": "already"})
                continue
            if actual == expected:
                plan.append({"path": relative, "action": "direct"})
                continue
            if change["kind"] != "modified" or not current.exists() or not current.is_file():
                plan.append({"path": relative, "action": "conflict"})
                continue
            before_text = self._read_text(directory / "before" / relative)
            after_text = self._read_text(directory / "after" / relative)
            current_text = self._read_text(current)
            if before_text is None or after_text is None or current_text is None:
                plan.append({"path": relative, "action": "conflict"})
                continue
            merged = self._reverse_task_changes(before_text, after_text, current_text)
            if merged is None:
                plan.append({"path": relative, "action": "conflict"})
            else:
                plan.append({"path": relative, "action": "merge", "content": merged})
        return plan

    @classmethod
    def _reverse_task_changes(cls, before: str, after: str, current: str) -> str | None:
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        current_lines = current.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
        edits: list[tuple[int, int, list[str]]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            old = after_lines[j1:j2]
            replacement = before_lines[i1:i2]
            matches = cls._sequence_matches(current_lines, old) if old else []
            if len(matches) == 1:
                start = matches[0]
                edits.append((start, start + len(old), replacement))
                continue
            prefix = after_lines[max(0, j1 - 3):j1]
            suffix = after_lines[j2:min(len(after_lines), j2 + 3)]
            pattern = prefix + old + suffix
            if not pattern:
                return None
            context_matches = cls._sequence_matches(current_lines, pattern)
            if len(context_matches) != 1:
                return None
            start = context_matches[0]
            edits.append((
                start, start + len(pattern), prefix + replacement + suffix,
            ))
        ordered = sorted(edits, key=lambda item: item[0])
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            return None
        for start, end, replacement in reversed(ordered):
            current_lines[start:end] = replacement
        return "".join(current_lines)

    @staticmethod
    def _sequence_matches(lines: list[str], pattern: list[str]) -> list[int]:
        if not pattern or len(pattern) > len(lines):
            return []
        return [
            index for index in range(len(lines) - len(pattern) + 1)
            if lines[index:index + len(pattern)] == pattern
        ]

    @staticmethod
    def _changed_lines(before: str, after: str) -> tuple[list[str], list[str]]:
        removed, added = [], []
        for line in difflib.ndiff(before.splitlines(), after.splitlines()):
            if line.startswith("- "):
                removed.append(line[2:])
            elif line.startswith("+ "):
                added.append(line[2:])
        return removed, added

    def _line_counts(self, directory: Path, change: dict[str, str]) -> tuple[int, int]:
        relative = change["path"]
        before = self._read_text(directory / "before" / relative)
        after = self._read_text(directory / "after" / relative)
        if before is None or after is None:
            return 0, 0
        removed, added = self._changed_lines(before, after)
        return len(added), len(removed)

    @staticmethod
    def _read_text(path: Path) -> str | None:
        if not path.exists():
            return ""
        try:
            raw = path.read_bytes()
            if b"\0" in raw[:4096]:
                return None
            return raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _denied(relative: Path) -> bool:
        lowered = [part.lower() for part in relative.parts]
        name = relative.name.lower()
        return (
            any(part in SKIP_PARTS for part in lowered)
            or name in SENSITIVE_NAMES
            or name.startswith(".env.")
            or relative.suffix.lower() in SENSITIVE_SUFFIXES
        )

    def _directory(self, task_id: str) -> Path:
        if not task_id or not all(char in "0123456789ABCDEF" for char in task_id.upper()):
            raise CheckpointError("任务编号无效")
        return self.root / task_id.upper()

    def _load(self, task_id: str) -> tuple[Path, dict[str, object]]:
        directory = self._directory(task_id)
        path = directory / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError("checkpoint不存在或已损坏") from exc
        if not isinstance(value, dict) or value.get("task_id") != task_id.upper():
            raise CheckpointError("checkpoint格式无效")
        return directory, value

    @staticmethod
    def _write_manifest(directory: Path, manifest: dict[str, object]) -> None:
        temporary = directory / "manifest.tmp"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, directory / "manifest.json")

    @staticmethod
    def _file_map(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items()}

    @staticmethod
    def _string_list(value: object) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def _changes(manifest: dict[str, object]) -> list[dict[str, str]]:
        value = manifest.get("changes")
        if not isinstance(value, list):
            return []
        return [
            {"path": str(item["path"]), "kind": str(item["kind"])}
            for item in value if isinstance(item, dict) and "path" in item and "kind" in item
        ]
