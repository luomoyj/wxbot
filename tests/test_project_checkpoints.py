from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wxbot.project.checkpoints import CheckpointError, TaskCheckpointStore


class TaskCheckpointStoreTests(unittest.TestCase):
    def test_records_task_changes_and_formats_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "README.md").write_text("旧说明\n", encoding="utf-8")
            store = TaskCheckpointStore(Path(directory) / "checkpoints")

            store.begin("ABC123", "demo", root)
            (root / "README.md").write_text("新说明\n", encoding="utf-8")
            (root / "new.txt").write_text("新增\n", encoding="utf-8")
            store.finish("ABC123")

            summary = store.summary("ABC123")
            detail = store.file_detail("ABC123", "README.md")
            raw = store.raw_diff("ABC123")
            self.assertIn("涉及 2 个文件", summary)
            self.assertIn("README.md：修改", summary)
            self.assertIn("改前", detail)
            self.assertIn("旧说明", detail)
            self.assertIn("新说明", detail)
            self.assertNotIn("+ 新说明", detail)
            self.assertIn("--- a/README.md", raw)
            self.assertIn("ABC123", store.list_available("demo"))

    def test_file_detail_labels_added_content_and_hides_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            target = root / "README.md"
            target.write_text("已有内容\n", encoding="utf-8")
            store = TaskCheckpointStore(Path(directory) / "checkpoints")
            store.begin("ABC123", "demo", root)
            target.write_text("已有内容\ncheckpoint 查看验收\n\n", encoding="utf-8")
            store.finish("ABC123")

            detail = store.file_detail("ABC123", "README.md")

            self.assertIn("新增内容：\ncheckpoint 查看验收", detail)
            self.assertNotIn("改后", detail)
            self.assertNotIn("+ checkpoint", detail)

    def test_restore_requires_preview_and_restores_exact_task_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "keep.txt").write_text("before", encoding="utf-8")
            (root / "deleted.txt").write_text("restore me", encoding="utf-8")
            store = TaskCheckpointStore(Path(directory) / "checkpoints")
            store.begin("ABC123", "demo", root)
            (root / "keep.txt").write_text("after", encoding="utf-8")
            (root / "deleted.txt").unlink()
            (root / "added.txt").write_text("remove me", encoding="utf-8")
            store.finish("ABC123")

            with self.assertRaisesRegex(CheckpointError, "请先发送"):
                store.restore("ABC123")
            preview = store.prepare_restore("ABC123")
            self.assertIn("确认恢复刚才的修改", preview)
            result = store.restore("ABC123")

            self.assertIn("已直接恢复", result)
            self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "before")
            self.assertEqual((root / "deleted.txt").read_text(encoding="utf-8"), "restore me")
            self.assertFalse((root / "added.txt").exists())

    def test_restore_skips_file_when_task_change_cannot_be_located(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            target = root / "README.md"
            target.write_text("before", encoding="utf-8")
            store = TaskCheckpointStore(Path(directory) / "checkpoints")
            store.begin("ABC123", "demo", root)
            target.write_text("task result", encoding="utf-8")
            store.finish("ABC123")
            target.write_text("later edit", encoding="utf-8")

            preview = store.prepare_restore("ABC123")
            result = store.restore("ABC123")

            self.assertIn("存在冲突，将跳过", preview)
            self.assertIn("没有文件被修改", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "later edit")

    def test_restore_merges_non_overlapping_later_changes_and_restores_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            readme = root / "README.md"
            roadmap = root / "ROADMAP.md"
            readme.write_text("说明\n", encoding="utf-8")
            roadmap.write_text("已完成：\n", encoding="utf-8")
            store = TaskCheckpointStore(Path(directory) / "checkpoints")
            store.begin("ABC123", "demo", root)
            readme.write_text("说明\ncheckpoint 验收\n", encoding="utf-8")
            roadmap.write_text("已完成：\n- checkpoint 验收\n", encoding="utf-8")
            store.finish("ABC123")
            roadmap.write_text(
                "已完成：\n- checkpoint 验收\n- 后续展示优化\n", encoding="utf-8"
            )

            preview = store.prepare_restore("ABC123")
            result = store.restore("ABC123")

            self.assertIn("README.md：可直接恢复", preview)
            self.assertIn("ROADMAP.md：可撤销本任务内容并保留后续修改", preview)
            self.assertIn("已直接恢复：README.md", result)
            self.assertIn("已合并恢复：ROADMAP.md", result)
            self.assertEqual(readme.read_text(encoding="utf-8"), "说明\n")
            self.assertEqual(
                roadmap.read_text(encoding="utf-8"), "已完成：\n- 后续展示优化\n"
            )

    def test_skips_sensitive_and_runtime_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / ".env").write_text("SECRET=value", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "session.json").write_text("token", encoding="utf-8")
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            store = TaskCheckpointStore(Path(directory) / "checkpoints")

            store.begin("ABC123", "demo", root)

            checkpoint = Path(directory) / "checkpoints" / "ABC123" / "before"
            self.assertTrue((checkpoint / "safe.txt").exists())
            self.assertFalse((checkpoint / ".env").exists())
            self.assertFalse((checkpoint / "data" / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
