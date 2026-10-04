from __future__ import annotations

import re
import secrets
import threading
from dataclasses import replace
from pathlib import Path

from wxbot.ai.app_server import AppServerError
from wxbot.ai.codex_reply import GeneratedReply
from wxbot.ai.session_manager import SessionManager
from wxbot.api.models import WeixinMessage
from wxbot.message.media import (
    InboundTextFile,
    PermanentMediaError,
    prepare_outbound_audio_file,
    prepare_outbound_text_file,
)
from wxbot.message.commands import SlashCommand, command_help, parse_command
from wxbot.project.checkpoints import CheckpointError, TaskCheckpointStore
from wxbot.project.control import ProjectControlError, ProjectController
from wxbot.project.status import ProjectStatusError, ProjectStatusReader
from wxbot.project.tasks import ProjectTask, TaskStateError, TaskWorker


def safe_app_server_error(error: Exception) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)\bwxid_[A-Za-z0-9_-]+\b", "[USER]", text)
    text = re.sub(r"(?i)\b[^\s@]+@im\.wechat\b", "[USER]", text)
    text = re.sub(
        r"(?i)\b(token|secret|password|authorization|context_token)\s*[:=]\s*[^\s,;，；]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "…",
        text,
    )
    text = re.sub(r"[A-Za-z]:[\\/][^\s，。；;]+", "<path>", text)
    text = re.sub(r"\b[A-Za-z0-9_\-+/=]{32,}\b", "…", text)
    return text[:240] or "未知 App Server错误"


class AppServerReplyGenerator:
    HELP_COMMANDS = {
        "帮助", "帮助指令", "指令帮助", "有哪些指令", "列出所有指令",
    }
    COMPACT_CONTEXT_PHRASES = {
        "压缩上下文",
        "压缩对话上下文",
        "压缩当前上下文",
        "整理上下文",
    }

    def __init__(
        self, *, sessions: SessionManager, projects: ProjectController,
        task_sender=None, checkpoints: TaskCheckpointStore | None = None,
    ) -> None:
        self.sessions = sessions
        self.projects = projects
        self.task_sender = task_sender
        self.checkpoints = checkpoints
        self._task_lock = threading.Lock()
        self.task_worker: TaskWorker | None = None
        self._task_messages: dict[str, WeixinMessage] = {}
        self._analyzing_projects: dict[str, int] = {}
        self._restore_previews: dict[str, str] = {}

    def attach_worker(self, worker: TaskWorker) -> None:
        self.task_worker = worker

    @staticmethod
    def is_immediate_control(message: str) -> bool:
        slash = parse_command(message)
        if slash is not None:
            return bool(slash.error or slash.name not in {"project", "chat"})
        command = message.strip().rstrip("。！？!?")
        return bool(
            AppServerReplyGenerator.is_help_control(message)
            or
            AppServerReplyGenerator.is_compact_control(message)
            or
            SessionManager.is_runtime_query(message)
            or
            SessionManager.is_thread_control(message)
            or
            ProjectStatusReader.classify(message) is not None
            or
            AppServerReplyGenerator.is_status_query(message)
            or re.fullmatch(r"取消(?:刚才|当前|这个)(?:的)?任务", command)
            or re.fullmatch(
                r"确认(?:安装刚才的项目依赖|(?:创建)?刚才的本地提交)", command
            )
            or re.search(r"查看.*修改|修改了什么|撤销.*修改|确认恢复.*修改", command)
            or re.search(r"有哪些(?:正在|运行中)的任务|运行中的任务", command)
        )

    @classmethod
    def is_help_control(cls, message: str) -> bool:
        return message.strip().rstrip("。！？!?") in cls.HELP_COMMANDS

    @classmethod
    def is_compact_control(cls, message: str) -> bool:
        command = message.strip().rstrip("。！？!?")
        return "".join(command.split()) in cls.COMPACT_CONTEXT_PHRASES

    @staticmethod
    def help_text() -> str:
        return command_help()

    @staticmethod
    def is_status_query(message: str) -> bool:
        command = message.strip().rstrip("。！？!?")
        return bool(
            re.fullmatch(
                r"(?:当前|刚才|这个)(?:的)?任务(?:怎么样了|完成了吗|状态)?",
                command,
            )
            or re.fullmatch(
                r"(?:任务)?(?:还没|仍没|仍然没)(?:做)?完(?:成)?(?:吗|呢)?",
                command,
            )
            or re.fullmatch(r"(?:任务)?(?:做完|完成)(?:了)?吗", command)
            or re.fullmatch(
                r"(?:任务)?怎么(?:还|仍然)(?:没|没有)(?:做)?(?:完|好)(?:呢)?",
                command,
            )
        )

    def suppress_pending_notification(
        self, message: WeixinMessage, task: ProjectTask
    ) -> bool:
        project = self.projects.active_project()
        slash = parse_command(message.text or "")
        is_task_query = slash is not None and not slash.error and slash.name in {"task", "error"}
        return (is_task_query or self.is_status_query(message.text)) and project == task.project

    def begin_analysis(self) -> str | None:
        project = self.projects.active_project()
        if project is not None:
            with self._task_lock:
                self._analyzing_projects[project] = self._analyzing_projects.get(project, 0) + 1
        return project

    def end_analysis(self, project: str | None) -> None:
        if project is None:
            return
        with self._task_lock:
            remaining = self._analyzing_projects.get(project, 0) - 1
            if remaining > 0:
                self._analyzing_projects[project] = remaining
            else:
                self._analyzing_projects.pop(project, None)

    def generate(self, message: str, _history: list[dict[str, str]]) -> GeneratedReply:
        slash = parse_command(message)
        if slash is not None:
            return self._slash_reply(slash)
        if self.is_help_control(message):
            return GeneratedReply(True, self.help_text())
        if self.is_compact_control(message):
            try:
                self.compact_context()
            except AppServerError as exc:
                return GeneratedReply(True, f"上下文压缩失败：{safe_app_server_error(exc)}")
            return GeneratedReply(
                True,
                "当前对话上下文已压缩；对话内容、其他项目和本地状态均保留。",
            )
        if SessionManager.is_thread_control(message):
            try:
                thread_control = self.sessions.thread_control(message)
            except AppServerError as exc:
                return GeneratedReply(True, f"Thread操作失败：{safe_app_server_error(exc)}")
            if thread_control is not None:
                return GeneratedReply(True, thread_control)
        if SessionManager.is_runtime_query(message):
            return GeneratedReply(True, self.sessions.runtime_status(message))
        if ProjectStatusReader.classify(message) is not None:
            project_status = self._handle_project_status(message, self.projects.active_project())
            return GeneratedReply(True, project_status or "项目进度读取失败。")
        try:
            controlled = self.projects.handle_control(message)
        except ProjectControlError as exc:
            return GeneratedReply(True, f"项目操作失败：{exc}")
        if controlled is not None:
            return controlled
        return GeneratedReply(True, self.sessions.reply(message))

    def generate_message(
        self, message: WeixinMessage, local_image: Path | None = None,
        local_file: InboundTextFile | None = None,
    ) -> GeneratedReply:
        slash = parse_command(message.text or "")
        if slash is not None:
            return self._slash_reply(slash)
        if local_file is not None:
            return GeneratedReply(
                True,
                self.sessions.reply(
                    message.text or "",
                    attachment_name=local_file.display_name,
                    attachment_text=local_file.path.read_text(encoding="utf-8"),
                ),
            )
        if local_image is not None:
            return GeneratedReply(
                True, self.sessions.reply(message.text or "", local_image=local_image),
            )
        text = message.text or ""
        if self.is_help_control(text):
            return GeneratedReply(True, self.help_text())
        if self.is_compact_control(text):
            try:
                self.compact_context()
            except AppServerError as exc:
                return GeneratedReply(True, f"上下文压缩失败：{safe_app_server_error(exc)}")
            return GeneratedReply(
                True,
                "当前对话上下文已压缩；对话内容、其他项目和本地状态均保留。",
            )
        outbound = self._outbound_file_reply(text)
        if outbound is not None:
            return outbound
        if SessionManager.is_thread_control(text):
            try:
                thread_control = self.sessions.thread_control(message.text)
            except AppServerError as exc:
                return GeneratedReply(True, f"Thread操作失败：{safe_app_server_error(exc)}")
            if thread_control is not None:
                return GeneratedReply(True, thread_control)
        if SessionManager.is_runtime_query(text):
            return GeneratedReply(True, self.sessions.runtime_status(text))
        project = self.projects.active_project()
        task_control = self._handle_task_control(text, project)
        if task_control is not None:
            return GeneratedReply(True, task_control)
        project_status = self._handle_project_status(text, project)
        if project_status is not None:
            return GeneratedReply(True, project_status)
        try:
            controlled = self.projects.handle_control(text, allow_change=False)
        except ProjectControlError as exc:
            return GeneratedReply(True, f"项目操作失败：{exc}")
        if controlled is not None:
            return controlled
        if project is None:
            return GeneratedReply(True, self.sessions.reply(text))
        if self._is_acceptance_confirmation(text):
            self._accept_latest_pending_task(project)
        if self.task_worker is None:
            return GeneratedReply(True, "项目请求 Worker尚未启动，当前消息未处理。")
        task_id = secrets.token_hex(3).upper()
        with self._task_lock:
            self._task_messages[task_id] = message
        try:
            task = self.task_worker.enqueue(
                task_id=task_id, project=project, request=text,
                confirmation_action="",
                confirmation_owner="",
                confirmation_summary="",
            )
        except TaskStateError as exc:
            with self._task_lock:
                self._task_messages.pop(task_id, None)
            return GeneratedReply(True, str(exc))
        if task.status == "waiting_approval":
            return GeneratedReply(True, self._confirmation_notice(task))
        ahead = self._queue_ahead(task.id)
        if ahead:
            return GeneratedReply(
                True,
                f"当前项目：{project}\n\n"
                f"已加入等待队列，前面还有 {ahead} 条。",
            )
        return GeneratedReply(False, "", deferred=True)

    def _slash_reply(self, command: SlashCommand) -> GeneratedReply:
        if command.error:
            return GeneratedReply(True, command.error)
        name, args = command.name, command.args
        if name == "help":
            return GeneratedReply(True, self.help_text())
        project = self.projects.active_project()
        if name in {"progress", "next", "task", "stop", "error", "accept", "diff", "rollback", "send"} and project is None:
            return GeneratedReply(True, "请先用 /project 项目名 切换到项目。")
        try:
            if name == "projects":
                names = self.projects.list_projects()
                lines = [f"{'*' if item == project else '-'} {item}" for item in names]
                text = "可用项目：\n" + ("\n".join(lines) or "没有发现 Git项目。")
            elif name == "project":
                if args and args[0] == "add" and len(args) == 2:
                    added = self.projects.add_project(args[1])
                    text = f'项目已登记：{added}\n发送 /project "{added}" 切换；当前会话保持不变。'
                elif args:
                    self.projects.select_project(args[0])
                    text = f"已切换到项目：{args[0]}"
                else:
                    text = f"当前模式：{'项目会话' if project else '普通聊天'}\n当前项目：{project or '无'}"
            elif name == "chat":
                self.projects.auto_reply_store.set_chat_mode()
                text = "已切回普通聊天；各项目会话均保留。"
            elif name in {"progress", "next"}:
                roadmap = self.projects.project_path(project) / "ROADMAP.md"
                text = ProjectStatusReader(roadmap).read().render(name)
            elif name in {"tasks", "task", "stop", "error"}:
                requests = {
                    "tasks": "有哪些运行中的任务", "task": "刚才的任务怎么样了",
                    "stop": "取消当前任务", "error": "刚才的任务为什么失败",
                }
                latest = self.task_worker.store.latest(project) if self.task_worker is not None else None
                if name == "error" and (latest is None or latest.status != "failed"):
                    text = "当前项目最近任务没有失败记录。"
                else:
                    text = self._handle_task_control(requests[name], project) or "当前没有对应的任务记录。"
            elif name == "accept":
                text = self._accept_command(project)
            elif name in {"sessions", "resume", "status"}:
                if name == "status":
                    request = "当前会话信息"
                elif name == "resume":
                    request = f"切换到第{args[0]}个会话"
                elif not args:
                    request = "有哪些会话"
                elif args[0] == "search":
                    request = "搜索会话 " + " ".join(args[1:])
                else:
                    request = f"查看第{args[1]}个会话"
                text = self.sessions.thread_control(request) or "没有找到对应会话。"
            elif name in {"model", "usage", "context"}:
                request = {"model": "当前模型", "usage": "当前Token用量", "context": "当前上下文窗口"}[name]
                text = self.sessions.runtime_status(request)
            elif name == "compress":
                self.compact_context()
                text = "当前对话上下文已压缩；对话内容、其他项目和本地状态均保留。"
            elif name == "new":
                self.clear_context()
                text = "对话上下文已清空，我们重新开始。"
            elif name == "reset-ai-state":
                text = "全部 AI状态清理必须通过微信 /reset-ai-state 预览后确认。"
            elif name == "send":
                return self._outbound_file_reply("", filename=args[0])
            else:
                text = self._checkpoint_command(name, args, project)
        except (AppServerError, ProjectControlError, ProjectStatusError, CheckpointError, TaskStateError) as exc:
            text = "命令执行失败：" + safe_app_server_error(exc)
        return GeneratedReply(True, text)

    def _accept_command(self, project: str) -> str:
        if self.task_worker is None:
            return "当前没有待验收的任务。"
        task = self.task_worker.store.latest_pending_acceptance(project)
        if task is None:
            return "当前没有待验收的任务。"
        roadmap = self.projects.project_path(project) / "ROADMAP.md"
        checkpoint_id = secrets.token_hex(3).upper()
        if self.checkpoints is not None:
            self.checkpoints.begin(checkpoint_id, project, roadmap.parent)
        try:
            ProjectStatusReader(roadmap).record_acceptance(task.id)
            self.task_worker.store.update(task.id, acceptance_status="accepted")
        finally:
            if self.checkpoints is not None:
                self.checkpoints.finish(checkpoint_id)
        return "最近待验收任务已确认通过，验收记录已写入 ROADMAP.md；其他待验收事项保留。"

    def _checkpoint_command(self, name: str, args: tuple[str, ...], project: str) -> str:
        if self.checkpoints is None:
            return "当前未启用 checkpoint。"
        if name == "rollback" and not args:
            return self.checkpoints.list_available(project)
        if name == "rollback" and args == ("confirm",):
            with self._task_lock:
                task_id = self._restore_previews.get(project)
            if task_id is None:
                return "请先用 /rollback latest 查看恢复范围。"
            result = self.checkpoints.restore(task_id)
            with self._task_lock:
                self._restore_previews.pop(project, None)
            return result
        manifest = self.checkpoints.latest(project)
        if manifest is None:
            return "当前项目没有可查看的任务 checkpoint。"
        task_id = str(manifest["task_id"])
        if name == "rollback":
            preview = self.checkpoints.prepare_restore(task_id)
            with self._task_lock:
                self._restore_previews[project] = task_id
            return preview.replace("回复“确认恢复刚才的修改”执行。", "回复 /rollback confirm 执行本次预览的恢复。")
        if args == ("--stat",):
            return self.checkpoints.summary(task_id)
        if args:
            return self.checkpoints.file_detail(task_id, args[0])
        return self.checkpoints.raw_diff(task_id)

    def _outbound_file_reply(self, text: str, *, filename: str | None = None) -> GeneratedReply | None:
        if filename is None and not re.search(r"发给我|发我|发送给我|传给我|发过来", text):
            return None
        project = self.projects.active_project()
        if project is None:
            return GeneratedReply(True, "请先切换到文件所在项目，再说明要发送的文件。")
        root = self.projects.project_path(project)
        text_suffixes = {
            ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".jsonl",
            ".yaml", ".yml", ".toml", ".ini", ".diff", ".patch",
        }
        audio_suffixes = {".mp3", ".wav", ".ogg", ".m4a", ".silk"}
        suffixes = r"(?:txt|md|log|csv|tsv|json|jsonl|yaml|yml|toml|ini|diff|patch|mp3|wav|ogg|m4a|silk)"
        quoted = re.findall(
            rf"[\"“]([^\"”\r\n]+\.{suffixes})[\"”]", text, flags=re.I,
        )
        simple = re.findall(
            rf"(?<![A-Za-z0-9_./\\-])([A-Za-z0-9_./\\-]+\.{suffixes})",
            text,
            flags=re.I,
        )
        names = [filename] if filename is not None else list(dict.fromkeys([*quoted, *simple]))
        candidates: list[Path] = []
        if names:
            if len(names) != 1:
                return GeneratedReply(True, "一次只能发送一个文件，请明确一个文件名。")
            relative = Path(names[0])
            if relative.is_absolute():
                return GeneratedReply(True, "第一阶段只发送当前项目内的允许文件。")
            candidates = [root / relative]
        elif self.checkpoints is not None:
            latest = self.checkpoints.latest(project)
            changes = latest.get("changes", []) if latest is not None else []
            candidates = [
                root / str(change["path"])
                for change in changes
                if (
                    isinstance(change, dict)
                    and change.get("kind") in {"added", "modified"}
                    and isinstance(change.get("path"), str)
                )
            ]
            candidates = [
                path for path in candidates
                if path.suffix.lower() in text_suffixes
                and path.is_file()
            ]
        if len(candidates) != 1:
            return GeneratedReply(True, "无法唯一确定要发送的文件，请在消息中写明文件名。")
        try:
            if candidates[0].suffix.lower() in audio_suffixes:
                prepared = prepare_outbound_audio_file(candidates[0], root)
            else:
                prepared = prepare_outbound_text_file(candidates[0], root)
        except (OSError, PermanentMediaError):
            return GeneratedReply(True, "该文件不在允许范围内、格式无效、内容敏感或超过大小限制，未发送。")
        return GeneratedReply(
            False,
            "",
            outbound_file=prepared.path,
            outbound_name=prepared.display_name,
            outbound_sha256=prepared.sha256,
        )

    def _handle_project_status(self, message: str, project: str | None) -> str | None:
        intent = ProjectStatusReader.classify(message)
        if intent is None:
            return None
        if project is None:
            return "当前未切换到项目，请先说“切换到项目名”。"
        try:
            roadmap = self.projects.project_path(project) / "ROADMAP.md"
            return ProjectStatusReader(roadmap).read().render(intent)
        except (ProjectControlError, ProjectStatusError) as exc:
            return f"项目进度读取失败：{exc}"

    def task_completed(self, task: ProjectTask) -> None:
        if self.task_worker is not None:
            if self._is_acceptance_confirmation(task.request):
                self._accept_latest_pending_task(task.project, exclude_id=task.id)
        with self._task_lock:
            message = self._task_messages.pop(task.id, None)
        if message is None or self.task_sender is None:
            return
        try:
            self.task_sender(message, self.task_notice(task), task.id)
        except Exception:
            return
        if self.task_worker is not None:
            self.task_worker.store.update(task.id, notification_pending=False)

    def task_message(self, task_id: str) -> WeixinMessage | None:
        with self._task_lock:
            return self._task_messages.get(task_id)

    def pending_notifications(self) -> list[ProjectTask]:
        if self.task_worker is None:
            return []
        return self.task_worker.store.pending_notifications()

    def acknowledge_notification(self, task_id: str) -> None:
        if self.task_worker is not None:
            self.task_worker.store.update(task_id, notification_pending=False)

    @staticmethod
    def _is_acceptance_confirmation(message: str) -> bool:
        command = message.strip().rstrip("。！？!?")
        return command in {
            "已验证", "验证通过", "已验收", "验收通过", "确认验收通过",
        }

    def _accept_latest_pending_task(
        self, project: str, *, exclude_id: str | None = None,
    ) -> None:
        if self.task_worker is None:
            return
        task = self.task_worker.store.latest_pending_acceptance(
            project, exclude_id=exclude_id,
        )
        if task is not None:
            self.task_worker.store.update(task.id, acceptance_status="accepted")

    def _handle_task_control(self, message: str, project: str | None) -> str | None:
        if self.task_worker is None:
            return None
        command = message.strip().rstrip("。！？!?")
        confirmation_actions = {
            "确认安装刚才的项目依赖": "project_dependency_install",
            "确认刚才的本地提交": "local_git_commit",
            "确认创建刚才的本地提交": "local_git_commit",
        }
        action = confirmation_actions.get(command)
        if action is not None:
            latest = self.task_worker.store.latest(project)
            if latest is None or latest.status != "waiting_approval":
                return "当前没有对应的待确认任务。"
            try:
                self.task_worker.confirm(
                    latest.id, owner=self.projects.owner_hash(), action=action,
                )
            except TaskStateError as exc:
                return str(exc)
            label = (
                "项目依赖安装"
                if action == "project_dependency_install"
                else "本地 Git提交"
            )
            return f"已确认本次{label}，任务开始执行；完成后会自动通知。"
        checkpoint_reply = self._handle_checkpoint_control(command, project)
        if checkpoint_reply is not None:
            return checkpoint_reply
        if re.search(r"有哪些(?:正在|运行中)的任务|运行中的任务", command):
            active = self._active_tasks()
            with self._task_lock:
                analyzing = sorted(self._analyzing_projects)
            if not active and not analyzing:
                return "当前没有运行中的任务。"
            lines = [f"- {name}：正在处理请求" for name in analyzing]
            positions: dict[str, int] = {}
            for task in active:
                if task.status == "queued":
                    positions[task.project] = positions.get(task.project, 0) + 1
                    lines.append(
                        f"- {task.project}：等待队列第 {positions[task.project]} 位"
                    )
                else:
                    lines.append(f"- {task.project}：{self._status_label(task.status)}")
            return "运行中的任务：\n" + "\n".join(lines)
        current_status_query = re.fullmatch(
            r"当前(?:的)?任务(?:怎么样了|完成了吗|状态)?", command
        )
        status_query = self.is_status_query(command)
        failure_query = re.fullmatch(
            r"(?:(?:刚才|当前|这个)(?:的)?任务)?(?:为什么|怎么会|为何)失败"
            r"|(?:查看|看看|查询)(?:刚才|当前|这个)(?:的)?任务(?:失败)?(?:原因|错误)",
            command,
        )
        if status_query and project is not None:
            with self._task_lock:
                if self._analyzing_projects.get(project, 0):
                    return "当前项目请求正在等待处理。"
        active = self._active_tasks(project)
        if status_query and active:
            running = next((task for task in active if task.status == "running"), None)
            queued = [task for task in active if task.status == "queued"]
            current = running or queued[0]
            reply = self._task_status(current)
            if queued:
                reply += f"\n等待队列：{len(queued)} 条。"
            return reply
        latest = self.task_worker.store.latest(project)
        if latest is None:
            return None
        if status_query or failure_query:
            if current_status_query and latest.status not in {
                "queued", "running", "waiting_approval",
            } and latest.status not in {"failed", "cancelled"} and not latest.notification_pending:
                return None
            return self._task_status(latest)
        if re.fullmatch(r"取消(?:刚才|当前|这个)(?:的)?任务", command):
            cancellable = self._active_tasks(project)
            queued = [task for task in cancellable if task.status == "queued"]
            target = (
                max(queued, key=lambda task: (task.created_at, task.id))
                if queued else next(
                    (task for task in cancellable if task.status == "running"),
                    next(
                        (task for task in cancellable if task.status == "waiting_approval"),
                        None,
                    ),
                )
            )
            if target is None:
                return None
            try:
                cancelled = self.task_worker.cancel(target.id)
            except TaskStateError as exc:
                return str(exc)
            return "任务已取消。" if cancelled.status == "cancelled" else "正在取消任务，完成后会通知。"
        return None

    def _active_tasks(self, project: str | None = None) -> list[ProjectTask]:
        if self.task_worker is None:
            return []
        store = self.task_worker.store
        active = getattr(store, "active", None)
        if callable(active):
            return active(project)
        list_tasks = getattr(store, "list", None)
        if not callable(list_tasks):
            latest = store.latest(project)
            return (
                [latest]
                if latest is not None
                and latest.status in {"queued", "running", "waiting_approval"}
                else []
            )
        return sorted(
            (
                task for task in list_tasks()
                if task.status in {"queued", "running", "waiting_approval"}
                and (project is None or task.project == project)
            ),
            key=lambda task: (task.created_at, task.id),
        )

    def _queue_ahead(self, task_id: str) -> int:
        if self.task_worker is None:
            return 0
        store = self.task_worker.store
        queue_ahead = getattr(store, "queue_ahead", None)
        if callable(queue_ahead):
            return queue_ahead(task_id)
        tasks = self._active_tasks()
        target = next((task for task in tasks if task.id == task_id), None)
        if target is None or target.status != "queued":
            return 0
        return sum(
            1 for task in tasks
            if task.project == target.project
            and (
                task.status == "running"
                or (
                    task.status == "queued"
                    and (task.created_at, task.id) < (target.created_at, target.id)
                )
            )
        )

    def _handle_checkpoint_control(self, command: str, project: str | None) -> str | None:
        if self.checkpoints is None:
            return None
        manifest = self.checkpoints.latest(project)
        if re.fullmatch(r"有哪些可以恢复的修改|查看(?:可用|所有)?checkpoint", command, re.I):
            return self.checkpoints.list_available(project)
        if manifest is None:
            if re.search(r"查看.*修改|修改了什么|撤销.*修改|恢复.*修改", command):
                return "当前项目还没有可查看或恢复的任务 checkpoint。"
            return None
        task_id = str(manifest["task_id"])
        try:
            if re.fullmatch(r"确认恢复(?:刚才|当前|这个)?(?:的)?修改", command):
                return self.checkpoints.restore(task_id)
            if re.fullmatch(
                r"(?:撤销|恢复)(?:刚才|当前|这个|上次)?(?:的)?(?:任务|修改)?"
                r"|恢复到(?:刚才|上次)(?:任务|修改)?前",
                command,
            ):
                return self.checkpoints.prepare_restore(task_id)
            if re.fullmatch(r"查看(?:刚才|当前|这个)?(?:的)?原始\s*(?:diff|补丁)", command, re.I):
                return self.checkpoints.raw_diff(task_id)
            file_match = re.fullmatch(r"查看\s+(.+?)\s*的修改", command)
            if file_match:
                return self.checkpoints.file_detail(task_id, file_match.group(1).strip())
            if re.fullmatch(
                r"(?:查看|看看)(?:刚才|当前|这个)?(?:的)?修改"
                r"|(?:刚才|当前|这个)(?:的)?(?:任务)?修改了(?:什么|哪些地方|哪些内容)"
                r"|(?:刚才|当前|这个)(?:的)?任务怎么改的",
                command,
            ):
                return self.checkpoints.summary(task_id)
        except CheckpointError as exc:
            return f"checkpoint操作失败：{exc}"
        return None

    @classmethod
    def _task_status(cls, task: ProjectTask) -> str:
        if task.status == "queued":
            return f"{task.project} 任务正在等待执行。"
        if task.status == "running":
            return f"{task.project} 任务正在电脑上直接执行。"
        if task.status == "waiting_approval":
            return cls._confirmation_notice(task)
        if task.status == "completed":
            if task.acceptance_status == "accepted":
                result = f"当前项目：{task.project}\n\n任务已执行并验收完成。"
                return result + cls._notification_note(task)
            if task.acceptance_status == "pending":
                result = (
                    f"当前项目：{task.project}\n\n"
                    f"{task.result}\n验收状态：待验收。"
                )
                return result + cls._notification_note(task)
            result = task.result or f"{task.project} 修改已经执行完成。"
            return (
                f"当前项目：{task.project}\n\n{result}"
                + cls._notification_note(task)
            )
        if task.status == "failed":
            return cls._failed_task_status(task) + cls._notification_note(task)
        if task.status == "cancelled":
            result = f"{task.project} 任务已取消；取消前已经写入的真实文件不会自动回滚。"
            return result + cls._notification_note(task)
        return f"{task.project} 任务状态：{cls._status_label(task.status)}。"

    @staticmethod
    def _confirmation_notice(task: ProjectTask) -> str:
        scope = (task.confirmation_summary or task.request).strip()
        if len(scope) > 240:
            scope = scope[:237] + "..."
        if task.confirmation_action == "project_dependency_install":
            return (
                "待确认｜安装项目依赖\n\n"
                f"项目：{task.project}\n"
                "范围：仅安装当前项目所需依赖\n"
                "风险：可能修改依赖目录和锁文件，并执行依赖包安装脚本\n"
                "不包含：全局安装、其他项目修改\n\n"
                "10分钟内回复：\n"
                "确认安装刚才的项目依赖\n\n"
                "取消请回复：\n"
                "取消刚才的任务"
            )
        if task.confirmation_action == "local_git_commit":
            return (
                "待确认｜创建本地 Git 提交\n\n"
                f"项目：{task.project}\n"
                f"范围：{scope}\n"
                "风险：会把本次指定范围写入本地 Git 历史\n"
                "不包含：推送远端\n\n"
                "10分钟内回复：\n"
                "确认刚才的本地提交\n\n"
                "取消请回复：\n"
                "取消刚才的任务"
            )
        return f"{task.project} 任务正在等待确认。"

    @staticmethod
    def _failed_task_status(task: ProjectTask) -> str:
        detail = f"{task.error} {task.result}".lower()
        if "服务重启" in detail:
            reason = "wxbot 重启时任务仍在执行，已将其标记为中断"
        elif "超时" in detail:
            reason = "Codex App Server响应超时，已停止等待"
        elif any(marker in detail for marker in ("进程已退出", "通信失败", "未启动")):
            reason = "Codex App Server进程退出或通信中断"
        else:
            reason = task.error or "未记录具体原因"
        reason = reason.rstrip("。.!！")
        return (
            f"{task.project} 任务执行失败。\n原因：{reason}。\n"
            "真实文件可能已有部分修改，请先检查工作区再继续。"
        )

    @staticmethod
    def _notification_note(task: ProjectTask) -> str:
        if not task.notification_pending:
            return ""
        return "\n通知状态：完成通知尚未确认发送，本次查询正在返回任务结果。"

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "queued": "等待执行", "running": "正在执行",
            "waiting_approval": "等待确认", "completed": "已完成",
            "failed": "执行失败", "cancelled": "已取消",
        }.get(status, status)

    @classmethod
    def task_notice(cls, task: ProjectTask) -> str:
        if task.status == "waiting_approval":
            return task.result
        return cls._task_status(replace(task, notification_pending=False))

    def clear_context(self) -> None:
        self.sessions.clear_current()

    def compact_context(self) -> None:
        self.sessions.compact_current()

    def reset_all_ai_state(self) -> None:
        with self._task_lock:
            self._restore_previews.clear()
        self.sessions.clear_all()

    def close(self) -> None:
        if self.task_worker is not None:
            self.task_worker.close()
        self.sessions.close()
