from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from wxbot.project.commands import SafeCommandRunner


class SafeCommandRunnerTests(unittest.TestCase):
    def test_selects_existing_python_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "tests").mkdir()

            command = SafeCommandRunner().select(project, "运行测试")

            self.assertIsNotNone(command)
            self.assertEqual(command[1:], ["-m", "unittest", "discover", "-s", "tests", "-v"])

    def test_latest_is_not_mistaken_for_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "tests").mkdir()
            self.assertIsNone(SafeCommandRunner().select(project, "show latest progress"))

    def test_local_commit_request_is_not_mistaken_for_git_diff_query(self) -> None:
        request = (
            "把当前修改创建为本地 Git 提交，"
            "提交信息为 test: 验收确认流程"
        )

        self.assertIsNone(SafeCommandRunner().select(Path.cwd(), request))
        self.assertEqual(
            SafeCommandRunner().select(Path.cwd(), "查看 Git 修改"),
            ["git", "diff", "--stat"],
        )

    def test_selects_only_existing_package_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "package.json").write_text(
                json.dumps({"scripts": {"lint": "eslint ."}}), encoding="utf-8"
            )

            self.assertEqual(SafeCommandRunner().select(project, "运行 lint")[-2:], ["run", "lint"])
            self.assertIsNone(SafeCommandRunner().select(project, "运行构建"))

    def test_run_returns_bounded_output(self) -> None:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "x" * 3000, "")

        result = SafeCommandRunner(runner).run(Path.cwd(), ["git", "status"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.output), 2400)
        self.assertGreaterEqual(result.elapsed_seconds, 0)


if __name__ == "__main__":
    unittest.main()
