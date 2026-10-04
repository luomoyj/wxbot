from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from wxbot.message.auto_reply import AutoReplyState, AutoReplyStore
from wxbot.project.control import Approval, ProjectControlError, ProjectController
from wxbot.project.workspace import TaskWorkspace


class ProjectControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.data = self.root / "data"
        self.projects.mkdir()
        self.project = self.projects / "demo"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.store = AutoReplyStore(self.data / "auto.json")
        self.store.save(AutoReplyState(allowed_user_id="owner"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def controller(self, runner=subprocess.run) -> ProjectController:
        return ProjectController(
            projects_root=self.projects,
            data_dir=self.data,
            auto_reply_store=self.store,
            executable="codex.cmd",
            runner=runner,
        )

    def test_list_and_select_projects(self) -> None:
        other = self.projects / "other"
        other.mkdir()
        (other / ".git").mkdir()
        controller = self.controller()
        self.assertEqual(controller.list_projects(), ["demo", "other"])
        controller.select_project("demo")
        self.assertEqual(controller.current_project(), "demo")
        with self.assertRaises(ProjectControlError):
            controller.select_project("../outside")

    def test_register_external_non_git_project_persists_and_resolves(self) -> None:
        external = Path(self.temporary.name) / "nested" / "my 项目"
        external.mkdir(parents=True)
        controller = self.controller()
        self.assertEqual(controller.add_project(str(external)), "my 项目")
        self.assertIsNone(self.store.load().current_project)
        restored = self.controller()
        self.assertIn("my 项目", restored.list_projects())
        restored.select_project("my 项目")
        self.assertEqual(restored.project_path("my 项目"), external.resolve())
        before = restored.registry_path.read_bytes()
        restored.add_project(str(external))
        self.assertEqual(restored.registry_path.read_bytes(), before)

    def test_registration_rejects_conflicts_and_invalid_targets_without_overwrite(self) -> None:
        controller = self.controller()
        external = Path(self.temporary.name) / "outside" / "demo"
        external.mkdir(parents=True)
        for value in (str(external), "relative", str(external / "missing")):
            with self.subTest(value=value), self.assertRaises(ProjectControlError):
                controller.add_project(value)
        self.assertFalse(controller.registry_path.exists())
        target = external / "file.txt"
        target.write_text("fictional", encoding="utf-8")
        with self.assertRaises(ProjectControlError):
            controller.add_project(str(target))

    def test_missing_registered_directory_is_hidden_until_available_again(self) -> None:
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        controller = self.controller()
        controller.add_project(str(external))
        missing = external.with_name("temporarily-moved")
        external.rename(missing)
        self.assertNotIn("external", controller.list_projects())
        with self.assertRaises(ProjectControlError):
            controller.project_path("external")
        missing.rename(external)
        self.assertIn("external", controller.list_projects())

    def test_corrupt_registry_is_never_overwritten(self) -> None:
        controller = self.controller()
        controller.registry_path.parent.mkdir(parents=True, exist_ok=True)
        controller.registry_path.write_text("invalid-json", encoding="utf-8")
        with self.assertRaises(ProjectControlError):
            controller.add_project(str(self.project))
        with self.assertRaises(ProjectControlError):
            controller.list_projects()
        self.assertEqual(controller.registry_path.read_text(), "invalid-json")

    def test_natural_language_lists_and_switches_projects(self) -> None:
        controller = self.controller()
        listed = controller.handle_control("我电脑上有哪些项目？")
        self.assertIsNotNone(listed)
        self.assertIn("demo", listed.reply)  # type: ignore[union-attr]
        switched = controller.handle_control("切换到 demo 项目")
        self.assertEqual(switched.reply, "已切换到项目：demo")  # type: ignore[union-attr]

    def test_switch_project_does_not_require_project_suffix(self) -> None:
        controller = self.controller()
        for command in ("切换到 demo", "进入 demo", "使用 demo。"):
            switched = controller.handle_control(command)
            self.assertIsNotNone(switched)
            self.assertEqual(switched.reply, "已切换到项目：demo")  # type: ignore[union-attr]

    def test_list_current_projects_question_is_control_not_model_input(self) -> None:
        controller = self.controller()
        for command in ("列一下当前项目", "列一下当前项目。", "列出当前的项目", "列一下所有项目"):
            with self.subTest(command=command):
                reply = controller.handle_control(command, allow_change=False)
                self.assertIsNotNone(reply)
                self.assertIn("可用项目：", reply.reply)  # type: ignore[union-attr]
                self.assertIn("demo", reply.reply)  # type: ignore[union-attr]
        self.assertIsNone(controller.handle_control("帮我写一篇列一下当前项目的说明"))

    def test_natural_language_approval_actions(self) -> None:
        controller = self.controller()
        approval = Approval(
            code="ABC123",
            project="demo",
            owner_hash=controller.owner_hash(),
            fingerprint=controller.fingerprint(self.project),
            patch="diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+new\n",
        )
        controller.save_approval(approval)
        shown = controller.handle_control("查看修改 ABC123")
        self.assertIn("待确认修改｜ABC123", shown.reply)  # type: ignore[union-attr]
        self.assertIn("新增内容：\n+ new", shown.reply)  # type: ignore[union-attr]
        self.assertNotIn("diff --git", shown.reply)  # type: ignore[union-attr]
        rejected = controller.handle_control("取消任务 ABC123")
        self.assertIn("已拒绝", rejected.reply)  # type: ignore[union-attr]

    def test_current_task_status_finds_pending_task(self) -> None:
        self.store.set_current_project("demo")
        controller = self.controller()
        controller.save_approval(
            Approval(
                code="ABC123", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project),
                patch="diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+x\n",
            )
        )

        reply = controller.handle_control("当前任务状态")

        self.assertIn("待确认修改｜ABC123", reply.reply)  # type: ignore[union-attr]

    def test_large_patch_shows_file_summary_and_on_demand_details(self) -> None:
        controller = self.controller()
        patch = (
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
            "@@ -1,2 +1,2 @@\n-old\n+new\n context\n"
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n keep\n+added\n"
        )
        controller.save_approval(
            Approval(
                code="ABC123", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project), patch=patch,
            )
        )

        summary = controller.handle_control("查看修改 ABC123")
        file_detail = controller.handle_control("查看 README.md 的修改 ABC123")
        deleted = controller.handle_control("查看删除内容 ABC123")
        raw = controller.handle_control("查看原始补丁 ABC123")

        self.assertIn("涉及：2 个文件（新增 2 行，删除 1 行）", summary.reply)  # type: ignore[union-attr]
        self.assertNotIn("diff --git", summary.reply)  # type: ignore[union-attr]
        self.assertIn("改前（1 行）：\nold", file_detail.reply)  # type: ignore[union-attr]
        self.assertIn("改后（1 行）：\nnew", file_detail.reply)  # type: ignore[union-attr]
        self.assertNotIn("\n- old", file_detail.reply)  # type: ignore[union-attr]
        self.assertIn("README.md：\n- old", deleted.reply)  # type: ignore[union-attr]
        self.assertIn("diff --git", raw.reply)  # type: ignore[union-attr]
        self.assertIn("```diff", raw.reply)  # type: ignore[union-attr]

    def test_file_detail_extracts_replaced_user_visible_message(self) -> None:
        controller = self.controller()
        patch = (
            "diff --git a/src/wxbot/cli.py b/src/wxbot/cli.py\n"
            "--- a/src/wxbot/cli.py\n+++ b/src/wxbot/cli.py\n"
            "@@ -1 +1,4 @@\n"
            "-return GeneratedReply(True, f\"{project} 修改任务已开始，完成后会自动通知。\")\n"
            "+return GeneratedReply(\n+    True,\n"
            "+    f\"{project} 正在隔离环境中生成并验证修改，完成后会发给你确认。\",\n+ )\n"
        )
        controller.save_approval(
            Approval(
                code="ABC123", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project), patch=patch,
            )
        )

        detail = controller.handle_control("查看 src/wxbot/cli.py 的修改 ABC123")

        self.assertIn("改前（1 行）：\n{project} 修改任务已开始", detail.reply)  # type: ignore[union-attr]
        self.assertIn("改后（4 行）：\n{project} 正在隔离环境中", detail.reply)  # type: ignore[union-attr]
        self.assertNotIn("return GeneratedReply", detail.reply)  # type: ignore[union-attr]

    def test_pending_task_supports_natural_language_without_code(self) -> None:
        self.store.set_current_project("demo")
        controller = self.controller()
        controller.save_approval(
            Approval(
                code="ABC123", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project),
                patch="diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+new\n",
            )
        )

        shown = controller.handle_control("你这个任务是怎么改的")
        cancelled = controller.handle_control("取消刚才的修改")

        self.assertIn("待确认修改｜ABC123", shown.reply)  # type: ignore[union-attr]
        self.assertIn("已拒绝修改 ABC123", cancelled.reply)  # type: ignore[union-attr]

    def test_new_task_immediately_returns_readable_summary(self) -> None:
        self.store.set_current_project("demo")
        controller = self.controller()
        controller.run_codex = lambda *_args, **_kwargs: (
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
            "@@ -0,0 +1 @@\n+new\n"
        )

        reply = controller.propose_change("demo", "新增 README 说明")

        self.assertIn("待确认修改｜", reply)
        self.assertIn("新增内容：\n+ new", reply)
        self.assertIn("批准刚才的修改", reply)
        self.assertNotIn("回复“查看修改", reply)

    def test_configured_task_executor_generates_diff_from_workspace_changes(self) -> None:
        self.store.set_current_project("demo")
        (self.project / "README.md").write_text("old\n", encoding="utf-8")
        controller = self.controller()
        received: list[str] = []

        def execute(
            project: str, message: str, workspace: Path, _cancel_event: object
        ) -> str:
            received.extend([project, message])
            (workspace / "README.md").write_text("new\n", encoding="utf-8")
            return "完成"

        controller.configure_tasks(
            workspace=TaskWorkspace(
                projects_root=self.projects, task_root=self.root / "tasks"
            ),
            executor=execute,
        )

        reply = controller.propose_change("demo", "修改 README")

        self.assertEqual(received, ["demo", "修改 README"])
        self.assertIn("- old", reply)
        self.assertIn("+ new", reply)
        self.assertEqual((self.project / "README.md").read_text(encoding="utf-8"), "old\n")

    def test_natural_language_approval_uses_current_pending_task(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        self.store.set_current_project("demo")
        target = self.project / "README.md"
        target.write_text("old\n", encoding="utf-8")
        controller = self.controller()
        controller.save_approval(
            Approval(
                code="ABC123", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project),
                patch="diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
            )
        )

        reply = controller.handle_control("批准刚才的修改")

        self.assertIn("修改完成｜ABC123", reply.reply)  # type: ignore[union-attr]
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_snapshot_excludes_sensitive_paths(self) -> None:
        (self.project / "README.md").write_text("public", encoding="utf-8")
        (self.project / ".env").write_text("SECRET=value", encoding="utf-8")
        (self.project / "data").mkdir()
        (self.project / "data" / "session.json").write_text("token", encoding="utf-8")
        controller = self.controller()
        with controller.snapshot("demo") as snapshot:
            self.assertTrue((snapshot / "README.md").exists())
            self.assertFalse((snapshot / ".env").exists())
            self.assertFalse((snapshot / "data" / "session.json").exists())

    def test_snapshot_copies_all_non_sensitive_text_files(self) -> None:
        src = self.project / "src"
        src.mkdir()
        (src / "daemon.py").write_text("class DaemonController: pass", encoding="utf-8")
        (src / "unrelated.py").write_text("UNRELATED = True", encoding="utf-8")
        with self.controller().snapshot("demo", "daemon.py 是做什么的") as snapshot:
            self.assertEqual(snapshot.parent, self.root / "tmp")
            self.assertTrue((snapshot / "src" / "daemon.py").exists())
            self.assertTrue((snapshot / "src" / "unrelated.py").exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_patch_rejects_delete_and_sensitive_path(self) -> None:
        controller = self.controller()
        with self.assertRaises(ProjectControlError):
            controller.validate_patch(
                "diff --git a/a.txt b/a.txt\ndeleted file mode 100644\n--- a/a.txt\n+++ /dev/null\n"
            )
        with self.assertRaises(ProjectControlError):
            controller.validate_patch(
                "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b\n"
            )

    def test_high_risk_action_is_routed_to_agent_for_explicit_confirmation(self) -> None:
        self.store.set_current_project("demo")
        reply = self.controller().handle_control("删除文件并推送到远端")
        self.assertIsNone(reply)

    def test_sensitive_topic_query_is_not_blocked(self) -> None:
        self.store.set_current_project("demo")
        reply = self.controller().handle_control("token 保存在哪里")
        self.assertIsNone(reply)

    def test_safe_test_request_runs_existing_project_tests(self) -> None:
        self.store.set_current_project("demo")
        (self.project / "tests").mkdir()
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "Ran 70 tests in 1.234s\n\nOK", "")

        reply = self.controller(runner).handle_control("运行测试")

        self.assertEqual(reply.reply, "demo 测试通过：70 项全部成功，耗时 1.234 秒。")  # type: ignore[union-attr]
        self.assertNotIn("命令：", reply.reply)  # type: ignore[union-attr]
        self.assertEqual(calls[0][1:4], ["-m", "unittest", "discover"])

    def test_failed_safe_command_returns_only_error_summary(self) -> None:
        self.store.set_current_project("demo")
        (self.project / "tests").mkdir()

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "long details\nlast failure", "")

        reply = self.controller(runner).handle_control("运行测试")

        self.assertIn("执行失败", reply.reply)  # type: ignore[union-attr]
        self.assertIn("错误摘要：last failure", reply.reply)  # type: ignore[union-attr]
        self.assertNotIn("long details", reply.reply)  # type: ignore[union-attr]

    def test_project_allows_only_one_pending_write_task(self) -> None:
        controller = self.controller()
        controller.save_approval(
            Approval(
                code="ABC123", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project),
                patch="diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+x\n",
            )
        )

        with self.assertRaisesRegex(ProjectControlError, "已有待审批"):
            controller.propose_change("demo", "新增说明")

    def test_approve_applies_patch_without_commit(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        target = self.project / "README.md"
        target.write_text("old\n", encoding="utf-8")
        controller = self.controller()
        patch = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
        )
        approval = Approval(
            code="A1B2C3",
            project="demo",
            owner_hash=controller.owner_hash(),
            fingerprint=controller.fingerprint(self.project),
            patch=patch,
        )
        controller.save_approval(approval)
        result = controller.approve("A1B2C3")
        self.assertIn("修改完成｜A1B2C3", result)
        self.assertIn("本次修改未提交、未推送", result)
        self.assertNotIn(str(self.project), result)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(controller.load_approval("A1B2C3").status, "applied")

    def test_approve_preserves_utf8_lf_patch_input_on_windows(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        target = self.project / "README.md"
        target.write_text("## 验证\n\n协议范围和真实微信验收步骤。\n", encoding="utf-8", newline="\n")
        controller = self.controller()
        patch = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,5 @@\n"
            " ## 验证\n \n+测试默认使用本地 mock server。\n+\n"
            " 协议范围和真实微信验收步骤。\n"
        )
        controller.save_approval(
            Approval(
                code="A1B2C3", project="demo", owner_hash=controller.owner_hash(),
                fingerprint=controller.fingerprint(self.project), patch=patch,
            )
        )

        result = controller.approve("A1B2C3")

        self.assertIn("修改完成｜A1B2C3", result)
        self.assertIn("测试默认使用本地 mock server。", target.read_text(encoding="utf-8"))

    def test_approve_runs_detected_validation(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        target = self.project / "README.md"
        target.write_text("old\n", encoding="utf-8")
        tests = self.project / "tests"
        tests.mkdir()
        (tests / "test_ok.py").write_text(
            "import unittest\n\nclass Ok(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        controller = self.controller()
        approval = Approval(
            code="A1B2C3", project="demo", owner_hash=controller.owner_hash(),
            fingerprint=controller.fingerprint(self.project),
            patch="diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
            validation_command=controller.command_runner.validation_command(self.project),
        )
        controller.save_approval(approval)

        result = controller.approve("A1B2C3")

        self.assertIn("1 项测试全部通过", result)
        self.assertNotIn("python", result.lower())

    def test_project_change_invalidates_approval(self) -> None:
        target = self.project / "README.md"
        target.write_text("old\n", encoding="utf-8")
        controller = self.controller()
        approval = Approval(
            code="D4E5F6",
            project="demo",
            owner_hash=controller.owner_hash(),
            fingerprint=controller.fingerprint(self.project),
            patch="diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
        )
        controller.save_approval(approval)
        target.write_text("changed\n", encoding="utf-8")
        with self.assertRaises(ProjectControlError):
            controller.approve("D4E5F6")


if __name__ == "__main__":
    unittest.main()
