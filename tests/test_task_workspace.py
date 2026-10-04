from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from wxbot.project.workspace import TaskWorkspace


class TaskWorkspaceTests(unittest.TestCase):
    def test_creates_non_sensitive_baseline_and_generates_programmatic_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            project = projects / "wxbot"
            project.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (project / "README.md").write_text("old\n", encoding="utf-8")
            (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            tasks = TaskWorkspace(projects_root=projects, task_root=root / "tasks")

            workspace = tasks.create("wxbot", "ABC123")
            (workspace / "README.md").write_text("new\n", encoding="utf-8")
            patch = tasks.diff(workspace)

            self.assertFalse((workspace / ".env").exists())
            self.assertIn("diff --git a/README.md b/README.md", patch)
            self.assertIn("-old", patch)
            self.assertIn("+new", patch)

    def test_rejects_reusing_existing_task_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "wxbot"
            project.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            (project / "README.md").write_text("text\n", encoding="utf-8")
            tasks = TaskWorkspace(projects_root=root / "projects", task_root=root / "tasks")
            tasks.create("wxbot", "ABC123")

            with self.assertRaisesRegex(ValueError, "已存在"):
                tasks.create("wxbot", "ABC123")


if __name__ == "__main__":
    unittest.main()
