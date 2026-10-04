from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Callable

from wxbot.ai.codex_reply import CodexReplyError, GeneratedReply
from wxbot.message.auto_reply import AutoReplyStore
from wxbot.project.commands import SafeCommandRunner
from wxbot.project.workspace import TaskWorkspace
from wxbot.storage.process_lock import ProcessLock, ProcessLockError


Runner = Callable[..., subprocess.CompletedProcess[str]]
ChangeExecutor = Callable[[str, str, Path, threading.Event | None], str]
ApprovalListener = Callable[[str, str], None]
DENIED_PARTS = {
    ".git", ".venv", "venv", "data", "tmp", "node_modules", "__pycache__",
    ".codex", ".github", ".idea", ".vscode",
}
DENIED_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "session.json"}
DENIED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
CHANGE_WORDS = ("修改", "修复", "新增", "添加", "实现", "重构", "改一下", "写入", "update", "fix", "add")
HIGH_RISK_WORDS = (
    "删除文件", "删除目录", "重命名", "数据库迁移", "全局安装", "系统配置",
    "推送", "push", "部署", "发布",
)
SENSITIVE_WORDS = (".env", "密钥", "token", "凭证", "secret", "credential")


class ProjectControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class Approval:
    code: str
    project: str
    owner_hash: str
    fingerprint: str
    patch: str
    status: str = "pending"
    validation_command: list[str] | None = None


class ProjectController:
    def __init__(
        self,
        *,
        projects_root: Path,
        data_dir: Path,
        auto_reply_store: AutoReplyStore,
        executable: str,
        runner: Runner = subprocess.run,
    ) -> None:
        self.projects_root = projects_root.resolve()
        self.registry_path = data_dir / "projects.json"
        self._registry_lock = threading.RLock()
        self.approvals_dir = data_dir / "approvals"
        self.temp_root = data_dir.parent.resolve() / "tmp"
        self.auto_reply_store = auto_reply_store
        self.executable = executable
        self.runner = runner
        self.command_runner = SafeCommandRunner(runner)
        self.task_workspace: TaskWorkspace | None = None
        self.change_executor: ChangeExecutor | None = None
        self.approval_listener: ApprovalListener | None = None

    def configure_tasks(self, *, workspace: TaskWorkspace, executor: ChangeExecutor) -> None:
        self.task_workspace = workspace
        self.change_executor = executor

    def configure_approval_listener(self, listener: ApprovalListener) -> None:
        self.approval_listener = listener

    def handle_control(self, message: str, *, allow_change: bool = True) -> GeneratedReply | None:
        command = message.strip()
        if re.search(r"有哪些项目|列出(?:所有)?项目|项目列表", command) or re.fullmatch(
            r"(?:列一下|列出)\s*(?:当前|所有|全部)(?:的)?项目[。！？!?]?", command,
        ):
            projects = self.list_projects()
            current = self.current_project()
            lines = [f"{'*' if name == current else '-'} {name}" for name in projects]
            return GeneratedReply(True, "可用项目：\n" + ("\n".join(lines) or "没有发现 Git 项目"))
        switch = re.fullmatch(
            r"(?:切换到|使用|进入)\s*([A-Za-z0-9_.-]+)(?:\s*项目)?[。！？!?]?",
            command,
            re.I,
        )
        if switch:
            name = switch.group(1)
            self.select_project(name)
            return GeneratedReply(True, f"已切换到项目：{name}")
        approval_code = (
            re.search(r"(?<![A-Fa-f0-9])([A-Fa-f0-9]{6})(?![A-Fa-f0-9])", command)
            if allow_change else None
        )
        if approval_code and "查看原始补丁" in command:
            return GeneratedReply(True, self.show_raw_diff(approval_code.group(1)))
        if approval_code and "查看删除内容" in command:
            return GeneratedReply(True, self.show_deleted_content(approval_code.group(1)))
        file_detail = re.search(r"查看\s+([^\s，。]+)\s*的修改", command)
        if approval_code and file_detail:
            return GeneratedReply(
                True, self.show_file_change(approval_code.group(1), file_detail.group(1))
            )
        if approval_code and re.search(r"查看(?:修改|补丁|审批|任务)|看看(?:修改|补丁)|任务状态", command):
            return GeneratedReply(True, self.show_diff(approval_code.group(1)))
        if approval_code and re.search(
            r"批准(?:修改|补丁|审批)?|同意(?:修改|补丁)?|应用(?:修改|补丁)|确认执行|执行任务",
            command,
        ):
            return GeneratedReply(True, self.approve(approval_code.group(1)))
        if approval_code and re.search(r"拒绝(?:修改|补丁|审批)?|取消(?:修改|补丁|任务)?", command):
            return GeneratedReply(True, self.reject(approval_code.group(1)))
        state = self.auto_reply_store.load()
        current = self.current_project()
        if state.conversation_mode == "project" and current is not None:
            pending = self.pending_approval(current) if allow_change else None
            if pending is not None:
                if re.fullmatch(
                    r"(?:批准|同意|应用|确认执行)(?:刚才|当前|这个)?(?:的)?(?:修改|任务|补丁)?[。！？!?]?",
                    command,
                ):
                    return GeneratedReply(True, self.approve(pending.code))
                if re.fullmatch(
                    r"(?:取消|拒绝)(?:刚才|当前|这个)?(?:的)?(?:修改|任务|补丁)?[。！？!?]?",
                    command,
                ):
                    return GeneratedReply(True, self.reject(pending.code))
                if "查看原始补丁" in command:
                    return GeneratedReply(True, self.show_raw_diff(pending.code))
                if "查看删除内容" in command:
                    return GeneratedReply(True, self.show_deleted_content(pending.code))
                if file_detail:
                    return GeneratedReply(
                        True, self.show_file_change(pending.code, file_detail.group(1))
                    )
                if re.search(
                    r"(?:查看|看看)(?:刚才|当前|这个)?(?:的)?(?:修改|任务|补丁)"
                    r"|(?:刚才|当前|这个)(?:的)?任务.*(?:怎么改|改了什么|修改内容)"
                    r"|(?:刚才|当前|这个)(?:的)?修改.*(?:是什么|怎么样)",
                    command,
                ):
                    return GeneratedReply(True, self.show_diff(pending.code))
            if re.fullmatch(r"(?:查看)?(?:当前)?任务(?:状态)?[。！？!?]?", command):
                if pending is None:
                    return GeneratedReply(True, f"{current} 当前没有待审批写任务。")
                return GeneratedReply(True, self.show_diff(pending.code))
            safe_command = self.command_runner.select(self.project_path(current), command)
            if safe_command is not None:
                return GeneratedReply(True, self.run_safe_command(current, safe_command))
        return None

    def active_project(self) -> str | None:
        state = self.auto_reply_store.load()
        return self.current_project() if state.conversation_mode == "project" else None

    def list_projects(self) -> list[str]:
        registered = self._registered_projects()
        names = {
            path.name
            for path in (self.projects_root.iterdir() if self.projects_root.exists() else ())
            if path.is_dir() and not path.name.startswith(".") and (path / ".git").exists()
        }
        names.update(name for name, path in registered.items() if Path(path).is_dir())
        return sorted(names)

    def _registered_projects(self) -> dict[str, str]:
        if not self.registry_path.exists():
            return {}
        try:
            records = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(records, dict) or any(
                not isinstance(name, str) or not self._valid_project_name(name)
                or not isinstance(path, str) or not Path(path).is_absolute()
                for name, path in records.items()
            ):
                raise ValueError("invalid registry")
            return records
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProjectControlError("项目登记表无法读取或格式无效，原文件未修改") from exc

    @staticmethod
    def _valid_project_name(name: str) -> bool:
        return bool(name and len(name) <= 128 and name not in {".", ".."}
                    and re.search(r'[\\/:*?"<>|\x00-\x1f]', name) is None)

    def add_project(self, directory: str) -> str:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            raise ProjectControlError("请提供项目目录的绝对路径；含空格路径请加引号")
        try:
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise ProjectControlError("项目路径必须是已存在的目录")
            # Verify access without reading any file contents.
            next(path.iterdir(), None)
        except OSError as exc:
            raise ProjectControlError("项目目录不存在或不可访问") from exc
        name = path.name
        if not self._valid_project_name(name):
            raise ProjectControlError("项目目录名不能作为项目名，请选择具体项目目录")
        try:
            with self._registry_lock, ProcessLock(self.registry_path.with_suffix(".lock")):
                registered = self._registered_projects()
                for existing, value in registered.items():
                    if Path(value).resolve() == path:
                        return existing
                for existing in self.list_projects():
                    if self.project_path(existing) == path:
                        return existing
                if any(existing.casefold() == name.casefold()
                       for existing in set(registered) | set(self.list_projects())):
                    raise ProjectControlError("已有同名项目指向其他目录，未覆盖登记；请使用不同目录名")
                registered[name] = str(path)
                temporary = self.registry_path.with_suffix(".json.tmp")
                with temporary.open("w", encoding="utf-8", newline="\n") as file:
                    json.dump(registered, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.registry_path)
        except (OSError, ProcessLockError) as exc:
            raise ProjectControlError("项目登记暂时无法保存，请稍后重试") from exc
        return name

    def current_project(self) -> str | None:
        selected = self.auto_reply_store.load().current_project
        if selected in self.list_projects():
            return selected
        return "wxbot" if "wxbot" in self.list_projects() else None

    def select_project(self, name: str) -> None:
        if name not in self.list_projects():
            raise ProjectControlError("项目不存在或不在允许范围内")
        self.auto_reply_store.set_current_project(name)

    def propose_change(
        self, project: str, message: str, *, task_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        project_path = self.project_path(project)
        if self.pending_approval(project) is not None:
            raise ProjectControlError("当前项目已有待审批写任务，请先批准或拒绝")
        code = task_id or os.urandom(3).hex().upper()
        if self.task_workspace is not None and self.change_executor is not None:
            try:
                workspace = self.task_workspace.create(project, code)
                self.change_executor(project, message, workspace, cancel_event)
                patch = self.task_workspace.diff(workspace)
            except (OSError, ValueError, RuntimeError) as exc:
                raise ProjectControlError(f"隔离修改任务失败：{exc}") from exc
        else:
            with self.snapshot(project, message) as snapshot:
                prompt = (
                    "分析临时项目快照并为用户要求生成 unified diff。只输出以 diff --git 开头的文本，"
                    "不要 Markdown 围栏、解释或命令。不得删除、重命名文件，不得修改敏感配置、CI/CD、"
                    "数据库迁移或快照外内容。用户要求：" + message
                )
                raw = self.run_codex(snapshot, prompt, max_chars=100_000)
            patch = self.extract_patch(raw)
        self.validate_patch(patch)
        approval = Approval(
            code=code,
            project=project,
            owner_hash=self.owner_hash(),
            fingerprint=self.fingerprint(project_path),
            patch=patch,
            validation_command=self.command_runner.validation_command(project_path),
        )
        self.save_approval(approval)
        return self.show_diff(approval.code)

    def show_diff(self, code: str) -> str:
        approval = self.load_approval(code)
        changes = self.patch_changes(approval.patch)
        status = {"pending": "待确认", "applied": "已执行", "rejected": "已取消"}.get(
            approval.status, approval.status
        )
        added = sum(len(change["added"]) for change in changes)
        deleted = sum(len(change["deleted"]) for change in changes)
        lines = [
            f"{status}修改｜{approval.code}",
            f"项目：{approval.project}",
            f"涉及：{len(changes)} 个文件（新增 {added} 行，删除 {deleted} 行）",
            "",
        ]
        for index, change in enumerate(changes[:8], 1):
            lines.append(
                f"{index}. {change['file']}：新增 {len(change['added'])} 行，"
                f"删除 {len(change['deleted'])} 行"
            )
        if len(changes) > 8:
            lines.append(f"其余 {len(changes) - 8} 个文件未展开。")
        if len(changes) == 1 and added + deleted <= 8:
            lines.extend(["", self.format_change_content(changes[0])])
        if approval.status == "pending":
            lines.extend([
                "",
                "回复“批准刚才的修改”执行。",
                "回复“取消刚才的修改”取消。",
                f"内部任务编号：{approval.code}",
            ])
        return "\n".join(lines)

    def show_raw_diff(self, code: str) -> str:
        approval = self.load_approval(code)
        excerpt = approval.patch[:1800]
        suffix = "\n…原始补丁已截断" if len(approval.patch) > len(excerpt) else ""
        return f"原始补丁｜{approval.code}\n```diff\n{excerpt}{suffix}\n```"

    def show_file_change(self, code: str, filename: str) -> str:
        approval = self.load_approval(code)
        changes = self.patch_changes(approval.patch)
        normalized = filename.replace("\\", "/")
        matches = [change for change in changes if change["file"] == normalized]
        if not matches:
            matches = [change for change in changes if change["file"].endswith("/" + normalized)]
        if len(matches) != 1:
            raise ProjectControlError("没有找到唯一匹配的修改文件")
        return f"文件修改｜{matches[0]['file']}\n\n{self.format_file_change(matches[0])}"

    def show_deleted_content(self, code: str) -> str:
        approval = self.load_approval(code)
        changes = self.patch_changes(approval.patch)
        sections = []
        for change in changes:
            if change["deleted"]:
                content = "\n".join(f"- {line}" for line in change["deleted"][:12])
                suffix = "\n…其余删除内容未展开" if len(change["deleted"]) > 12 else ""
                sections.append(f"{change['file']}：\n{content}{suffix}")
        return "删除内容：\n" + ("\n\n".join(sections) if sections else "没有删除内容。")

    def patch_changes(self, patch: str) -> list[dict[str, object]]:
        self.validate_patch(patch)
        matches = list(re.finditer(r"^diff --git a/(.+?) b/(.+?)$", patch, re.M))
        changes: list[dict[str, object]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(patch)
            section = patch[match.end():end]
            added = [
                line[1:] for line in section.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ]
            deleted = [
                line[1:] for line in section.splitlines()
                if line.startswith("-") and not line.startswith("---")
            ]
            changes.append({"file": match.group(2), "added": added, "deleted": deleted})
        return changes

    @staticmethod
    def format_change_content(change: dict[str, object]) -> str:
        added = change["added"]
        deleted = change["deleted"]
        sections = []
        if deleted:
            sections.append("删除内容：\n" + "\n".join(f"- {line}" for line in deleted[:8]))
        if added:
            sections.append("新增内容：\n" + "\n".join(f"+ {line}" for line in added[:8]))
        return "\n\n".join(sections) or "未检测到文本行变化。"

    @staticmethod
    def format_file_change(change: dict[str, object]) -> str:
        added = [str(line) for line in change["added"]]
        deleted = [str(line) for line in change["deleted"]]

        def visible_text(lines: list[str]) -> str | None:
            joined = " ".join(line.strip() for line in lines if line.strip())
            matches = re.findall(r"f?[\"']([^\"']*[\u4e00-\u9fff][^\"']*)[\"']", joined)
            return matches[0].strip() if len(matches) == 1 else None

        def readable(lines: list[str]) -> str:
            content = [line.strip() for line in lines if line.strip()][:8]
            suffix = "\n……其余内容未展开" if len(lines) > 8 else ""
            return "\n".join(content) + suffix

        before = visible_text(deleted) or readable(deleted)
        after = visible_text(added) or readable(added)
        sections = []
        if deleted:
            sections.append(f"改前（{len(deleted)} 行）：\n{before}")
        if added:
            sections.append(f"改后（{len(added)} 行）：\n{after}")
        if deleted and added:
            sections.append(f"变化：替换 {len(deleted)} 行，新增 {len(added)} 行。")
        elif added:
            sections.append(f"变化：新增 {len(added)} 行。")
        elif deleted:
            sections.append(f"变化：删除 {len(deleted)} 行。")
        return "\n\n".join(sections) or "未检测到文本行变化。"

    def approve(self, code: str) -> str:
        approval = self.load_approval(code)
        if approval.status != "pending":
            raise ProjectControlError("该审批已处理")
        if approval.owner_hash != self.owner_hash():
            raise ProjectControlError("审批用户不匹配")
        path = self.project_path(approval.project)
        if self.fingerprint(path) != approval.fingerprint:
            raise ProjectControlError("项目在生成补丁后已变化，请重新提出修改请求")
        files = self.validate_patch(approval.patch)
        patch_bytes = approval.patch.encode("utf-8")
        check = self.runner(
            ["git", "apply", "--check", "-"], cwd=path, input=patch_bytes,
            capture_output=True, check=False,
        )
        if check.returncode != 0:
            raise ProjectControlError("补丁校验失败，未修改项目")
        applied = self.runner(
            ["git", "apply", "-"], cwd=path, input=patch_bytes,
            capture_output=True, check=False,
        )
        if applied.returncode != 0:
            raise ProjectControlError("补丁应用失败")
        self.save_approval(Approval(**{**asdict(approval), "status": "applied"}))
        if self.approval_listener is not None:
            self.approval_listener(approval.code, "completed")
        validation = "未运行自动验证"
        if approval.validation_command:
            result = self.command_runner.run(path, approval.validation_command)
            validation = self.validation_summary(result)
        status = self.command_runner.run(path, ["git", "status", "--short", "--branch"])
        changed_count = len([
            line for line in status.output.splitlines()
            if line.strip() and not line.startswith("##")
        ])
        return (
            f"修改完成｜{approval.code}\n"
            f"文件：{', '.join(files)}\n"
            f"验证：{validation}\n"
            "Git：本次修改未提交、未推送\n"
            f"项目工作区：{changed_count} 个未提交或未跟踪文件"
        )

    @staticmethod
    def validation_summary(result: object) -> str:
        output = getattr(result, "output", "")
        returncode = getattr(result, "returncode", 1)
        elapsed = getattr(result, "elapsed_seconds", 0.0)
        match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s", output)
        if match and returncode == 0:
            return f"{match.group(1)} 项测试全部通过，耗时 {match.group(2)} 秒"
        if returncode == 0:
            return f"通过，耗时 {elapsed:.1f} 秒"
        return f"失败，退出码 {returncode}，耗时 {elapsed:.1f} 秒"

    def reject(self, code: str) -> str:
        approval = self.load_approval(code)
        if approval.status != "pending":
            raise ProjectControlError("该审批已处理")
        self.save_approval(Approval(**{**asdict(approval), "status": "rejected"}))
        if self.approval_listener is not None:
            self.approval_listener(approval.code, "cancelled")
        return f"已拒绝修改 {code}，真实项目未变化。"

    def run_codex(self, cwd: Path, prompt: str, max_chars: int) -> str:
        output = cwd / ".wxbot-output.txt"
        result = self.runner(
            [self.executable, "exec", "--ephemeral", "--sandbox", "read-only", "--ignore-user-config",
             "--skip-git-repo-check", "-C", str(cwd), "-o", str(output), "-"],
            cwd=cwd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
        )
        if result.returncode != 0 or not output.exists():
            raise CodexReplyError("项目查询失败")
        text = output.read_text(encoding="utf-8").strip()
        if not text or len(text) > max_chars:
            raise CodexReplyError("项目查询结果为空或过长")
        return text

    def project_path(self, name: str) -> Path:
        registered = self._registered_projects()
        if name in registered:
            path = Path(registered[name]).resolve()
            if not path.is_dir():
                raise ProjectControlError("已登记的项目目录不存在或不可访问")
            return path
        if name not in self.list_projects():
            raise ProjectControlError("项目不存在或不在允许范围内")
        path = (self.projects_root / name).resolve()
        if path.parent != self.projects_root:
            raise ProjectControlError("项目路径越界")
        return path


    @contextmanager
    def snapshot(self, project: str, query: str = "") -> Iterator[Path]:
        source = self.project_path(project)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wxbot-project-", dir=self.temp_root) as directory:
            target = Path(directory)
            for path in source.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(source)
                if self.denied_path(relative) or path.stat().st_size > 256_000:
                    continue
                if not self.is_text(path):
                    continue
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            status, log = self.git_summary(source)
            (target / "_PROJECT_STATUS.md").write_text(
                f"# Git status\n{status}\n# Recent commits\n{log}", encoding="utf-8"
            )
            yield target

    def git_summary(self, source: Path) -> tuple[str, str]:
        status = self.runner(
            ["git", "status", "--short", "--branch"], cwd=source,
            capture_output=True, text=True, encoding="utf-8", check=False,
        ).stdout
        safe_status_lines = []
        for line in status.splitlines():
            candidate = line[3:].strip().split(" -> ")[-1] if len(line) > 3 else ""
            if not candidate or not self.denied_path(Path(candidate)):
                safe_status_lines.append(line)
        log = self.runner(
            ["git", "log", "-5", "--oneline"], cwd=source,
            capture_output=True, text=True, encoding="utf-8", check=False,
        ).stdout
        return "\n".join(safe_status_lines), log

    @staticmethod
    def is_text(path: Path) -> bool:
        try:
            return b"\0" not in path.read_bytes()[:4096]
        except OSError:
            return False

    @staticmethod
    def is_high_risk_request(command: str) -> bool:
        lowered = command.lower()
        if any(word in lowered for word in HIGH_RISK_WORDS):
            return True
        return (
            any(word in lowered for word in SENSITIVE_WORDS)
            and any(word in lowered for word in CHANGE_WORDS)
        )

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

    def fingerprint(self, project: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(project.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(project)
            if self.denied_path(relative) or path.stat().st_size > 256_000:
                continue
            digest.update(relative.as_posix().encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def extract_patch(text: str) -> str:
        if "```" in text:
            match = re.search(r"```(?:diff)?\s*(diff --git .*?)```", text, re.S)
            if match:
                text = match.group(1)
        start = text.find("diff --git ")
        if start < 0:
            raise ProjectControlError("模型没有生成可审批的补丁")
        return text[start:].strip() + "\n"

    def validate_patch(self, patch: str) -> list[str]:
        if any(marker in patch for marker in ("deleted file mode", "rename from", "rename to", "GIT binary patch")):
            raise ProjectControlError("补丁包含删除、重命名或二进制修改，已拒绝")
        files = re.findall(r"^diff --git a/(.+?) b/(.+?)$", patch, re.M)
        if not files:
            raise ProjectControlError("补丁格式无效")
        result: list[str] = []
        for old, new in files:
            if old != new:
                raise ProjectControlError("补丁包含文件重命名，已拒绝")
            path = Path(new)
            if path.is_absolute() or ".." in path.parts or self.denied_path(path):
                raise ProjectControlError("补丁包含越界或敏感路径，已拒绝")
            result.append(path.as_posix())
        return result

    def owner_hash(self) -> str:
        owner = self.auto_reply_store.load().allowed_user_id or ""
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def approval_path(self, code: str) -> Path:
        if not re.fullmatch(r"[A-F0-9]{6}", code.upper()):
            raise ProjectControlError("审批码格式错误")
        return self.approvals_dir / f"{code.upper()}.json"

    def save_approval(self, approval: Approval) -> None:
        path = self.approval_path(approval.code)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(approval), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def load_approval(self, code: str) -> Approval:
        try:
            return Approval(**json.loads(self.approval_path(code).read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise ProjectControlError("审批码不存在") from exc

    def pending_approval(self, project: str) -> Approval | None:
        if not self.approvals_dir.exists():
            return None
        for path in self.approvals_dir.glob("*.json"):
            try:
                approval = Approval(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, json.JSONDecodeError):
                continue
            if approval.project == project and approval.status == "pending":
                return approval
        return None

    def run_safe_command(self, project: str, command: list[str]) -> str:
        result = self.command_runner.run(self.project_path(project), command)
        elapsed = f"{result.elapsed_seconds:.1f}"
        if command[:2] == ["git", "status"] or command[:2] == ["git", "diff"] or command[:2] == ["git", "log"]:
            return result.output or "没有可显示的 Git结果。"
        if "unittest" in command:
            match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s", result.output)
            if result.returncode == 0:
                detail = f"{match.group(1)} 项全部成功" if match else "全部成功"
                duration = match.group(2) if match else elapsed
                return f"{project} 测试通过：{detail}，耗时 {duration} 秒。"
        if result.returncode == 0:
            action = "检查" if any("lint" in part for part in command) else "构建"
            return f"{project} {action}通过，耗时 {elapsed} 秒。"
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        summary = lines[-1][:300] if lines else "没有错误详情"
        return f"{project} 执行失败，退出码 {result.returncode}，耗时 {elapsed} 秒。\n错误摘要：{summary}"
