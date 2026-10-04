from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wxbot.ai.thread_sessions import ThreadSessionStateError, ThreadSessionStore


class ThreadSessionStoreTests(unittest.TestCase):
    def test_round_trip_multiple_sessions_and_remove_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ThreadSessionStore(Path(directory) / "threads.json")
            store.set("chat", "thread-chat")
            store.set("project:wxbot", "thread-project", client_visible=True)

            self.assertEqual(store.get("chat"), "thread-chat")
            self.assertEqual(store.get("project:wxbot"), "thread-project")
            self.assertTrue(store.is_client_visible("project:wxbot"))
            self.assertFalse(store.is_client_visible("chat"))

            store.remove("chat")
            self.assertIsNone(store.get("chat"))
            self.assertEqual(store.get("project:wxbot"), "thread-project")

    def test_legacy_record_without_visibility_marker_requires_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "threads.json"
            path.write_text(
                '{"version":1,"sessions":{"project:wxbot":'
                '{"thread_id":"thread-old","updated_at":1}}}',
                encoding="utf-8",
            )
            store = ThreadSessionStore(path)

            self.assertEqual(store.get("project:wxbot"), "thread-old")
            self.assertFalse(store.is_client_visible("project:wxbot"))
            store.set("project:wxbot", "thread-new", client_visible=True)
            self.assertTrue(store.is_client_visible("project:wxbot"))

    def test_corrupt_state_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "threads.json"
            path.write_text("{broken", encoding="utf-8")
            store = ThreadSessionStore(path)

            with self.assertRaises(ThreadSessionStateError):
                store.set("chat", "thread-chat")

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_clear_all_removes_every_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ThreadSessionStore(Path(directory) / "threads.json")
            store.set("chat", "thread-chat")
            store.set("project:wxbot", "thread-project", client_visible=True)

            store.clear_all()

            self.assertEqual(store.load(), {})
            self.assertFalse(store.is_client_visible("project:wxbot"))


if __name__ == "__main__":
    unittest.main()
