from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from ctypes import WinDLL, byref
from ctypes.wintypes import DWORD, FILETIME, HANDLE
from dataclasses import asdict, dataclass
from pathlib import Path

from wxbot.message.inbox import InboxStateError, InboxStore


STARTUP_ERROR_HINTS = {
    "CodexNotFound": "未找到 Codex CLI；请先安装 Codex CLI并完成登录",
    "CodexNotLoggedIn": "Codex未登录；请在本机运行 codex login，完成后重新启动",
    "AppServerError": "Codex App Server启动失败；请运行 wxbot restart，持续失败时检查 Codex登录状态",
}


@dataclass(frozen=True)
class DaemonState:
    pid: int
    created_at: int
    daemon_id: str


@dataclass(frozen=True)
class RuntimeHealth:
    daemon_id: str
    status: str
    started_at: float
    last_poll_at: float | None = None
    error_type: str | None = None
    consecutive_failures: int = 0
    retry_in_seconds: float | None = None


class RuntimeFiles:
    def __init__(self, data_dir: Path) -> None:
        self.health_path = data_dir / "wxbot.health.json"
        self.control_path = data_dir / "wxbot.control.json"
        self.compaction_result_path = data_dir / "wxbot.compaction.json"
        self.clear_context_result_path = data_dir / "wxbot.context-clear.json"

    def write_health(self, health: RuntimeHealth) -> None:
        self._write_json(self.health_path, asdict(health))

    def load_health(self) -> RuntimeHealth | None:
        try:
            data = json.loads(self.health_path.read_text(encoding="utf-8"))
            return RuntimeHealth(
                daemon_id=str(data["daemon_id"]),
                status=str(data["status"]),
                started_at=float(data["started_at"]),
                last_poll_at=float(data["last_poll_at"]) if data.get("last_poll_at") is not None else None,
                error_type=str(data["error_type"]) if data.get("error_type") else None,
                consecutive_failures=int(data.get("consecutive_failures", 0)),
                retry_in_seconds=(
                    float(data["retry_in_seconds"])
                    if data.get("retry_in_seconds") is not None else None
                ),
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def request_run(self, daemon_id: str) -> None:
        self._write_json(self.control_path, {"daemon_id": daemon_id, "action": "run"})

    def request_stop(self, daemon_id: str) -> None:
        self._write_json(self.control_path, {"daemon_id": daemon_id, "action": "stop"})

    def request_compact(self, daemon_id: str, request_id: str) -> None:
        self._request_operation(
            self.compaction_result_path, daemon_id, request_id, "compact",
        )

    def compact_request(self, daemon_id: str) -> str | None:
        return self._operation_request(daemon_id, "compact")

    def request_clear_context(self, daemon_id: str, request_id: str) -> None:
        self._request_operation(
            self.clear_context_result_path, daemon_id, request_id, "clear-context",
        )

    def clear_context_request(self, daemon_id: str) -> str | None:
        return self._operation_request(daemon_id, "clear-context")

    def _request_operation(
        self, result_path: Path, daemon_id: str, request_id: str, action: str,
    ) -> None:
        self._write_json(
            result_path,
            {
                "daemon_id": daemon_id,
                "request_id": request_id,
                "status": "pending",
            },
        )
        self._write_json(
            self.control_path,
            {
                "daemon_id": daemon_id,
                "action": action,
                "request_id": request_id,
            },
        )

    def _operation_request(self, daemon_id: str, action: str) -> str | None:
        try:
            data = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        request_id = data.get("request_id")
        if (
            data.get("daemon_id") != daemon_id
            or data.get("action") != action
            or not isinstance(request_id, str)
            or not request_id
        ):
            return None
        return request_id

    def write_compaction_result(
        self, daemon_id: str, request_id: str, *, success: bool, message: str,
    ) -> None:
        self._write_operation_result(
            self.compaction_result_path, daemon_id, request_id,
            success=success, message=message,
        )

    def write_clear_context_result(
        self, daemon_id: str, request_id: str, *, success: bool, message: str,
    ) -> None:
        self._write_operation_result(
            self.clear_context_result_path, daemon_id, request_id,
            success=success, message=message,
        )

    def _write_operation_result(
        self, result_path: Path, daemon_id: str, request_id: str,
        *, success: bool, message: str,
    ) -> None:
        self._write_json(result_path, {
            "daemon_id": daemon_id,
            "request_id": request_id,
            "status": "success" if success else "failed",
            "message": message,
        })

    def load_compaction_result(self) -> dict[str, str] | None:
        return self._load_operation_result(self.compaction_result_path)

    def load_clear_context_result(self) -> dict[str, str] | None:
        return self._load_operation_result(self.clear_context_result_path)

    @staticmethod
    def _load_operation_result(path: Path) -> dict[str, str] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        daemon_id = data.get("daemon_id")
        request_id = data.get("request_id")
        status = data.get("status")
        message = data.get("message", "")
        if not all(isinstance(value, str) and value for value in (daemon_id, request_id, status)):
            return None
        if not isinstance(message, str):
            return None
        return {
            "daemon_id": daemon_id,
            "request_id": request_id,
            "status": status,
            "message": message,
        }

    def stop_requested(self, daemon_id: str) -> bool:
        try:
            data = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return data.get("daemon_id") == daemon_id and data.get("action") == "stop"

    @staticmethod
    def _write_json(path: Path, data: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)


class DaemonController:
    def __init__(
        self,
        *,
        project_root: Path,
        session_path: Path,
        startup_timeout: float = 30.0,
        stop_timeout: float = 5.0,
        compact_timeout: float = 120.0,
    ) -> None:
        self.project_root = project_root
        self.session_path = session_path
        self.state_path = session_path.parent / "wxbot.pid.json"
        self.runtime = RuntimeFiles(session_path.parent)
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.compact_timeout = compact_timeout

    def start(self) -> tuple[bool, str]:
        state = self.load_state()
        if state is not None and self.matches_process(state):
            return self.status()
        if not self.session_path.exists():
            return False, "尚未登录，请先运行：wxbot login"

        daemon_id = uuid.uuid4().hex
        self.runtime.request_run(daemon_id)
        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        command: list[str]
        if os.name == "nt":
            command = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                sys.executable,
                "-m",
                "wxbot",
                "_worker",
                "--daemon-id",
                daemon_id,
            ]
        else:
            command = [
                sys.executable, "-m", "wxbot", "_worker", "--daemon-id", daemon_id,
            ]
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
        )
        created_at = self.process_identity(process.pid)
        if created_at is None:
            return False, "后台进程启动失败"
        state = DaemonState(pid=process.pid, created_at=created_at, daemon_id=daemon_id)
        self.save_state(state)

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            health = self.runtime.load_health()
            if health is not None and health.daemon_id == daemon_id:
                if health.status == "running":
                    return True, f"自动回复已启动（PID {process.pid}）"
                if health.status == "error":
                    detail = STARTUP_ERROR_HINTS.get(
                        health.error_type or "", health.error_type or "UnknownError",
                    )
                    return False, f"自动回复启动失败：{detail}"
            if not self.matches_process(state):
                return False, "自动回复启动失败：进程已退出"
            time.sleep(0.05)
        self.stop()
        return False, "自动回复启动超时，未收到健康信号"

    def stop(self) -> tuple[bool, str]:
        state = self.load_state()
        if state is None or not self.matches_process(state):
            return True, "自动回复未运行"
        self.runtime.request_stop(state.daemon_id)
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline:
            if not self.matches_process(state):
                return True, "自动回复已停止"
            time.sleep(0.05)
        if not self.matches_process(state):
            return True, "自动回复已停止"
        if os.name != "nt":
            return False, "优雅停止超时；强制停止当前只支持 Windows"
        result = subprocess.run(
            ["taskkill", "/PID", str(state.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            if not self.matches_process(state):
                return True, "自动回复已停止"
            return False, "停止自动回复失败"
        return True, "自动回复已停止（优雅停止超时，已强制结束）"

    def status(self) -> tuple[bool, str]:
        state = self.load_state()
        if state is None or not self.matches_process(state):
            health = self.runtime.load_health()
            if health is not None and health.status == "session_expired":
                return False, "微信会话已失效，请在本机重新执行 login 扫码"
            return True, "自动回复未运行"
        health = self.runtime.load_health()
        if health is None or health.daemon_id != state.daemon_id:
            return False, f"自动回复进程存在但没有有效健康状态（PID {state.pid}）"
        if health.status != "running":
            detail = health.error_type or health.status
            return False, f"自动回复状态异常：{detail}（PID {state.pid}）"
        if health.consecutive_failures > 0:
            if health.error_type == "SessionExpired":
                return False, f"微信会话已失效，需要重新扫码（PID {state.pid}）"
            retry = int(health.retry_in_seconds or 0)
            return False, (
                f"自动回复临时轮询异常，正在重试：{health.error_type or 'UnknownError'}；"
                f"连续失败 {health.consecutive_failures} 次，约 {retry} 秒后重试（PID {state.pid}）"
            )
        heartbeat = health.last_poll_at or health.started_at
        if time.time() - heartbeat > 90:
            return False, f"自动回复进程存在但轮询健康信号已过期（PID {state.pid}）"
        return True, f"自动回复运行正常（PID {state.pid}）{self.inbox_summary()}"

    def inbox_summary(self) -> str:
        try:
            counts = InboxStore(self.session_path.parent / "inbox.json").counts()
        except InboxStateError:
            return "；入站队列状态不可读"
        return (
            f"；入站队列：待处理 {counts['queued']}，处理中 {counts['processing']}，"
            f"已完成 {counts['completed']}，结果不确定 {counts['uncertain']}"
        )

    def restart(self) -> tuple[bool, str]:
        stopped, stop_message = self.stop()
        if not stopped:
            return False, stop_message
        started, start_message = self.start()
        if not started:
            return False, start_message
        return True, start_message.replace("已启动", "已重启", 1)

    def compact(self) -> tuple[bool, str]:
        state = self.load_state()
        if state is None or not self.matches_process(state):
            return False, "自动回复未运行，请先运行：wxbot start"
        request_id = uuid.uuid4().hex
        self.runtime.request_compact(state.daemon_id, request_id)
        deadline = time.monotonic() + self.compact_timeout
        while time.monotonic() < deadline:
            result = self.runtime.load_compaction_result()
            if (
                result is not None
                and result["daemon_id"] == state.daemon_id
                and result["request_id"] == request_id
                and result["status"] in {"success", "failed"}
            ):
                return result["status"] == "success", result["message"]
            if not self.matches_process(state):
                return False, "上下文压缩失败：自动回复进程已退出"
            time.sleep(0.05)
        return False, "上下文压缩超时；自动回复仍在运行，请查询状态后重试"

    def clear_context(self) -> tuple[bool, str]:
        state = self.load_state()
        if state is None or not self.matches_process(state):
            return False, "自动回复未运行，请先运行：wxbot start"
        request_id = uuid.uuid4().hex
        self.runtime.request_clear_context(state.daemon_id, request_id)
        deadline = time.monotonic() + self.compact_timeout
        while time.monotonic() < deadline:
            result = self.runtime.load_clear_context_result()
            if (
                result is not None
                and result["daemon_id"] == state.daemon_id
                and result["request_id"] == request_id
                and result["status"] in {"success", "failed"}
            ):
                return result["status"] == "success", result["message"]
            if not self.matches_process(state):
                return False, "上下文清理失败：自动回复进程已退出"
            time.sleep(0.05)
        return False, "上下文清理超时；自动回复仍在运行，请查询状态后重试"

    def load_state(self) -> DaemonState | None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = DaemonState(
                pid=int(data["pid"]),
                created_at=int(data["created_at"]),
                daemon_id=str(data["daemon_id"]),
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return state if state.pid > 0 and state.created_at > 0 and state.daemon_id else None

    def save_state(self, state: DaemonState) -> None:
        RuntimeFiles._write_json(self.state_path, asdict(state))

    def matches_process(self, state: DaemonState) -> bool:
        return self.process_identity(state.pid) == state.created_at

    @staticmethod
    def process_identity(pid: int) -> int | None:
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except OSError:
                return None
            return pid
        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        creation = FILETIME()
        exit_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        exit_code = DWORD()
        try:
            if not kernel32.GetExitCodeProcess(handle, byref(exit_code)) or exit_code.value != 259:
                return None
            if not kernel32.GetProcessTimes(
                handle, byref(creation), byref(exit_time), byref(kernel_time), byref(user_time)
            ):
                return None
            return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
