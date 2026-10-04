from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from wxbot.project.workspace import ProjectWorkspace


class ProjectWorkspaceTests(unittest.TestCase):
    def test_refresh_uses_stable_path_and_excludes_sensitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            (project / ".git").mkdir()
            (project / "README.md").write_text("demo", encoding="utf-8")
            (project / "feature.py").write_text("def feature(): pass", encoding="utf-8")
            (project / "unrelated.py").write_text("UNRELATED = True", encoding="utf-8")
            (project / "binary.bin").write_bytes(b"text\0binary")
            (project / ".env").write_text("TOKEN=secret", encoding="utf-8")
            (project / "data").mkdir()
            (project / "data" / "session.json").write_text("secret", encoding="utf-8")

            workspace = ProjectWorkspace(
                projects_root=root / "projects",
                workspace_root=root / "tmp" / "project-sessions",
                runner=self.runner,
            )
            first = workspace.refresh("demo", "feature")
            second = workspace.refresh("demo", "feature")

            self.assertEqual(first, second)
            self.assertTrue((first / "README.md").exists())
            self.assertTrue((first / "feature.py").exists())
            self.assertTrue((first / "unrelated.py").exists())
            self.assertFalse((first / "binary.bin").exists())
            self.assertFalse((first / ".env").exists())
            self.assertFalse((first / "data" / "session.json").exists())

    def test_refresh_removes_only_files_recorded_by_its_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            (project / ".git").mkdir()
            source = project / "old.py"
            source.write_text("old_token", encoding="utf-8")
            workspace = ProjectWorkspace(
                projects_root=root / "projects",
                workspace_root=root / "tmp" / "project-sessions",
                runner=self.runner,
            )
            target = workspace.refresh("demo", "old_token")
            untracked = target / "untracked.txt"
            untracked.write_text("keep", encoding="utf-8")
            source.unlink()

            workspace.refresh("demo", "new_token")

            self.assertFalse((target / "old.py").exists())
            self.assertTrue(untracked.exists())

    @staticmethod
    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = "## main\n M .env\n M README.md\n" if command[1] == "status" else "abc latest\n"
        return subprocess.CompletedProcess(command, 0, output, "")


if __name__ == "__main__":
    unittest.main()
