from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wxbot.daemon import DaemonController, DaemonState, RuntimeFiles, RuntimeHealth
from wxbot.message.inbox import InboxStore
from wxbot.api.models import MessageItem, WeixinMessage


class DaemonControllerTests(unittest.TestCase):
    def controller(self, directory: str, **kwargs: float) -> DaemonController:
        root = Path(directory)
        return DaemonController(
            project_root=root,
            session_path=root / "data" / "session.json",
            **kwargs,
        )

    def test_start_requires_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            success, message = self.controller(directory).start()
            self.assertFalse(success)
            self.assertIn("尚未登录", message)

    def test_runtime_health_round_trip_keeps_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeFiles(Path(directory))
            expected = RuntimeHealth(
                "daemon", "running", 100.0, 120.0, "HTTP 503", 4, 16,
            )
            runtime.write_health(expected)
            self.assertEqual(runtime.load_health(), expected)

    def test_runtime_compaction_request_and_result_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeFiles(Path(directory))
            runtime.request_compact("daemon", "request-1")
            self.assertEqual(runtime.compact_request("daemon"), "request-1")
            self.assertEqual(runtime.load_compaction_result(), {
                "daemon_id": "daemon",
                "request_id": "request-1",
                "status": "pending",
                "message": "",
            })
            runtime.write_compaction_result(
                "daemon", "request-1", success=True, message="压缩完成",
            )
            self.assertEqual(runtime.load_compaction_result(), {
                "daemon_id": "daemon",
                "request_id": "request-1",
                "status": "success",
                "message": "压缩完成",
            })

    def test_runtime_context_clear_request_and_result_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeFiles(Path(directory))
            runtime.request_clear_context("daemon", "request-1")
            self.assertEqual(runtime.clear_context_request("daemon"), "request-1")
            self.assertEqual(runtime.load_clear_context_result(), {
                "daemon_id": "daemon",
                "request_id": "request-1",
                "status": "pending",
                "message": "",
            })
            runtime.write_clear_context_result(
                "daemon", "request-1", success=True, message="上下文已清空",
            )
            self.assertEqual(runtime.load_clear_context_result(), {
                "daemon_id": "daemon",
                "request_id": "request-1",
                "status": "success",
                "message": "上下文已清空",
            })

    def test_start_waits_for_matching_health_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.session_path.parent.mkdir(parents=True)
            controller.session_path.write_text("{}", encoding="utf-8")
            process = Mock(pid=2468)
            health = RuntimeHealth("daemon", "running", time.time(), time.time())
            with patch("wxbot.daemon.uuid.uuid4", return_value=Mock(hex="daemon")), patch(
                "wxbot.daemon.subprocess.Popen", return_value=process
            ) as popen, patch.object(controller, "process_identity", return_value=777), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.start()
            self.assertTrue(success)
            self.assertIn("2468", message)
            self.assertEqual(controller.load_state(), DaemonState(2468, 777, "daemon"))
            self.assertIn("--daemon-id", " ".join(popen.call_args.args[0]))
            self.assertIn("_worker", popen.call_args.args[0])
            self.assertNotIn("auto-start", popen.call_args.args[0])
            self.assertNotIn("--ai-runtime", " ".join(popen.call_args.args[0]))
            self.assertEqual(popen.call_args.kwargs["stdout"], -3)

    def test_start_reports_child_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.session_path.parent.mkdir(parents=True)
            controller.session_path.write_text("{}", encoding="utf-8")
            health = RuntimeHealth("daemon", "error", time.time(), error_type="CodexNotFound")
            with patch("wxbot.daemon.uuid.uuid4", return_value=Mock(hex="daemon")), patch(
                "wxbot.daemon.subprocess.Popen", return_value=Mock(pid=2468)
            ), patch.object(controller, "process_identity", return_value=777), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.start()
            self.assertFalse(success)
            self.assertIn("安装 Codex CLI", message)
            self.assertIn("完成登录", message)

    def test_start_reports_codex_login_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.session_path.parent.mkdir(parents=True)
            controller.session_path.write_text("{}", encoding="utf-8")
            health = RuntimeHealth("daemon", "error", time.time(), error_type="CodexNotLoggedIn")
            with patch("wxbot.daemon.uuid.uuid4", return_value=Mock(hex="daemon")), patch(
                "wxbot.daemon.subprocess.Popen", return_value=Mock(pid=2468)
            ), patch.object(controller, "process_identity", return_value=777), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.start()
            self.assertFalse(success)
            self.assertIn("codex login", message)
            self.assertIn("重新启动", message)

    def test_start_does_not_duplicate_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            state = DaemonState(1357, 888, "existing")
            controller.save_state(state)
            health = RuntimeHealth("existing", "running", time.time(), time.time())
            with patch.object(controller, "matches_process", return_value=True), patch.object(
                controller.runtime, "load_health", return_value=health
            ), patch("wxbot.daemon.subprocess.Popen") as popen:
                success, message = controller.start()
            self.assertTrue(success)
            self.assertIn("运行正常", message)
            popen.assert_not_called()

    def test_status_reports_only_inbox_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(1357, 888, "daemon"))
            health = RuntimeHealth("daemon", "running", time.time(), time.time())
            inbox = InboxStore(Path(directory) / "data" / "inbox.json")
            inbox.enqueue(WeixinMessage(
                from_user_id="wxid_private_owner", to_user_id="bot",
                context_token="private-context-token", message_type=1,
                message_id=1, client_id="client-1", create_time_ms=1,
                items=(MessageItem(type=1, text="private message body"),),
            ))
            with patch.object(controller, "matches_process", return_value=True), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.status()
            self.assertTrue(success)
            self.assertIn("待处理 1", message)
            self.assertNotIn("wxid_private_owner", message)
            self.assertNotIn("private-context-token", message)
            self.assertNotIn("private message body", message)

    def test_stop_requests_graceful_exit_before_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(9753, 999, "daemon"))
            with patch.object(controller, "matches_process", side_effect=[True, False]), patch.object(
                controller.runtime, "request_stop"
            ) as request_stop, patch("wxbot.daemon.subprocess.run") as run:
                success, message = controller.stop()
            self.assertTrue(success)
            self.assertEqual(message, "自动回复已停止")
            request_stop.assert_called_once_with("daemon")
            run.assert_not_called()

    def test_stop_forces_only_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, stop_timeout=0)
            controller.save_state(DaemonState(9753, 999, "daemon"))
            completed = Mock(returncode=0)
            with patch.object(controller, "matches_process", return_value=True), patch(
                "wxbot.daemon.os.name", "nt"
            ), patch("wxbot.daemon.subprocess.run", return_value=completed) as run:
                success, message = controller.stop()
            self.assertTrue(success)
            self.assertIn("强制结束", message)
            self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "9753", "/T", "/F"])

    def test_stop_accepts_process_exit_race_after_taskkill_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, stop_timeout=0)
            controller.save_state(DaemonState(9753, 999, "daemon"))
            completed = Mock(returncode=128)
            with patch.object(
                controller, "matches_process", side_effect=[True, True, False]
            ), patch("wxbot.daemon.os.name", "nt"), patch(
                "wxbot.daemon.subprocess.run", return_value=completed
            ):
                success, message = controller.stop()
            self.assertTrue(success)
            self.assertEqual(message, "自动回复已停止")

    def test_pid_identity_mismatch_is_not_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(9753, 111, "old"))
            with patch.object(controller, "process_identity", return_value=222), patch(
                "wxbot.daemon.subprocess.run"
            ) as run:
                success, message = controller.stop()
            self.assertTrue(success)
            self.assertEqual(message, "自动回复未运行")
            run.assert_not_called()

    def test_status_rejects_stale_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(1357, 888, "daemon"))
            health = RuntimeHealth("daemon", "running", time.time() - 100, time.time() - 100)
            with patch.object(controller, "matches_process", return_value=True), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.status()
            self.assertFalse(success)
            self.assertIn("已过期", message)

    def test_status_reports_transient_poll_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(1357, 888, "daemon"))
            health = RuntimeHealth(
                "daemon", "running", time.time(), time.time(),
                "NetworkError", 3, 8,
            )
            with patch.object(controller, "matches_process", return_value=True), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.status()
            self.assertFalse(success)
            self.assertIn("临时轮询异常", message)
            self.assertIn("NetworkError", message)
            self.assertIn("连续失败 3 次", message)
            self.assertIn("8 秒后重试", message)

    def test_status_reports_expired_wechat_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(1357, 888, "daemon"))
            health = RuntimeHealth(
                "daemon", "running", time.time(), time.time(),
                "SessionExpired", 1, 0,
            )
            with patch.object(controller, "matches_process", return_value=True), patch.object(
                controller.runtime, "load_health", return_value=health
            ):
                success, message = controller.status()
            self.assertFalse(success)
            self.assertIn("会话已失效", message)
            self.assertIn("重新扫码", message)

    def test_status_keeps_expired_session_after_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            controller.save_state(DaemonState(1357, 888, "daemon"))
            controller.runtime.write_health(RuntimeHealth(
                "daemon", "session_expired", time.time(), time.time(),
                "SessionExpired", 1, 0,
            ))
            with patch.object(controller, "matches_process", return_value=False):
                success, message = controller.status()
            self.assertFalse(success)
            self.assertIn("会话已失效", message)
            self.assertIn("本机", message)
            self.assertIn("login", message)

    def test_restart_stops_then_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            with patch.object(controller, "stop", return_value=(True, "自动回复已停止")), patch.object(
                controller, "start", return_value=(True, "自动回复已启动（PID 8642）")
            ):
                success, message = controller.restart()
            self.assertTrue(success)
            self.assertEqual(message, "自动回复已重启（PID 8642）")

    def test_restart_does_not_start_when_stop_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            with patch.object(controller, "stop", return_value=(False, "停止自动回复失败")), patch.object(
                controller, "start"
            ) as start:
                success, message = controller.restart()
            self.assertFalse(success)
            self.assertEqual(message, "停止自动回复失败")
            start.assert_not_called()

    def test_compact_routes_request_to_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, compact_timeout=1)
            controller.save_state(DaemonState(9753, 999, "daemon"))
            result = {
                "daemon_id": "daemon",
                "request_id": "request-1",
                "status": "success",
                "message": "当前对话上下文已压缩",
            }
            with patch.object(controller, "matches_process", return_value=True), patch(
                "wxbot.daemon.uuid.uuid4", return_value=Mock(hex="request-1")
            ), patch.object(controller.runtime, "request_compact") as request_compact, patch.object(
                controller.runtime, "load_compaction_result", return_value=result
            ):
                success, message = controller.compact()
            self.assertTrue(success)
            self.assertEqual(message, "当前对话上下文已压缩")
            request_compact.assert_called_once_with("daemon", "request-1")

    def test_compact_requires_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            with patch.object(controller, "matches_process", return_value=False):
                success, message = controller.compact()
            self.assertFalse(success)
            self.assertIn("自动回复未运行", message)

    def test_clear_context_routes_request_to_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, compact_timeout=1)
            controller.save_state(DaemonState(9753, 999, "daemon"))
            result = {
                "daemon_id": "daemon",
                "request_id": "request-1",
                "status": "success",
                "message": "当前对话上下文已清空",
            }
            with patch.object(controller, "matches_process", return_value=True), patch(
                "wxbot.daemon.uuid.uuid4", return_value=Mock(hex="request-1")
            ), patch.object(
                controller.runtime, "request_clear_context"
            ) as request_clear, patch.object(
                controller.runtime, "load_clear_context_result", return_value=result
            ):
                success, message = controller.clear_context()
            self.assertTrue(success)
            self.assertEqual(message, "当前对话上下文已清空")
            request_clear.assert_called_once_with("daemon", "request-1")

    def test_clear_context_requires_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            with patch.object(controller, "matches_process", return_value=False):
                success, message = controller.clear_context()
            self.assertFalse(success)
            self.assertIn("自动回复未运行", message)


if __name__ == "__main__":
    unittest.main()
