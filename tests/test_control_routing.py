from __future__ import annotations

import unittest

from wxbot.dispatcher import MessageDispatcher
from wxbot.replies import AppServerReplyGenerator


class HelpControlTests(unittest.TestCase):
    def test_help_phrases_match(self) -> None:
        for phrase in (
            "帮助", "帮助指令", "指令帮助", "有哪些指令", "列出所有指令",
            "帮助。", "有哪些指令！",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(AppServerReplyGenerator.is_help_control(phrase))

    def test_help_phrases_do_not_match_other_text(self) -> None:
        for phrase in ("帮助我修改 README", "怎么获得帮助", ""):
            with self.subTest(phrase=phrase):
                self.assertFalse(AppServerReplyGenerator.is_help_control(phrase))


class CompactControlTests(unittest.TestCase):
    def test_compact_phrases_match_with_spaces_and_punctuation(self) -> None:
        for phrase in (
            "压缩上下文", "压缩 上下文", "压缩对话上下文。", "整理上下文！",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(AppServerReplyGenerator.is_compact_control(phrase))

    def test_compact_phrases_do_not_match_other_text(self) -> None:
        for phrase in ("压缩这个项目的上下文", "上下文压缩", "帮我压缩"):
            with self.subTest(phrase=phrase):
                self.assertFalse(AppServerReplyGenerator.is_compact_control(phrase))


class StatusQueryTests(unittest.TestCase):
    def test_status_query_variants_match(self) -> None:
        for phrase in (
            "当前任务怎么样了", "刚才的任务完成了吗", "还没做完吗", "任务做完了吗",
            "怎么还没好", "做完了吗。",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(AppServerReplyGenerator.is_status_query(phrase))

    def test_status_query_rejects_non_queries(self) -> None:
        for phrase in ("帮我完成任务", "这个任务之前怎么样了", "取消刚才的任务"):
            with self.subTest(phrase=phrase):
                self.assertFalse(AppServerReplyGenerator.is_status_query(phrase))


class ImmediateControlTests(unittest.TestCase):
    def test_task_and_checkpoint_controls_are_immediate(self) -> None:
        for phrase in (
            "取消刚才的任务",
            "有哪些运行中的任务",
            "运行中的任务",
            "查看刚才的修改",
            "修改了什么",
            "确认恢复刚才的修改",
            "确认安装刚才的项目依赖",
            "确认刚才的本地提交",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(AppServerReplyGenerator.is_immediate_control(phrase))

    def test_plain_project_request_is_not_immediate(self) -> None:
        for phrase in (
            "修改 README并运行测试",
            "把这个函数改成异步的",
            "帮我看看登录模块为什么超时",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(AppServerReplyGenerator.is_immediate_control(phrase))


class RoutingControlTests(unittest.TestCase):
    def test_project_routing_controls_match(self) -> None:
        for phrase in (
            "有哪些项目", "列出所有项目", "项目列表",
            "切换到 wxbot 项目", "切换到 wxbot", "使用 chaonao。",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(MessageDispatcher._is_routing_control(phrase))

    def test_project_routing_controls_reject_other_text(self) -> None:
        for phrase in ("怎么切换项目分支", "把项目都列出来", "提交代码"):
            with self.subTest(phrase=phrase):
                self.assertFalse(MessageDispatcher._is_routing_control(phrase))


if __name__ == "__main__":
    unittest.main()
