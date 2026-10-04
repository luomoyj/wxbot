from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from wxbot import __version__
from wxbot.cli import build_parser, doctor_command, setup_command


class CliTests(unittest.TestCase):
    def test_version_uses_package_version(self) -> None:
        output = io.StringIO()
        parser = build_parser()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            parser.parse_args(["--version"])

        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"wxbot {__version__}")

    def test_parser_exposes_user_facing_commands_and_nested_context(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["start"]).command, "start")
        self.assertEqual(parser.parse_args(["run"]).command, "run")
        context = parser.parse_args(["context", "compact"])
        self.assertEqual(context.command, "context")
        self.assertEqual(context.context_command, "compact")
        project = parser.parse_args(["project", "status", "wxbot"])
        self.assertEqual(project.command, "project")
        self.assertEqual(project.project_command, "status")
        self.assertEqual(project.project, "wxbot")

    def test_help_describes_public_commands_without_internal_worker(self) -> None:
        output = io.StringIO()
        parser = build_parser()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            parser.parse_args(["--help"])

        self.assertEqual(caught.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("首次设置并启动", help_text)
        self.assertIn("管理当前对话上下文", help_text)
        self.assertIn("检查 Codex CLI和 App Server兼容性", help_text)
        self.assertNotIn("auto-start", help_text)
        self.assertNotIn("daemon-start", help_text)

    def test_setup_requires_codex_before_wechat_login(self) -> None:
        output = io.StringIO()
        with patch("wxbot.cli.find_codex_executable", return_value=None), contextlib.redirect_stderr(output):
            result = setup_command(object())  # type: ignore[arg-type]

        self.assertEqual(result, 1)
        self.assertIn("未找到 Codex CLI", output.getvalue())

    def test_setup_starts_service_when_wechat_session_exists(self) -> None:
        class ExistingStore:
            def load(self) -> object:
                return object()

        store = ExistingStore()
        with patch("wxbot.cli.find_codex_executable", return_value="codex.cmd"), patch(
            "wxbot.cli.service_command", return_value=0,
        ) as service:
            result = setup_command(store)  # type: ignore[arg-type]

        self.assertEqual(result, 0)
        service.assert_called_once_with("start", store)

    def test_setup_logs_in_before_starting_when_session_is_missing(self) -> None:
        class MissingStore:
            def load(self) -> None:
                return None

        store = MissingStore()
        with patch("wxbot.cli.find_codex_executable", return_value="codex.cmd"), patch(
            "wxbot.cli.login_command", return_value=0,
        ) as login, patch("wxbot.cli.service_command", return_value=0) as service:
            result = setup_command(store)  # type: ignore[arg-type]

        self.assertEqual(result, 0)
        login.assert_called_once_with(store)
        service.assert_called_once_with("start", store)

    def test_legacy_daemon_command_is_not_public(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["daemon-status"])

        self.assertEqual(caught.exception.code, 2)

    def test_doctor_runs_critical_app_server_operations(self) -> None:
        calls: list[str] = []

        class FakeClient:
            def __init__(self, *, executable: str) -> None:
                self.executable = executable

            def start(self) -> None:
                calls.append("initialize")

            def start_thread(self, **kwargs: object) -> str:
                calls.append("thread/start")
                if kwargs.get("name"):
                    self.set_thread_name("thread-1", str(kwargs["name"]))
                return "thread-1"

            def set_thread_name(self, _thread_id: str, _name: str) -> None:
                calls.append("thread/name/set")

            def resume_thread(self, **_kwargs: object) -> str:
                calls.append("thread/resume")
                return "thread-1"

            def list_threads(self, **_kwargs: object) -> list[object]:
                calls.append("thread/list")
                return []

            def read_thread(self, _thread_id: str) -> object:
                calls.append("thread/read")
                return object()

            def turn(self, **_kwargs: object) -> str:
                calls.append("turn/start")
                return "OK"

            def compact_thread(self, **_kwargs: object) -> None:
                calls.append("thread/compact/start")

            def close(self) -> None:
                calls.append("close")

        output = io.StringIO()
        with (
            patch("wxbot.cli.find_codex_executable", return_value="codex.cmd"),
            patch("wxbot.cli.AppServerClient", FakeClient),
            contextlib.redirect_stdout(output),
        ):
            result = doctor_command()

        self.assertEqual(result, 0)
        self.assertEqual(calls, [
            "initialize", "thread/list", "thread/start", "thread/name/set",
            "thread/start", "thread/resume", "thread/read", "turn/start",
            "thread/compact/start", "close",
        ])
        self.assertEqual(output.getvalue().strip(), "Codex CLI与 App Server兼容检查通过")
