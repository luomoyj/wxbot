from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Protocol

from wxbot.ai.codex_reply import GeneratedReply
from wxbot.api.client import ILinkClient
from wxbot.api.models import WeixinMessage
from wxbot.message.media import InboundTextFile
from wxbot.message.commands import parse_command
from wxbot.message.poller import message_key, outbound_client_id
from wxbot.message.typing import TypingController
from wxbot.storage.process_lock import ProcessLock, ProcessLockError


class AutoReplyStateError(RuntimeError):
    pass


class ReplyGenerator(Protocol):
    def generate(self, message: str, history: list[dict[str, str]]) -> GeneratedReply: ...


CLEAR_HISTORY_PHRASES = {
    "清空上下文",
    "清空对话上下文",
    "忘掉之前的对话",
    "重新开始聊天",
    "清除聊天记忆",
}
REPLY_CHUNK_MAX_CHARS = 1800


def split_reply_text(
    text: str, limit: int = REPLY_CHUNK_MAX_CHARS,
) -> list[str]:
    content = text.strip()
    if not content or len(content) <= limit:
        return [content]
    payload_limit = max(1, limit - 32)
    raw_parts: list[str] = []
    remaining = content
    boundaries = ("\n\n", "\n", "。", "！", "？", "；", ". ")
    while len(remaining) > payload_limit:
        window = remaining[:payload_limit]
        cut = 0
        for boundary in boundaries:
            position = window.rfind(boundary)
            if position >= payload_limit // 3:
                cut = max(cut, position + len(boundary))
        if cut == 0:
            cut = payload_limit
        raw_parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        raw_parts.append(remaining)

    balanced: list[str] = []
    fence_open = False
    for index, part in enumerate(raw_parts):
        body = ("```\n" if fence_open else "") + part
        if part.count("```") % 2:
            fence_open = not fence_open
        if fence_open and index < len(raw_parts) - 1:
            body += "\n```"
        balanced.append(body)
    total = len(balanced)
    return [f"（{index}/{total}）\n{part}" for index, part in enumerate(balanced, 1)]


RESET_AI_STATE_PHRASES = {"清空全部AI状态", "重置全部AI状态"}
CONFIRM_RESET_AI_STATE_PHRASES = {"确认清空全部AI状态", "确认重置全部AI状态"}
AI_RESET_CONFIRM_TTL_SECONDS = 300
AI_RESET_PREVIEW = (
    "将清空：唯一白名单、消息去重、当前项目、会话模式、全部降级历史和 Codex Thread映射。\n"
    "将保留：微信登录、checkpoint、任务记录、模型配置、响应指标和项目文件。\n"
    "如需继续，请在5分钟内回复“确认清空全部AI状态”。"
)

HISTORY_MAX_TURNS = 20
HISTORY_CHAR_BUDGET = 6000


@dataclass
class AutoReplyState:
    allowed_user_id: str | None = None
    processed_keys: list[str] = field(default_factory=list)
    current_project: str | None = None
    conversation_mode: str = "chat"
    chat_history: list[dict[str, str]] = field(default_factory=list)
    project_histories: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    @property
    def history(self) -> list[dict[str, str]]:
        if self.conversation_mode == "project" and self.current_project:
            return self.project_histories.setdefault(self.current_project, [])
        return self.chat_history


class AutoReplyStore:
    def __init__(self, path: Path, max_processed: int = 1000) -> None:
        self.path = path
        self.max_processed = max_processed
        self.lock_path = path.with_suffix(".lock")
        self._thread_lock = threading.RLock()

    def load(self) -> AutoReplyState:
        with self._exclusive():
            return self._load_unlocked()

    def _load_unlocked(self) -> AutoReplyState:
        if not self.path.exists():
            return AutoReplyState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutoReplyStateError("自动回复状态文件已损坏；为避免覆盖原文件，已停止处理") from exc
        if not isinstance(data, dict):
            raise AutoReplyStateError("自动回复状态格式无效；为避免覆盖原文件，已停止处理")
        allowed_user_id = data.get("allowed_user_id")
        processed_keys = data.get("processed_keys", [])
        current_project = data.get("current_project")
        if allowed_user_id is not None and not isinstance(allowed_user_id, str):
            raise AutoReplyStateError("自动回复白名单状态格式无效；为避免覆盖原文件，已停止处理")
        if not isinstance(processed_keys, list) or not all(isinstance(item, str) for item in processed_keys):
            raise AutoReplyStateError("自动回复去重状态格式无效；为避免覆盖原文件，已停止处理")
        if current_project is not None and not isinstance(current_project, str):
            raise AutoReplyStateError("当前项目状态格式无效；为避免覆盖原文件，已停止处理")
        if any(key in data for key in ("conversation_mode", "chat_history", "project_histories")):
            conversation_mode = data.get("conversation_mode", "chat")
            chat_history = data.get("chat_history", [])
            project_histories = data.get("project_histories", {})
            if conversation_mode not in {"chat", "project"}:
                raise AutoReplyStateError("自动回复会话模式无效；为避免覆盖原文件，已停止处理")
            if conversation_mode == "project" and current_project is None:
                raise AutoReplyStateError("项目会话缺少当前项目；为避免覆盖原文件，已停止处理")
            if not self._valid_history(chat_history):
                raise AutoReplyStateError("普通聊天历史状态无效；为避免覆盖原文件，已停止处理")
            if (
                not isinstance(project_histories, dict)
                or not all(isinstance(name, str) and self._valid_history(turns) for name, turns in project_histories.items())
            ):
                raise AutoReplyStateError("项目聊天历史状态无效；为避免覆盖原文件，已停止处理")
        else:
            history = data.get("history", [])
            if not self._valid_history(history):
                raise AutoReplyStateError("自动回复历史状态格式无效；为避免覆盖原文件，已停止处理")
            conversation_mode = "project" if current_project else "chat"
            chat_history = [] if current_project else history
            project_histories = {current_project: history} if current_project else {}
        return AutoReplyState(
            allowed_user_id=allowed_user_id,
            processed_keys=processed_keys,
            current_project=current_project,
            conversation_mode=conversation_mode,
            chat_history=chat_history,
            project_histories=project_histories,
        )

    def save(self, state: AutoReplyState) -> None:
        with self._exclusive():
            self._save_unlocked(state)

    def _save_unlocked(self, state: AutoReplyState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def claim(self, message: WeixinMessage) -> tuple[bool, bool]:
        with self._exclusive():
            state = self._load_unlocked()
            paired = False
            if state.allowed_user_id is None:
                state.allowed_user_id = message.from_user_id
                paired = True
            if state.allowed_user_id != message.from_user_id:
                return False, paired
            key = message_key(message)
            if key in state.processed_keys:
                return False, paired
            state.processed_keys.append(key)
            state.processed_keys = state.processed_keys[-self.max_processed :]
            self._save_unlocked(state)
            return True, paired

    def get_history(self) -> list[dict[str, str]]:
        return self.load().history

    def append_history(
        self, user: str, assistant: str,
        max_turns: int = HISTORY_MAX_TURNS,
        char_budget: int = HISTORY_CHAR_BUDGET,
    ) -> None:
        with self._exclusive():
            state = self._load_unlocked()
            state.history.append({"user": user, "assistant": assistant})
            state.history[:] = self._trim_history(
                state.history, max_turns=max_turns, char_budget=char_budget,
            )
            self._save_unlocked(state)

    @staticmethod
    def _trim_history(
        history: list[dict[str, str]], *, max_turns: int, char_budget: int,
    ) -> list[dict[str, str]]:
        if not history or max_turns <= 0:
            return []
        kept: list[dict[str, str]] = []
        used = 0
        for turn in reversed(history[-max_turns:]):
            size = len(turn["user"]) + len(turn["assistant"])
            if kept and used + size > char_budget:
                break
            kept.append(turn)
            used += size
        kept.reverse()
        return kept

    def clear_history(self) -> None:
        with self._exclusive():
            state = self._load_unlocked()
            state.history.clear()
            self._save_unlocked(state)

    def reset_all(self) -> None:
        with self._exclusive():
            self._save_unlocked(AutoReplyState())

    def set_current_project(self, project: str) -> None:
        with self._exclusive():
            state = self._load_unlocked()
            state.current_project = project
            state.conversation_mode = "project"
            state.project_histories.setdefault(project, [])
            self._save_unlocked(state)

    def set_chat_mode(self) -> None:
        with self._exclusive():
            state = self._load_unlocked()
            state.conversation_mode = "chat"
            self._save_unlocked(state)

    @staticmethod
    def _valid_turn(turn: object) -> bool:
        return (
            isinstance(turn, dict)
            and set(turn) == {"user", "assistant"}
            and isinstance(turn["user"], str)
            and isinstance(turn["assistant"], str)
        )

    @classmethod
    def _valid_history(cls, history: object) -> bool:
        return isinstance(history, list) and all(cls._valid_turn(turn) for turn in history)

    @contextmanager
    def _exclusive(self, timeout: float = 2.0) -> Iterator[None]:
        with self._thread_lock:
            lock = ProcessLock(self.lock_path)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    lock.acquire()
                    break
                except ProcessLockError as exc:
                    if time.monotonic() >= deadline:
                        raise AutoReplyStateError("自动回复状态正被其他进程使用，请稍后重试") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                lock.release()


class AutoReplyService:
    def __init__(
        self, *, client: ILinkClient, generator: ReplyGenerator, store: AutoReplyStore,
        typing: TypingController | None = None,
    ) -> None:
        self.client = client
        self.generator = generator
        self.store = store
        self.typing = typing
        self._reset_lock = threading.Lock()
        self._pending_reset: tuple[str, float] | None = None

    def handle(
        self, message: WeixinMessage, *, local_image: Path | None = None,
        local_file: InboundTextFile | None = None,
    ) -> str:
        if message.images and not message.text:
            allowed = self.store.load().allowed_user_id
            if allowed is None or allowed != message.from_user_id:
                return "ignored"
        claimed, paired = self.store.claim(message)
        if not claimed:
            return "ignored"
        if self.typing is None:
            return self._handle_claimed(
                message, paired, local_image=local_image, local_file=local_file,
            )
        self.typing.start(message.from_user_id, message.context_token or "")
        try:
            return self._handle_claimed(
                message, paired, local_image=local_image, local_file=local_file,
            )
        finally:
            self.typing.stop(message.from_user_id)

    def _handle_claimed(
        self, message: WeixinMessage, paired: bool, *,
        local_image: Path | None = None, local_file: InboundTextFile | None = None,
    ) -> str:
        if (
            not message.context_token
            or (not message.text and local_image is None and local_file is None)
        ):
            return "skipped"
        command = (message.text or "").strip().rstrip("。！？!?")
        slash = parse_command(message.text or "")
        if slash is not None and slash.error:
            self._send_control_reply(message, slash.error, "command-error")
            return "command-error"
        compact_command = "".join(command.split())
        if compact_command in RESET_AI_STATE_PHRASES or (
            slash is not None and slash.name == "reset-ai-state" and not slash.args
        ):
            with self._reset_lock:
                self._pending_reset = (
                    message.from_user_id,
                    time.monotonic() + AI_RESET_CONFIRM_TTL_SECONDS,
                )
            self._send_control_reply(
                message, AI_RESET_PREVIEW + "\n固定命令：/reset-ai-state confirm。", "reset-preview",
            )
            return "reset-previewed"
        if compact_command in CONFIRM_RESET_AI_STATE_PHRASES or (
            slash is not None and slash.name == "reset-ai-state" and slash.args == ("confirm",)
        ):
            with self._reset_lock:
                pending = self._pending_reset
                self._pending_reset = None
            if (
                pending is None
                or pending[0] != message.from_user_id
                or pending[1] < time.monotonic()
            ):
                self._send_control_reply(
                    message,
                    "当前没有待确认的全部 AI 状态清理。请先发送“清空全部AI状态”查看范围。",
                    "reset-missing-preview",
                )
                return "reset-not-confirmed"
            reset_all = getattr(self.generator, "reset_all_ai_state", None)
            if callable(reset_all):
                reset_all()
            self.store.reset_all()
            self._send_control_reply(
                message,
                "全部本地 AI 状态已清空；微信登录、checkpoint、任务记录、模型配置、响应指标和项目文件均已保留。下一位发来有效文本消息的用户将重新成为唯一白名单。",
                "reset-confirmed",
            )
            return "ai-state-reset"
        if command in CLEAR_HISTORY_PHRASES or (slash is not None and slash.name == "new"):
            clear_context = getattr(self.generator, "clear_context", None)
            if callable(clear_context):
                clear_context()
            else:
                self.store.clear_history()
            self.client.send_text(
                to=message.from_user_id,
                text="对话上下文已清空，我们重新开始。",
                context_token=message.context_token,
                client_id=outbound_client_id(message, "clear"),
            )
            return "history-cleared"
        pending_notifications = getattr(self.generator, "pending_notifications", None)
        acknowledge_notification = getattr(self.generator, "acknowledge_notification", None)
        suppress_pending_notification = getattr(
            self.generator, "suppress_pending_notification", None
        )
        deferred_notifications = []
        if callable(pending_notifications):
            for task in pending_notifications():
                if (
                    callable(suppress_pending_notification)
                    and suppress_pending_notification(message, task)
                ):
                    deferred_notifications.append(task)
                    continue
                self._send_text_parts(
                    message,
                    getattr(self.generator, "task_notice")(task),
                    f"wxbot:task:{task.id}",
                )
                if callable(acknowledge_notification):
                    acknowledge_notification(task.id)
        generate_message = getattr(self.generator, "generate_message", None)
        if callable(generate_message):
            if local_file is not None:
                generated = generate_message(message, local_file=local_file)
            else:
                generated = generate_message(message, local_image=local_image)
        else:
            generated = self.generator.generate(message.text, self.store.get_history())
        if generated.outbound_file is not None:
            self.client.send_text_file(
                to=message.from_user_id,
                path=generated.outbound_file,
                file_name=generated.outbound_name,
                expected_sha256=generated.outbound_sha256,
                context_token=message.context_token,
                client_id=outbound_client_id(message, "file"),
            )
            return "sent"
        if not generated.should_reply:
            return "deferred" if generated.deferred else "declined"
        self._send_text_parts(
            message,
            generated.reply,
            outbound_client_id(message, "auto"),
        )
        if callable(acknowledge_notification):
            for task in deferred_notifications:
                acknowledge_notification(task.id)
        if message.text:
            self.store.append_history(message.text, generated.reply)
        return "paired-and-sent" if paired else "sent"

    def _send_text_parts(
        self, message: WeixinMessage, text: str, base_client_id: str,
    ) -> None:
        parts = split_reply_text(text)
        for index, part in enumerate(parts, 1):
            client_id = (
                base_client_id
                if len(parts) == 1
                else f"{base_client_id}:part:{index}"
            )
            self.client.send_text(
                to=message.from_user_id,
                text=part,
                context_token=message.context_token or "",
                client_id=client_id,
            )

    def _send_control_reply(
        self, message: WeixinMessage, text: str, action: str,
    ) -> None:
        self.client.send_text(
            to=message.from_user_id,
            text=text,
            context_token=message.context_token or "",
            client_id=outbound_client_id(message, action),
        )
