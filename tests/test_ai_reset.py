from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wxbot.ai.thread_sessions import ThreadSessionStore
from wxbot.cli import reset_ai_state_command
from wxbot.message.auto_reply import AutoReplyState, AutoReplyStore
from wxbot.storage.session_store import SessionStore


class AiResetCommandTests(unittest.TestCase):
    def test_preview_does_not_change_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auto_path = root / "auto_reply.json"
            store = AutoReplyStore(auto_path)
            store.save(AutoReplyState(allowed_user_id="owner"))
            before = auto_path.read_bytes()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = reset_ai_state_command(
                    confirmed=False,
                    store=SessionStore(root / "session.json"),
                )

            self.assertEqual(result, 0)
            self.assertEqual(auto_path.read_bytes(), before)
            self.assertIn("将清空", output.getvalue())
            self.assertIn("将保留", output.getvalue())

    def test_confirm_clears_only_declared_ai_state_when_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            session_path = data / "session.json"
            session_path.write_text('{"login":"preserved"}', encoding="utf-8")
            auto_path = data / "auto_reply.json"
            thread_path = data / "thread_sessions.json"
            AutoReplyStore(auto_path).save(AutoReplyState(allowed_user_id="owner"))
            ThreadSessionStore(thread_path).set("chat", "thread-chat")
            checkpoint = data / "checkpoints" / "task.json"
            checkpoint.parent.mkdir()
            checkpoint.write_text("preserved", encoding="utf-8")
            tasks = data / "tasks.json"
            tasks.write_text(json.dumps({"tasks": []}), encoding="utf-8")

            output = io.StringIO()
            with patch("wxbot.cli.DEFAULT_AUTO_REPLY_PATH", auto_path), patch(
                "wxbot.cli.DEFAULT_THREAD_SESSIONS_PATH", thread_path
            ), contextlib.redirect_stdout(output):
                result = reset_ai_state_command(
                    confirmed=True,
                    store=SessionStore(session_path),
                )

            self.assertEqual(result, 0)
            self.assertIsNone(AutoReplyStore(auto_path).load().allowed_user_id)
            self.assertEqual(ThreadSessionStore(thread_path).load(), {})
            self.assertEqual(session_path.read_text(encoding="utf-8"), '{"login":"preserved"}')
            self.assertEqual(checkpoint.read_text(encoding="utf-8"), "preserved")
            self.assertTrue(tasks.exists())
            self.assertIn("全部本地 AI 状态已清空", output.getvalue())


if __name__ == "__main__":
    unittest.main()
