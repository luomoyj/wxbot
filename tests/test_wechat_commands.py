from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_auto_reply import FakeClient, FakeGenerator, message
from test_project_status import roadmap
from wxbot.ai.codex_reply import GeneratedReply
from wxbot.ai.session_manager import SessionManager
from wxbot.dispatcher import MessageDispatcher
from wxbot.message.auto_reply import AutoReplyService, AutoReplyStore
from wxbot.message.commands import COMMANDS, command_help, parse_command
from wxbot.message.media import InboundTextFile
from wxbot.project.checkpoints import TaskCheckpointStore
from wxbot.project.status import ProjectStatusReader
from wxbot.project.tasks import TaskStore
from wxbot.replies import AppServerReplyGenerator


class CommandTests(unittest.TestCase):
    def test_help_commands_are_separate_paragraphs_through_send_chain(self):
        help_text = command_help()
        paragraphs = help_text.split("\n\n")
        for usage, description in COMMANDS.values():
            self.assertIn(f"{usage}：{description}", paragraphs)
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            service = AutoReplyService(client=client,
                                       generator=FakeGenerator(GeneratedReply(True, help_text)),
                                       store=AutoReplyStore(Path(directory) / "state.json"))
            service.handle(message(text="/help"))
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0]["text"], help_text)

    def test_every_command_and_alias_has_exact_valid_form(self):
        required = {"resume": " 2", "send": ' "my notes.md"'}
        for name in COMMANDS:
            with self.subTest(name=name):
                parsed = parse_command("/" + name + required.get(name, ""))
                self.assertEqual(parsed.name, name)
                self.assertFalse(parsed.error)
                self.assertIn("/" + name, command_help())
        self.assertEqual(parse_command("/reset").name, "new")
        self.assertEqual(parse_command("/agents").name, "tasks")
        self.assertEqual(parse_command(" /PROJECT wxbot ").args, ("wxbot",))

    def test_invalid_and_unknown_commands_never_fall_back_to_chat(self):
        samples = ["/help extra", "/projects x", "/new confirm", "/reset confirm",
                   "/resume", "/resume 0", "/resume -1", "/resume １",
                   "/sessions show 0", "/sessions search", "/sessions use 2",
                   "/rollback now", "/reset-ai-state yes", "/send", "/send a b",
                   '/send "missing', "/diff --unknown", "/project a b",
                   "/help\n删除文件", "/helpful", "/cancel", "/"]
        generator = AppServerReplyGenerator(sessions=Mock(), projects=Mock())
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(parse_command(sample).error)
                self.assertTrue(generator.generate(sample, []).should_reply)
        generator.sessions.reply.assert_not_called()
        generator.projects.active_project.assert_not_called()
        self.assertIsNone(parse_command("解释一下 /reset 的作用"))

    def test_quoted_paths_and_subcommands(self):
        samples = {'/send "目录/my notes.md"': ("目录/my notes.md",),
                   '/send "docs\\my notes.md"': ("docs\\my notes.md",),
                   "/sessions search my project": ("search", "my", "project"),
                   "/sessions show 2": ("show", "2"),
                   "/diff --stat": ("--stat",),
                   "/rollback confirm": ("confirm",)}
        for sample, args in samples.items():
            self.assertEqual(parse_command(sample).args, args)
            self.assertFalse(parse_command(sample).error)

    def test_register_project_command_routes_without_switch_or_model(self):
        text = '/project add "F:\\my projects\\demo"'
        command = parse_command(text)
        self.assertEqual(command.args, ("add", "F:\\my projects\\demo"))
        self.assertFalse(command.error)
        projects = Mock()
        projects.add_project.return_value = "demo"
        generator = AppServerReplyGenerator(sessions=Mock(), projects=projects)
        self.assertIn("项目已登记", generator.generate(text, []).reply)
        projects.add_project.assert_called_once_with("F:\\my projects\\demo")
        projects.select_project.assert_not_called()
        generator.sessions.reply.assert_not_called()
        self.assertFalse(generator.is_immediate_control(text))
        self.assertTrue(parse_command("/project add a b").error)
        self.assertFalse(parse_command('/project "my 项目"').error)

    def test_slashes_are_merge_boundaries_but_project_changes_stay_serial(self):
        dispatcher = object.__new__(MessageDispatcher)
        dispatcher.generator = AppServerReplyGenerator(sessions=Mock(), projects=Mock())
        for sample in ("/help", "/project wxbot", "/chat", "/unknown"):
            self.assertTrue(dispatcher._is_text_merge_boundary(sample))
            self.assertFalse(dispatcher._attach_text(message(text=sample), ()))
        self.assertFalse(dispatcher.generator.is_immediate_control("/project wxbot"))
        self.assertFalse(dispatcher.generator.is_immediate_control("/chat"))
        self.assertTrue(dispatcher.generator.is_immediate_control("/stop"))

    def test_current_reset_and_all_state_reset_have_different_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            generator = FakeGenerator(GeneratedReply(True, "unused"))
            generator.clear_context = Mock()
            generator.reset_all_ai_state = Mock()
            store = AutoReplyStore(Path(directory) / "state.json")
            service = AutoReplyService(client=FakeClient(), generator=generator, store=store)
            for index, sample in enumerate(("/new", "/reset"), 1):
                self.assertEqual(service.handle(message(message_id=index, text=sample)), "history-cleared")
            self.assertEqual(generator.clear_context.call_count, 2)
            generator.reset_all_ai_state.assert_not_called()
            self.assertEqual(service.handle(message(message_id=3, text="/reset-ai-state confirm")), "reset-not-confirmed")
            self.assertEqual(service.handle(message(message_id=4, text="/reset-ai-state")), "reset-previewed")
            generator.reset_all_ai_state.assert_not_called()
            self.assertEqual(service.handle(message(message_id=5, text="/reset-ai-state confirm")), "ai-state-reset")
            generator.reset_all_ai_state.assert_called_once()
            self.assertEqual(generator.messages, [])

    def test_expired_reset_preview_and_invalid_commands_do_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            generator = Mock()
            service = AutoReplyService(client=FakeClient(), generator=generator,
                                       store=AutoReplyStore(Path(directory) / "state.json"))
            service.handle(message(text="/reset-ai-state"))
            with patch("wxbot.message.auto_reply.time.monotonic", return_value=10**12):
                self.assertEqual(service.handle(message(message_id=2, text="/reset-ai-state confirm")), "reset-not-confirmed")
            for index, text in enumerate(("/unknown", "/reset confirm", "/stop extra"), 3):
                self.assertEqual(service.handle(message(message_id=index, text=text)), "command-error")
            generator.reset_all_ai_state.assert_not_called()
            generator.generate.assert_not_called()

    def test_thread_runtime_and_context_commands_use_existing_controls(self):
        sessions, projects = Mock(), Mock()
        projects.active_project.return_value = None
        sessions.thread_control.return_value = "thread-result"
        sessions.runtime_status.return_value = "runtime-result"
        generator = AppServerReplyGenerator(sessions=sessions, projects=projects)
        cases = {"/sessions": "有哪些会话", "/sessions search my project": "搜索会话 my project",
                 "/sessions show 2": "查看第2个会话", "/resume 2": "切换到第2个会话",
                 "/status": "当前会话信息"}
        for command, request in cases.items():
            self.assertEqual(generator.generate(command, []).reply, "thread-result")
            sessions.thread_control.assert_called_with(request)
        for command in ("/model", "/usage", "/context"):
            self.assertEqual(generator.generate(command, []).reply, "runtime-result")
            self.assertTrue(SessionManager.is_runtime_query(sessions.runtime_status.call_args.args[0]))
        generator.generate("/compress", [])
        sessions.compact_current.assert_called_once()
        sessions.reply.assert_not_called()

    def test_tasks_and_stop_preserve_queue_priority_without_calling_model(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            store.create(task_id="ABC123", project="wxbot", request="fictional running")
            store.update("ABC123", status="running")
            store.create(task_id="ABC124", project="wxbot", request="fictional queued")
            projects = Mock()
            projects.active_project.return_value = "wxbot"
            generator = AppServerReplyGenerator(sessions=Mock(), projects=projects)
            worker = Mock(store=store)
            worker.cancel.side_effect = lambda task_id: store.update(task_id, status="cancelled")
            generator.task_worker = worker
            for command in ("/tasks", "/agents"):
                self.assertIn("等待队列", generator.generate(command, []).reply)
            self.assertIn("等待队列", generator.generate("/task", []).reply)
            generator.generate("/stop", [])
            worker.cancel.assert_called_with("ABC124")
            self.assertEqual(store.get("ABC123").status, "running")
            generator.generate("/stop", [])
            worker.cancel.assert_called_with("ABC123")
            store.update("ABC124", status="failed", error="fictional failure")
            self.assertIn("fictional failure", generator.generate("/error", []).reply)
            generator.sessions.reply.assert_not_called()

    def test_project_commands_and_progress_use_roadmap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ROADMAP.md").write_text(roadmap(), encoding="utf-8")
            projects = Mock()
            projects.active_project.return_value = "wxbot"
            projects.project_path.return_value = root
            projects.list_projects.return_value = ["wxbot", "other"]
            sessions = Mock()
            generator = AppServerReplyGenerator(sessions=sessions, projects=projects)
            self.assertIn("* wxbot", generator.generate("/projects", []).reply)
            generator.generate("/project other", [])
            projects.select_project.assert_called_once_with("other")
            self.assertIn("wxbot", generator.generate("/project", []).reply)
            generator.generate("/chat", [])
            projects.auto_reply_store.set_chat_mode.assert_called_once()
            self.assertIn("验收微信会话列表", generator.generate("/next", []).reply)
            self.assertIn("当前阶段", generator.generate("/progress", []).reply)
            sessions.reply.assert_not_called()

    def test_file_send_uses_quoted_path_and_existing_safety_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "my notes.md"
            target.write_text("ordinary content", encoding="utf-8")
            projects = Mock()
            projects.active_project.return_value = "wxbot"
            projects.project_path.return_value = root
            generator = AppServerReplyGenerator(sessions=Mock(), projects=projects)
            result = generator.generate('/send "my notes.md"', [])
            self.assertEqual(result.outbound_file, target)
            for command in ("/send ../outside.md", "/send .env", "/send program.exe"):
                self.assertIsNone(generator.generate(command, []).outbound_file)

    def test_attachment_command_text_is_data_and_raw_caption_is_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.txt"
            path.write_text("/reset-ai-state confirm", encoding="utf-8")
            attachment = InboundTextFile(path, "notes.txt")
            sessions = Mock()
            sessions.reply.return_value = "read"
            generator = AppServerReplyGenerator(sessions=sessions, projects=Mock())
            generator.generate_message(message(text="读取附件"), local_file=attachment)
            sessions.reply.assert_called_once_with("读取附件", attachment_name="notes.txt",
                                                   attachment_text="/reset-ai-state confirm")
            generator.generate_message(message(text="/help"), local_file=attachment)
            self.assertEqual(sessions.reply.call_count, 1)
            sessions.clear_all.assert_not_called()

    def test_rollback_confirms_previewed_checkpoint_even_if_newer_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            a, b = root / "a.md", root / "b.md"
            a.write_text("before", encoding="utf-8")
            b.write_text("before", encoding="utf-8")
            checkpoints = TaskCheckpointStore(Path(directory) / "checkpoints")
            checkpoints.begin("ABC123", "wxbot", root)
            a.write_text("after", encoding="utf-8")
            checkpoints.finish("ABC123")
            projects = Mock()
            projects.active_project.return_value = "wxbot"
            generator = AppServerReplyGenerator(sessions=Mock(), projects=projects, checkpoints=checkpoints)
            self.assertIn("请先", generator.generate("/rollback confirm", []).reply)
            self.assertIn("a.md", generator.generate("/diff --stat", []).reply)
            self.assertIn("before", generator.generate("/diff", []).reply)
            self.assertIn("after", generator.generate("/diff a.md", []).reply)
            self.assertIn("/rollback confirm", generator.generate("/rollback latest", []).reply)
            checkpoints.begin("ABC124", "wxbot", root)
            b.write_text("newer", encoding="utf-8")
            checkpoints.finish("ABC124")
            self.assertIn("恢复完成", generator.generate("/rollback confirm", []).reply)
            self.assertEqual(a.read_text(), "before")
            self.assertEqual(b.read_text(), "newer")

    def test_rollback_refuses_conflicting_later_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            target = root / "a.md"
            target.write_text("before")
            checkpoints = TaskCheckpointStore(Path(directory) / "checkpoints")
            checkpoints.begin("ABC123", "wxbot", root)
            target.write_text("after")
            checkpoints.finish("ABC123")
            projects = Mock()
            projects.active_project.return_value = "wxbot"
            generator = AppServerReplyGenerator(sessions=Mock(), projects=projects, checkpoints=checkpoints)
            generator.generate("/rollback latest", [])
            target.write_text("later")
            self.assertIn("冲突", generator.generate("/rollback confirm", []).reply)
            self.assertEqual(target.read_text(), "later")

    def test_accept_only_latest_pending_task_and_preserve_main_roadmap_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ROADMAP.md"
            path.write_text(roadmap(), encoding="utf-8")
            store = TaskStore(root / "tasks.json")
            for task_id in ("ABC123", "ABC124"):
                store.create(task_id=task_id, project="wxbot", request="fictional")
                store.update(task_id, status="completed", acceptance_status="pending")
            projects = Mock()
            projects.active_project.return_value = "wxbot"
            projects.project_path.return_value = root
            generator = AppServerReplyGenerator(sessions=Mock(), projects=projects)
            generator.task_worker = SimpleNamespace(store=store)
            generator.generate("/accept", [])
            self.assertEqual(store.get("ABC124").acceptance_status, "accepted")
            self.assertEqual(store.get("ABC123").acceptance_status, "pending")
            self.assertIn("ABC124", path.read_text(encoding="utf-8"))
            self.assertEqual(ProjectStatusReader(path).read().main_task_status.rstrip("。"), "待验收")

    def test_acceptance_record_stays_under_its_own_heading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(roadmap() + "\n## 微信任务验收记录\n\n## 后续任务\n", encoding="utf-8")
            reader = ProjectStatusReader(path)
            reader.record_acceptance("ABC123")
            reader.record_acceptance("ABC123")
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("ABC123"), 1)
            self.assertLess(text.index("ABC123"), text.index("## 后续任务"))
