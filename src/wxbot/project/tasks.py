from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable, Iterator

from wxbot.storage.process_lock import ProcessLock, ProcessLockError


ACTIVE_STATUSES = {"queued", "running"}
BLOCKING_STATUSES = ACTIVE_STATUSES | {"waiting_approval"}
FINAL_STATUSES = {"completed", "failed", "cancelled"}
VALID_STATUSES = ACTIVE_STATUSES | FINAL_STATUSES | {"waiting_approval"}
VALID_ACCEPTANCE_STATUSES = {"not_required", "pending", "accepted"}
VALID_CONFIRMATION_ACTIONS = {"", "project_dependency_install", "local_git_commit"}


def safe_task_error(error: Exception) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)\b[A-Z]:\\[^\s]+", "[PATH]", text)
    text = re.sub(r"(?i)\bwxid_[A-Za-z0-9_-]+", "[USER]", text)
    text = re.sub(
        r"(?i)(token|secret|password|authorization|context_token)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    if not text:
        return type(error).__name__
    return f"{type(error).__name__}：{text[:500]}"


class TaskStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectTask:
    id: str
    project: str
    request: str
    status: str
    created_at: float
    updated_at: float
    result: str = ""
    error: str = ""
    notification_pending: bool = False
    cancel_requested: bool = False
    acceptance_status: str = "not_required"
    confirmation_action: str = ""
    confirmation_owner: str = ""
    confirmation_expires_at: float = 0.0
    confirmation_summary: str = ""


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self._thread_lock = threading.RLock()

    def list(self) -> list[ProjectTask]:
        with self._exclusive():
            return self._load_unlocked()

    def get(self, task_id: str) -> ProjectTask | None:
        return next((task for task in self.list() if task.id == task_id), None)

    def latest(self, project: str | None = None) -> ProjectTask | None:
        tasks = [task for task in self.list() if project is None or task.project == project]
        return max(tasks, key=lambda task: task.created_at, default=None)

    def latest_pending_acceptance(
        self, project: str, *, exclude_id: str | None = None,
    ) -> ProjectTask | None:
        tasks = [
            task for task in self.list()
            if task.project == project
            and task.id != exclude_id
            and task.acceptance_status == "pending"
        ]
        return max(tasks, key=lambda task: task.created_at, default=None)

    def active(self, project: str | None = None) -> list[ProjectTask]:
        return sorted(
            (
                task for task in self.list()
                if task.status in BLOCKING_STATUSES
                and (project is None or task.project == project)
            ),
            key=lambda task: (task.created_at, task.id),
        )

    def queue_ahead(self, task_id: str) -> int:
        tasks = self.active()
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

    def create(
        self, *, task_id: str, project: str, request: str,
        confirmation_action: str = "", confirmation_owner: str = "",
        confirmation_ttl: float = 600.0, confirmation_summary: str = "",
    ) -> ProjectTask:
        with self._exclusive():
            tasks = self._load_unlocked()
            if confirmation_action and any(
                task.project == project and task.status in BLOCKING_STATUSES
                for task in tasks
            ):
                raise TaskStateError(f"{project} 已有运行中的修改任务")
            if confirmation_action not in VALID_CONFIRMATION_ACTIONS:
                raise TaskStateError("确认动作无效")
            now = time.time()
            requires_confirmation = bool(confirmation_action)
            task = ProjectTask(
                task_id, project, request,
                "waiting_approval" if requires_confirmation else "queued",
                now, now,
                confirmation_action=confirmation_action,
                confirmation_owner=confirmation_owner if requires_confirmation else "",
                confirmation_expires_at=(
                    now + confirmation_ttl if requires_confirmation else 0.0
                ),
                confirmation_summary=(
                    confirmation_summary if requires_confirmation else ""
                ),
            )
            tasks.append(task)
            self._save_unlocked(tasks)
            return task

    def update(self, task_id: str, **changes: object) -> ProjectTask:
        with self._exclusive():
            tasks = self._load_unlocked()
            for index, task in enumerate(tasks):
                if task.id != task_id:
                    continue
                values = asdict(task)
                values.update(changes)
                values["updated_at"] = time.time()
                updated = self._validate(values)
                tasks[index] = updated
                self._save_unlocked(tasks)
                return updated
        raise TaskStateError("任务不存在")

    def recover(self) -> list[ProjectTask]:
        recovered = []
        for task in self.list():
            if task.status == "running":
                recovered.append(self.update(
                    task.id,
                    status="failed",
                    error="服务重启导致任务中断",
                    result="任务因服务重启中断；真实文件可能已有部分修改，请先检查工作区再继续。",
                    notification_pending=True,
                    cancel_requested=False,
                ))
        return recovered

    def pending_notifications(self) -> list[ProjectTask]:
        return [task for task in self.list() if task.notification_pending]

    def _load_unlocked(self) -> list[ProjectTask]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskStateError("任务状态文件已损坏，已停止任务处理") from exc
        if not isinstance(value, list):
            raise TaskStateError("任务状态格式无效，已停止任务处理")
        return [self._validate(item) for item in value]

    def _save_unlocked(self, tasks: list[ProjectTask]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(task) for task in tasks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _validate(value: object) -> ProjectTask:
        if not isinstance(value, dict):
            raise TaskStateError("任务记录格式无效")
        required = {"id", "project", "request", "status", "created_at", "updated_at"}
        if not required.issubset(value):
            raise TaskStateError("任务记录缺少必要字段")
        task = ProjectTask(
            id=str(value["id"]), project=str(value["project"]),
            request=str(value["request"]), status=str(value["status"]),
            created_at=float(value["created_at"]), updated_at=float(value["updated_at"]),
            result=str(value.get("result", "")), error=str(value.get("error", "")),
            notification_pending=bool(value.get("notification_pending", False)),
            cancel_requested=bool(value.get("cancel_requested", False)),
            acceptance_status=str(value.get("acceptance_status", "not_required")),
            confirmation_action=str(value.get("confirmation_action", "")),
            confirmation_owner=str(value.get("confirmation_owner", "")),
            confirmation_expires_at=float(value.get("confirmation_expires_at", 0.0)),
            confirmation_summary=str(value.get("confirmation_summary", "")),
        )
        if (
            task.status not in VALID_STATUSES
            or task.acceptance_status not in VALID_ACCEPTANCE_STATUSES
            or task.confirmation_action not in VALID_CONFIRMATION_ACTIONS
            or not task.id or not task.project or not task.request
        ):
            raise TaskStateError("任务记录字段无效")
        return task

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
                        raise TaskStateError("任务状态正被其他进程使用") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                lock.release()


TaskExecutor = Callable[[ProjectTask, threading.Event], str]
TaskCompleted = Callable[[ProjectTask], None]


class TaskWorker:
    def __init__(self, *, store: TaskStore, executor: TaskExecutor, completed: TaskCompleted) -> None:
        self.store = store
        self.executor = executor
        self.completed = completed
        self._condition = threading.Condition()
        self._stop = False
        self._events: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.recover()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="wxbot-task-worker", daemon=True)
        self._thread.start()

    def enqueue(
        self, *, task_id: str, project: str, request: str,
        confirmation_action: str = "", confirmation_owner: str = "",
        confirmation_summary: str = "",
    ) -> ProjectTask:
        task = self.store.create(
            task_id=task_id, project=project, request=request,
            confirmation_action=confirmation_action,
            confirmation_owner=confirmation_owner,
            confirmation_summary=confirmation_summary,
        )
        with self._condition:
            self._condition.notify_all()
        return task

    def confirm(self, task_id: str, *, owner: str, action: str) -> ProjectTask:
        task = self.store.get(task_id)
        if task is None:
            raise TaskStateError("任务不存在")
        if task.status != "waiting_approval":
            raise TaskStateError("当前任务不在等待确认")
        if task.confirmation_owner != owner or task.confirmation_action != action:
            raise TaskStateError("确认用户或确认动作不匹配")
        if task.confirmation_expires_at <= time.time():
            self.store.update(
                task.id, status="cancelled", result="确认已过期，任务未执行。",
                notification_pending=False, cancel_requested=True,
            )
            raise TaskStateError("确认已过期，任务未执行")
        updated = self.store.update(task.id, status="queued")
        with self._condition:
            self._condition.notify_all()
        return updated

    def cancel(self, task_id: str) -> ProjectTask:
        task = self.store.get(task_id)
        if task is None:
            raise TaskStateError("任务不存在")
        if task.status in {"queued", "waiting_approval"}:
            return self.store.update(
                task.id, status="cancelled", result="任务已取消。",
                notification_pending=False, cancel_requested=True,
            )
        if task.status == "running":
            updated = self.store.update(task.id, cancel_requested=True)
            event = self._events.get(task.id)
            if event:
                event.set()
            return updated
        raise TaskStateError("当前任务不能取消")

    def close(self) -> None:
        with self._condition:
            self._stop = True
            for event in self._events.values():
                event.set()
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
            task = next((item for item in self.store.list() if item.status == "queued"), None)
            if task is None:
                with self._condition:
                    self._condition.wait(timeout=0.5)
                continue
            event = threading.Event()
            self._events[task.id] = event
            self.store.update(task.id, status="running")
            try:
                result = self.executor(task, event)
                if event.is_set():
                    completed = self.store.update(
                        task.id, status="cancelled", result="任务已取消。",
                        notification_pending=True, cancel_requested=True,
                    )
                else:
                    completed = self.store.update(
                        task.id, status="completed", result=result,
                        notification_pending=True,
                    )
            except Exception as exc:
                cancelled = event.is_set()
                error = safe_task_error(exc)
                completed = self.store.update(
                    task.id, status="cancelled" if cancelled else "failed",
                    result=(
                        "任务已取消；已经写入的真实文件不会自动回滚。"
                        if cancelled else f"任务执行失败：{error}"
                    ),
                    error="" if cancelled else error,
                    notification_pending=True,
                )
            finally:
                self._events.pop(task.id, None)
            self.completed(completed)
