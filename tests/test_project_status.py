from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wxbot.api.models import MessageItem, WeixinMessage
from wxbot.cli import AppServerReplyGenerator
from wxbot.project.status import ProjectStatusError, ProjectStatusReader


def roadmap(next_step: str = "验证 client_id 稳定性") -> str:
    return f"""# 路线图

## 当前状态

- 当前阶段：可靠性收敛。
- 主任务：验收微信会话列表。
- 主任务状态：待验收。
- 验收条件：确认显示最近一次有效提问。
- 当前插入任务：无。
- 插入任务状态：已完成。
- 插入任务完成后恢复：验收微信会话列表。
- 阻塞：无。
- 正式下一步：{next_step}。

## 待办

- [x] 已完成事项
- [ ] 第一项
- [ ] 第二项
- [ ] 第三项
- [ ] 第四项
- [ ] 第五项
- [ ] 第六项
"""


class ProjectStatusReaderTests(unittest.TestCase):
    def test_classifies_only_bounded_project_status_queries(self) -> None:
        cases = {
            "下一步": "next",
            "接下来做什么？": "next",
            "当前最优先的任务是什么": "priority",
            "当前项目进展怎么样": "progress",
            "项目当前任务": "task",
            "当前任务怎么样了": "task",
            "还有哪些未完成任务": "incomplete",
            "有没有阻塞项": "blocked",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(ProjectStatusReader.classify(message), expected)
        self.assertIsNone(ProjectStatusReader.classify("项目主要功能是什么"))

    def test_reads_latest_roadmap_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(
                roadmap().replace("验收微信会话列表", "先做第一件事"),
                encoding="utf-8",
            )
            reader = ProjectStatusReader(path)
            self.assertIn("先做第一件事", reader.answer("下一步") or "")
            path.write_text(
                roadmap().replace("验收微信会话列表", "改做第二件事"),
                encoding="utf-8",
            )
            self.assertIn("改做第二件事", reader.answer("下一步") or "")

    def test_renders_progress_and_limits_incomplete_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(roadmap(), encoding="utf-8")
            status = ProjectStatusReader(path).read()
            progress = status.render("progress")
            self.assertIn("当前阶段：可靠性收敛。", progress)
            self.assertIn("下一步：验收微信会话列表。", progress)
            incomplete = status.render("incomplete")
            self.assertIn("5. 第五项", incomplete)
            self.assertNotIn("第六项", incomplete)
            self.assertNotIn("已完成事项", incomplete)

    def test_rejects_missing_or_incomplete_roadmap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            with self.assertRaisesRegex(ProjectStatusError, "缺少 ROADMAP.md"):
                ProjectStatusReader(path).read()
            path.write_text("## 当前状态\n\n- 当前阶段：测试\n", encoding="utf-8")
            with self.assertRaisesRegex(ProjectStatusError, "缺少字段"):
                ProjectStatusReader(path).read()

    def test_prefers_inserted_task_then_resumes_waiting_main_task(self) -> None:
        text = roadmap().replace(
            "- 当前插入任务：无。\n- 插入任务状态：已完成。",
            "- 当前插入任务：修复任务恢复规则。\n- 插入任务状态：进行中。",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(text, encoding="utf-8")
            status = ProjectStatusReader(path).read()
            self.assertEqual(status.next_step, "修复任务恢复规则。")
            self.assertIn("主任务：验收微信会话列表。（待验收）", status.render("next"))

            path.write_text(
                text.replace("- 插入任务状态：进行中。", "- 插入任务状态：已完成。"),
                encoding="utf-8",
            )
            resumed = ProjectStatusReader(path).read()
            self.assertEqual(resumed.next_step, "验收微信会话列表。")

    def test_keeps_legacy_roadmap_compatible(self) -> None:
        legacy = """# 路线图

## 当前状态

- 当前阶段：旧项目。
- 进行中：无。
- 阻塞：无。
- 下一步：继续旧任务。
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(legacy, encoding="utf-8")
            self.assertEqual(ProjectStatusReader(path).read().next_step, "继续旧任务。")


class ProjectStatusRoutingTests(unittest.TestCase):
    def test_wechat_status_query_bypasses_model_and_reads_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            root.mkdir()
            path = root / "ROADMAP.md"
            path.write_text(roadmap("读取真实路线图"), encoding="utf-8")

            class Sessions:
                def decide_project(self, _message: str):
                    raise AssertionError("项目进度查询不应调用模型")

            class Projects:
                @staticmethod
                def active_project() -> str:
                    return "demo"

                @staticmethod
                def project_path(name: str) -> Path:
                    self.assertEqual(name, "demo")
                    return root

                @staticmethod
                def handle_control(_message: str, **_kwargs: object):
                    raise AssertionError("项目进度查询应先走确定性读取")

            generator = AppServerReplyGenerator(
                sessions=Sessions(),  # type: ignore[arg-type]
                projects=Projects(),  # type: ignore[arg-type]
            )
            message = WeixinMessage(
                "user", "bot", "context", 1, 1, "client", 1,
                (MessageItem(type=1, text="下一步"),),
            )
            reply = generator.generate_message(message)
            self.assertIn("验收微信会话列表", reply.reply)
            path.write_text(
                roadmap().replace("验收微信会话列表", "读取更新后的路线图"),
                encoding="utf-8",
            )
            self.assertIn("读取更新后的路线图", generator.generate_message(message).reply)


if __name__ == "__main__":
    unittest.main()
