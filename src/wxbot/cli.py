from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
import uuid
import re
from pathlib import Path

from wxbot import __version__
from wxbot.ai.codex_reply import CodexReplyError
from wxbot.ai.app_server import AppServerClient, AppServerError, actionable_server_error
from wxbot.ai.session_manager import SessionManager
from wxbot.ai.model_config import ModelConfig
from wxbot.ai.turn_metrics import TurnMetricsStore
from wxbot.ai.thread_sessions import ThreadSessionStateError, ThreadSessionStore
from wxbot.api.client import ILinkClient, ILinkError
from wxbot.api.models import WeixinMessage
from wxbot.auth.qr_login import login
from wxbot.daemon import DaemonController, RuntimeFiles, RuntimeHealth
from wxbot.message.auto_reply import (
    AI_RESET_PREVIEW,
    AutoReplyService,
    AutoReplyStateError,
    AutoReplyStore,
    split_reply_text,
)
from wxbot.message.inbox import InboxStateError, InboxStore
from wxbot.message.media import (
    InboundImageManager,
    InboundTextFile,
    InboundTextFileManager,
    VoiceProbeStore,
)
from wxbot.message.poller import MessagePoller
from wxbot.message.typing import TypingController
from wxbot.notices import inbound_message_notice, timed_terminal_notice
from wxbot.project.control import ProjectController
from wxbot.project.checkpoints import CheckpointError, TaskCheckpointStore
from wxbot.project.status import ProjectStatusError, ProjectStatusReader
from wxbot.project.tasks import ProjectTask, TaskStateError, TaskStore, TaskWorker
from wxbot.replies import AppServerReplyGenerator, safe_app_server_error
from wxbot.dispatcher import MessageDispatcher
from wxbot.runtime_log import log_event, setup_runtime_log
from wxbot.storage.process_lock import ProcessLock, ProcessLockError
from wxbot.storage.session_store import SessionStateError, SessionStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_PATH = PROJECT_ROOT / "data" / "session.json"
DEFAULT_AUTO_REPLY_PATH = PROJECT_ROOT / "data" / "auto_reply.json"
DEFAULT_THREAD_SESSIONS_PATH = PROJECT_ROOT / "data" / "thread_sessions.json"
DEFAULT_MODEL_CONFIG_PATH = PROJECT_ROOT / "data" / "model_config.json"
DEFAULT_VOICE_PROBE_PATH = PROJECT_ROOT / "data" / "voice_probe.json"


def find_codex_executable() -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        desktop_bin = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        try:
            candidates = [path for path in desktop_bin.glob("*/codex.exe") if path.is_file()]
            if candidates:
                return str(max(candidates, key=lambda path: path.stat().st_mtime))
        except OSError:
            pass

    current_cli = shutil.which("codex.cmd")
    if current_cli:
        return current_cli

    nvm_home_value = os.environ.get("NVM_HOME", "").strip()
    if nvm_home_value:
        nvm_home = Path(nvm_home_value)
        try:
            version_dirs = sorted(
                (
                    path
                    for path in nvm_home.iterdir()
                    if path.is_dir() and re.fullmatch(r"v?\d+\.\d+\.\d+", path.name)
                ),
                key=lambda path: tuple(
                    int(part) for part in path.name.removeprefix("v").split(".")
                ),
                reverse=True,
            )
        except OSError:
            version_dirs = []
        for version_dir in version_dirs:
            candidate = version_dir / "codex.cmd"
            if candidate.is_file():
                return str(candidate)

    return shutil.which("codex")


def safe_terminal_error(error: Exception) -> str:
    if isinstance(error, ILinkError):
        return safe_poll_error(error)
    if isinstance(error, AppServerError):
        actionable = actionable_server_error(str(error))
        if actionable.startswith("Codex未登录"):
            return actionable
        return type(error).__name__
    return type(error).__name__


def safe_poll_error(error: Exception) -> str:
    if isinstance(error, ILinkError):
        if error.code == -14:
            return "SessionExpired"
        message = str(error)
        if message == "iLink 网络请求失败":
            return "NetworkError"
        if re.fullmatch(r"iLink HTTP \d{3}", message):
            return message.replace("iLink ", "", 1)
        if message == "iLink 返回了无效 JSON":
            return "InvalidJSON"
        if message == "iLink 返回结构不是对象":
            return "InvalidResponse"
        if error.code is not None:
            return f"ServiceError({error.code})"
    return type(error).__name__


class Conversations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[WeixinMessage] = []

    def add(self, message: WeixinMessage) -> int:
        with self._lock:
            self._items.append(message)
            return len(self._items)

    def get(self, number: int | None = None) -> WeixinMessage | None:
        with self._lock:
            if not self._items:
                return None
            if number is None:
                return self._items[-1]
            if number < 1 or number > len(self._items):
                return None
            return self._items[number - 1]

    def summary(self) -> list[tuple[int, str]]:
        with self._lock:
            return [(index, item.from_user_id[-16:]) for index, item in enumerate(self._items, 1)]


def login_command(store: SessionStore) -> int:
    runtime = RuntimeFiles(store.path.parent)
    previous_health = runtime.load_health()
    restart_after_login = (
        previous_health is not None and previous_health.status == "session_expired"
    )
    previous_state = store.load()
    state = login(existing_state=previous_state)
    store.save(state)
    print("登录成功")
    if restart_after_login:
        controller = DaemonController(project_root=PROJECT_ROOT, session_path=store.path)
        success, message = controller.start()
        print(message, file=sys.stdout if success else sys.stderr)
        return 0 if success else 1
    return 0


def start_command(store: SessionStore) -> int:
    process_lock = ProcessLock(store.path.parent / "wxbot.lock")
    try:
        process_lock.acquire()
    except ProcessLockError as exc:
        print(f"启动失败：{safe_terminal_error(exc)}", file=sys.stderr)
        return 3
    state = store.load()
    if state is None:
        print("尚未登录，请先运行：wxbot login", file=sys.stderr)
        process_lock.release()
        return 2
    conversations = Conversations()
    client = ILinkClient(base_url=state.base_url, token=state.bot_token)

    def on_message(message: WeixinMessage) -> None:
        number = conversations.add(message)
        print(f"\n{inbound_message_notice(message, number)}")
        print("回复最近会话可直接输入文字；指定会话使用 /reply 编号 内容")

    def on_error(error: Exception, failures: int, retry_in: float) -> None:
        if isinstance(error, ILinkError) and error.code == -14:
            print("\n微信会话已失效，请退出后重新执行 login。", file=sys.stderr)
        else:
            print(
                f"\n轮询暂时失败：{safe_poll_error(error)}；"
                f"连续失败 {failures} 次，{int(retry_in)} 秒后重试",
                file=sys.stderr,
            )

    poller = MessagePoller(
        client=client,
        store=store,
        state=state,
        on_message=on_message,
        on_error=on_error,
    )
    thread = threading.Thread(target=poller.run, name="wxbot-poller", daemon=True)
    thread.start()
    print("wxbot 已启动。输入 /help 查看命令，输入 /quit 退出。")
    try:
        while thread.is_alive():
            try:
                command = input("> ").strip()
            except EOFError:
                break
            if not command:
                continue
            if command == "/quit":
                break
            if command == "/help":
                print("普通文字：回复最近会话；/reply 编号 内容：指定会话；/list：列出会话；/quit：退出")
                continue
            if command == "/list":
                for number, _masked_user in conversations.summary():
                    print(f"[{number}] 微信会话")
                continue
            target_number: int | None = None
            text = command
            if command.startswith("/reply "):
                parts = command.split(" ", 2)
                if len(parts) != 3 or not parts[1].isdigit():
                    print("格式：/reply 编号 内容")
                    continue
                target_number = int(parts[1])
                text = parts[2].strip()
            target = conversations.get(target_number)
            if target is None:
                print("没有可回复的会话，请先从微信向 Bot 发一条消息。")
                continue
            if not target.context_token:
                print("该消息缺少必要上下文，不能回复。")
                continue
            try:
                client.send_text(
                    to=target.from_user_id,
                    text=text,
                    context_token=target.context_token,
                    client_id=f"wxbot:{uuid.uuid4()}",
                )
                print("发送成功")
            except Exception as exc:
                print(f"发送失败：{type(exc).__name__}", file=sys.stderr)
    except KeyboardInterrupt:
        print()
    finally:
        poller.stop()
        client.close()
        process_lock.release()
    return 0


def auto_start_command(store: SessionStore, daemon_id: str | None = None) -> int:
    runtime = RuntimeFiles(store.path.parent) if daemon_id else None
    started_at = time.time()
    setup_runtime_log(store.path.parent)
    log_event("startup")

    def startup_error(error_type: str, message: str, code: int) -> int:
        if runtime is not None and daemon_id is not None:
            runtime.write_health(RuntimeHealth(daemon_id, "error", started_at, error_type=error_type))
        log_event("startup_failed", f"{error_type}: {message}")
        print(message, file=sys.stderr)
        return code

    process_lock = ProcessLock(store.path.parent / "wxbot.lock")
    try:
        process_lock.acquire()
    except ProcessLockError as exc:
        return startup_error("ProcessLockError", safe_terminal_error(exc), 3)
    try:
        state = store.load()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        process_lock.release()
        return startup_error(type(exc).__name__, "登录会话文件无效，自动回复未启动", 2)
    if state is None:
        process_lock.release()
        return startup_error("SessionMissing", "尚未登录，请先运行：wxbot login", 2)
    client = ILinkClient(base_url=state.base_url, token=state.bot_token)
    typing = TypingController(client)
    codex_executable = find_codex_executable()
    if codex_executable is None:
        client.close()
        process_lock.release()
        return startup_error(
            "CodexNotFound", "未找到 Codex CLI；请先安装 Codex CLI并完成登录，然后重新启动", 2,
        )
    auto_reply_store = AutoReplyStore(DEFAULT_AUTO_REPLY_PATH)
    thread_store = ThreadSessionStore(PROJECT_ROOT / "data" / "thread_sessions.json")
    try:
        thread_store.load()
    except ThreadSessionStateError as exc:
        client.close()
        process_lock.release()
        return startup_error(type(exc).__name__, safe_terminal_error(exc), 2)
    project_controller = ProjectController(
        projects_root=PROJECT_ROOT.parent,
        data_dir=PROJECT_ROOT / "data",
        auto_reply_store=auto_reply_store,
        executable=codex_executable,
    )
    try:
        model_config = ModelConfig.load(DEFAULT_MODEL_CONFIG_PATH)
    except ValueError as exc:
        client.close()
        process_lock.release()
        return startup_error(type(exc).__name__, safe_terminal_error(exc), 2)
    app_server = AppServerClient(
        executable=codex_executable,
        model=model_config.model,
        reasoning_effort=model_config.reasoning_effort,
    )
    try:
        app_server.start()
    except Exception as exc:
        client.close()
        process_lock.release()
        error = safe_terminal_error(exc)
        error_type = "CodexNotLoggedIn" if error.startswith("Codex未登录") else type(exc).__name__
        message = error
        return startup_error(error_type, message, 2)
    sessions = SessionManager(
        client=app_server,
        store=auto_reply_store,
        chat_workspace=Path.home(),
        project_workspace=lambda project, _message: project_controller.project_path(project),
        thread_store=thread_store,
        metrics_store=TurnMetricsStore(PROJECT_ROOT / "data" / "turn_metrics.json"),
    )
    sessions.migrate_current_thread_visibility()
    def send_task_result(message: WeixinMessage, text: str, task_id: str) -> None:
        if not message.context_token:
            return
        parts = split_reply_text(text)
        for index, part in enumerate(parts, 1):
            client.send_text(
                to=message.from_user_id,
                text=part,
                context_token=message.context_token,
                client_id=(
                    f"wxbot:task:{task_id}"
                    if len(parts) == 1
                    else f"wxbot:task:{task_id}:part:{index}"
                ),
            )

    checkpoints = TaskCheckpointStore(PROJECT_ROOT / "data" / "checkpoints")
    reply_generator = AppServerReplyGenerator(
        sessions=sessions, projects=project_controller, task_sender=send_task_result,
        checkpoints=checkpoints,
    )

    def execute_task(task: ProjectTask, cancel_event: threading.Event) -> str:
        project_path = project_controller.project_path(task.project)
        checkpoints.begin(task.id, task.project, project_path)
        task_message = reply_generator.task_message(task.id)
        if typing is not None and task_message is not None:
            typing.start(task_message.from_user_id, task_message.context_token or "")
        try:
            result = sessions.run_project_task(
                project=task.project, message=task.request, cancel_event=cancel_event,
                authorized_action=task.confirmation_action,
            )
        except Exception as exc:
            try:
                checkpoints.finish(task.id)
            except CheckpointError:
                pass
            log_event("task_failed", f"task={task.id} {safe_terminal_error(exc)}")
            raise
        else:
            checkpoints.finish(task.id)
            return result

    def complete_task(task: ProjectTask) -> None:
        task_message = reply_generator.task_message(task.id)
        try:
            reply_generator.task_completed(task)
        finally:
            if typing is not None and task_message is not None:
                typing.stop(task_message.from_user_id)

    task_worker = TaskWorker(
        store=TaskStore(PROJECT_ROOT / "data" / "tasks.json"),
        executor=execute_task,
        completed=complete_task,
    )
    for recovered_task in task_worker.store.recover():
        try:
            checkpoints.finish(recovered_task.id)
        except CheckpointError:
            pass
    reply_generator.attach_worker(task_worker)
    try:
        task_worker.start()
    except TaskStateError as exc:
        reply_generator.close()
        client.close()
        process_lock.release()
        return startup_error(type(exc).__name__, safe_terminal_error(exc), 2)
    service = AutoReplyService(
        client=client,
        generator=reply_generator,  # type: ignore[arg-type]
        store=auto_reply_store,
        typing=typing,
    )

    def process_message(
        message: WeixinMessage, local_image: Path | None = None,
        local_file: InboundTextFile | None = None,
    ) -> bool:
        try:
            result = service.handle(
                message, local_image=local_image, local_file=local_file,
            )
            labels = {
                "paired-and-sent": "已配对首位用户并自动回复",
                "sent": "已自动回复",
                "ignored": "非白名单或重复消息，未回复",
                "declined": "Codex 拒绝自动回复",
                "deferred": "已交给后台任务处理，等待完成结果",
                "skipped": "消息缺少必要上下文，未回复",
            }
            print(timed_terminal_notice(labels.get(result, result)))
            return True
        except AutoReplyStateError as exc:
            log_event("auto_reply_failed", safe_terminal_error(exc))
            print(
                timed_terminal_notice(f"自动回复状态异常：{safe_terminal_error(exc)}"),
                file=sys.stderr,
            )
            poller.stop()
            client.close()
            return False
        except CodexReplyError as exc:
            log_event("auto_reply_failed", safe_terminal_error(exc))
            print(
                timed_terminal_notice(f"Codex 生成失败，未发送：{safe_terminal_error(exc)}"),
                file=sys.stderr,
            )
            return False
        except AppServerError as exc:
            log_event("auto_reply_failed", safe_terminal_error(exc))
            print(
                timed_terminal_notice(
                    f"App Server请求失败，未发送：{safe_terminal_error(exc)}"
                ),
                file=sys.stderr,
            )
            return False
        except Exception as exc:
            log_event("auto_reply_failed", type(exc).__name__)
            print(
                timed_terminal_notice(f"自动回复失败，未发送：{type(exc).__name__}"),
                file=sys.stderr,
            )
            return False

    inbox = InboxStore(PROJECT_ROOT / "data" / "inbox.json")
    try:
        queued_messages, uncertain_count = inbox.recover()
    except InboxStateError as exc:
        reply_generator.close()
        client.close()
        process_lock.release()
        return startup_error(type(exc).__name__, safe_terminal_error(exc), 2)
    dispatcher = MessageDispatcher(
        handler=process_message,
        generator=reply_generator,
        inbox=inbox,
        image_manager=InboundImageManager(PROJECT_ROOT / "tmp" / "inbound-media"),
        file_manager=InboundTextFileManager(PROJECT_ROOT / "tmp" / "inbound-files"),
        voice_probe=VoiceProbeStore(DEFAULT_VOICE_PROBE_PATH),
        auto_reply_store=auto_reply_store,
    )

    def on_message(message: WeixinMessage) -> None:
        print(f"\n{timed_terminal_notice(inbound_message_notice(message))}")
        dispatcher.submit(message)

    last_poll_at = started_at
    session_expired = False

    def on_error(error: Exception, failures: int, retry_in: float) -> None:
        nonlocal session_expired
        if isinstance(error, ILinkError) and error.code == -14:
            session_expired = True
        log_event(
            "poll_error",
            f"error={safe_poll_error(error)} failures={failures} retry_in={int(retry_in)}",
        )
        if runtime is not None and daemon_id is not None:
            runtime.write_health(
                RuntimeHealth(
                    daemon_id, "running", started_at, last_poll_at,
                    safe_poll_error(error), failures, retry_in,
                )
            )
        if isinstance(error, ILinkError) and error.code == -14:
            print(
                f"\n{timed_terminal_notice('微信会话已失效，请重新登录。')}",
                file=sys.stderr,
            )
        else:
            print(
                "\n"
                + timed_terminal_notice(
                    f"轮询暂时失败：{safe_poll_error(error)}；"
                    f"连续失败 {failures} 次，{int(retry_in)} 秒后重试"
                ),
                file=sys.stderr,
            )

    def on_poll_success() -> None:
        nonlocal last_poll_at
        last_poll_at = time.time()
        if runtime is not None and daemon_id is not None:
            runtime.write_health(RuntimeHealth(daemon_id, "running", started_at, last_poll_at))

    poller = MessagePoller(
        client=client, store=store, state=state,
        on_message=on_message, on_error=on_error, on_poll_success=on_poll_success,
    )
    dispatcher.restore(queued_messages)
    if uncertain_count:
        print(
            timed_terminal_notice(
                f"检测到 {uncertain_count} 条发送结果不确定的中断消息，未自动重发。"
            ),
            file=sys.stderr,
        )
    if runtime is not None and daemon_id is not None:
        runtime.write_health(RuntimeHealth(daemon_id, "running", started_at, started_at))

        def watch_control() -> None:
            last_compaction_request: str | None = None
            last_clear_context_request: str | None = None
            while not poller.stop_event.wait(0.2):
                if runtime.stop_requested(daemon_id):
                    poller.stop()
                    client.close()
                    return
                clear_request_id = runtime.clear_context_request(daemon_id)
                if (
                    clear_request_id is not None
                    and clear_request_id != last_clear_context_request
                ):
                    last_clear_context_request = clear_request_id
                    try:
                        reply_generator.clear_context()
                    except Exception as exc:
                        runtime.write_clear_context_result(
                            daemon_id,
                            clear_request_id,
                            success=False,
                            message=f"上下文清理失败：{safe_terminal_error(exc)}",
                        )
                    else:
                        runtime.write_clear_context_result(
                            daemon_id,
                            clear_request_id,
                            success=True,
                            message=(
                                "当前对话上下文已清空；其他项目、白名单和消息去重记录均保留。"
                            ),
                        )
                    continue
                request_id = runtime.compact_request(daemon_id)
                if request_id is None or request_id == last_compaction_request:
                    continue
                last_compaction_request = request_id
                try:
                    reply_generator.compact_context()
                except Exception as exc:
                    runtime.write_compaction_result(
                        daemon_id,
                        request_id,
                        success=False,
                        message=f"上下文压缩失败：{safe_terminal_error(exc)}",
                    )
                else:
                    runtime.write_compaction_result(
                        daemon_id,
                        request_id,
                        success=True,
                        message="当前对话上下文已压缩；对话内容、其他项目和本地状态均保留。",
                    )

        threading.Thread(target=watch_control, name="wxbot-control", daemon=True).start()
    print(
        timed_terminal_notice(
            "自动回复已启动。首次发来文本消息的用户将成为唯一白名单。按 Ctrl+C 退出。"
        )
    )
    try:
        poller.run()
    except KeyboardInterrupt:
        print()
    finally:
        poller.stop()
        dispatcher.close()
        if dispatcher.image_manager is not None:
            dispatcher.image_manager.close()
        if dispatcher.file_manager is not None:
            dispatcher.file_manager.close()
        typing.close()
        client.close()
        close_generator = getattr(reply_generator, "close", None)
        if callable(close_generator):
            close_generator()
        process_lock.release()
        if runtime is not None and daemon_id is not None:
            if session_expired:
                runtime.write_health(RuntimeHealth(
                    daemon_id, "session_expired", started_at, last_poll_at,
                    "SessionExpired", 1, 0,
                ))
            else:
                runtime.write_health(RuntimeHealth(daemon_id, "stopped", started_at, time.time()))
        log_event("stopped", "session_expired" if session_expired else "clean")
    return 0


def service_command(command: str, store: SessionStore) -> int:
    controller = DaemonController(project_root=PROJECT_ROOT, session_path=store.path)
    actions = {
        "start": controller.start,
        "stop": controller.stop,
        "status": controller.status,
        "restart": controller.restart,
    }
    success, message = actions[command]()
    print(message, file=sys.stdout if success else sys.stderr)
    return 0 if success else 1


def setup_command(store: SessionStore) -> int:
    if find_codex_executable() is None:
        print("首次设置失败：未找到 Codex CLI；请先安装并完成 codex login", file=sys.stderr)
        return 1
    if store.load() is None:
        result = login_command(store)
        if result != 0:
            return result
    return service_command("start", store)


def clear_context_command() -> int:
    controller = DaemonController(
        project_root=PROJECT_ROOT,
        session_path=DEFAULT_SESSION_PATH,
    )
    success, message = controller.clear_context()
    print(message, file=sys.stdout if success else sys.stderr)
    return 0 if success else 1


def compact_context_command() -> int:
    controller = DaemonController(
        project_root=PROJECT_ROOT,
        session_path=DEFAULT_SESSION_PATH,
    )
    success, message = controller.compact()
    print(message, file=sys.stdout if success else sys.stderr)
    return 0 if success else 1


def reset_ai_state_command(*, confirmed: bool, store: SessionStore) -> int:
    if not confirmed:
        print(AI_RESET_PREVIEW)
        return 0
    controller = DaemonController(project_root=PROJECT_ROOT, session_path=store.path)
    state = controller.load_state()
    was_running = state is not None and controller.matches_process(state)
    if was_running:
        stopped, message = controller.stop()
        if not stopped:
            print(f"全部 AI 状态未清空：{message}", file=sys.stderr)
            return 1
    AutoReplyStore(DEFAULT_AUTO_REPLY_PATH).reset_all()
    ThreadSessionStore(DEFAULT_THREAD_SESSIONS_PATH).clear_all()
    if was_running:
        started, message = controller.start()
        if not started:
            print(f"全部本地 AI 状态已清空，但自动回复重启失败：{message}", file=sys.stderr)
            return 1
    print(
        "全部本地 AI 状态已清空；微信登录、checkpoint、任务记录、模型配置、"
        "响应指标和项目文件均已保留"
    )
    return 0


def project_status_command(project: str | None = None) -> int:
    if project is None:
        project = AutoReplyStore(DEFAULT_AUTO_REPLY_PATH).load().current_project or "wxbot"
    if re.fullmatch(r"[A-Za-z0-9_.-]+", project) is None:
        print("项目名称格式无效", file=sys.stderr)
        return 1
    projects_root = PROJECT_ROOT.parent.resolve()
    project_path = (projects_root / project).resolve()
    if project_path.parent != projects_root or not project_path.is_dir():
        print(f"项目不存在：{project}", file=sys.stderr)
        return 1
    try:
        print(ProjectStatusReader(project_path / "ROADMAP.md").read().render("progress"))
    except ProjectStatusError as exc:
        print(f"项目进度读取失败：{safe_terminal_error(exc)}", file=sys.stderr)
        return 1
    return 0


def doctor_command() -> int:
    executable = find_codex_executable()
    if executable is None:
        print("兼容检查失败：未找到 Codex CLI", file=sys.stderr)
        return 1
    client = AppServerClient(executable=executable)
    stage = "initialize"
    try:
        client.start()
        stage = "thread/list"
        records = client.list_threads(cwd=PROJECT_ROOT, limit=50)
        compatibility_name = "wxbot Schema兼容检查"
        compatibility_thread = next(
            (record.id for record in records if record.name == compatibility_name),
            None,
        )
        if compatibility_thread is None:
            stage = "thread/start(persistent)"
            compatibility_thread = client.start_thread(
                cwd=PROJECT_ROOT,
                instructions="这是协议兼容检查专用 Thread，不执行项目任务。",
                ephemeral=False,
                name=compatibility_name,
            )
        stage = "thread/start(ephemeral)"
        probe_thread = client.start_thread(
            cwd=PROJECT_ROOT,
            instructions="这是协议兼容检查，只回复 OK，不修改文件。",
        )
        stage = "thread/resume"
        resumed_id = client.resume_thread(
            thread_id=compatibility_thread,
            cwd=PROJECT_ROOT,
            instructions="这是协议兼容检查专用 Thread，不执行项目任务。",
        )
        stage = "thread/read"
        client.read_thread(resumed_id)
        stage = "turn/start"
        client.turn(thread_id=probe_thread, text="只回复 OK。", timeout=120)
        stage = "thread/compact/start"
        client.compact_thread(thread_id=probe_thread, timeout=120)
    except AppServerError as exc:
        print(f"兼容检查失败（{stage}）：{safe_terminal_error(exc)}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print("Codex CLI与 App Server兼容检查通过")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wxbot", description="微信 iLink 文本收发客户端")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    commands.add_parser("setup", help="首次设置并启动")
    commands.add_parser("login", help="扫码登录或重新登录微信")
    commands.add_parser("start", help="后台启动自动回复")
    commands.add_parser("stop", help="停止后台自动回复")
    commands.add_parser("status", help="查看后台健康状态")
    commands.add_parser("restart", help="重启后台自动回复")
    commands.add_parser("run", help="前台运行基础消息收发，供调试使用")

    context = commands.add_parser("context", help="管理当前对话上下文")
    context_commands = context.add_subparsers(
        dest="context_command", metavar="ACTION", required=True,
    )
    context_commands.add_parser("clear", help="清空当前对话并在下一轮创建新 Thread")
    context_commands.add_parser("compact", help="压缩当前对话并保留语义内容")

    reset = commands.add_parser("reset-ai-state", help="预览或确认重置全部本地 AI状态")
    reset.add_argument(
        "--confirm", action="store_true", help="执行预览中列出的完整 AI状态重置",
    )

    project = commands.add_parser("project", help="读取项目正式状态")
    project_commands = project.add_subparsers(
        dest="project_command", metavar="ACTION", required=True,
    )
    project_status = project_commands.add_parser("status", help="读取 ROADMAP.md正式状态")
    project_status.add_argument("project", nargs="?", help="同级项目名称，默认使用当前项目")

    commands.add_parser("doctor", help="检查 Codex CLI和 App Server兼容性")
    return parser


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "_worker":
        parser = argparse.ArgumentParser(prog="wxbot _worker", add_help=False)
        parser.add_argument("command", choices=("_worker",))
        parser.add_argument("--session", type=Path, default=DEFAULT_SESSION_PATH)
        parser.add_argument("--daemon-id", required=True)
        return parser.parse_args(raw_args)
    return build_parser().parse_args(raw_args)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = SessionStore(getattr(args, "session", DEFAULT_SESSION_PATH))
    try:
        if args.command == "setup":
            return setup_command(store)
        if args.command == "login":
            return login_command(store)
        if args.command == "_worker":
            return auto_start_command(store, args.daemon_id)
        if args.command in {"start", "stop", "status", "restart"}:
            return service_command(args.command, store)
        if args.command == "run":
            return start_command(store)
        if args.command == "context" and args.context_command == "clear":
            return clear_context_command()
        if args.command == "context" and args.context_command == "compact":
            return compact_context_command()
        if args.command == "reset-ai-state":
            return reset_ai_state_command(confirmed=args.confirm, store=store)
        if args.command == "project" and args.project_command == "status":
            return project_status_command(args.project)
        if args.command == "doctor":
            return doctor_command()
        raise AssertionError(f"未处理的 CLI命令：{args.command}")
    except ILinkError as exc:
        print(f"操作失败：{safe_terminal_error(exc)}", file=sys.stderr)
        return 1
    except SessionStateError as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    except AutoReplyStateError as exc:
        print(f"操作失败：{safe_terminal_error(exc)}", file=sys.stderr)
        return 4
