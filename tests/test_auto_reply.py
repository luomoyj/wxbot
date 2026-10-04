from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wxbot.ai.codex_reply import GeneratedReply
from wxbot.api.models import ImageItem, MessageItem, WeixinMessage
from wxbot.message.auto_reply import (
    AutoReplyService,
    AutoReplyStateError,
    AutoReplyStore,
    split_reply_text,
)
from wxbot.message.media import InboundTextFile
from wxbot.project.tasks import ProjectTask


def message(
    *, user: str = "user-a", message_id: int = 1,
    context: str | None = "ctx", text: str = "你好",
) -> WeixinMessage:
    return WeixinMessage(
        from_user_id=user,
        to_user_id="bot",
        context_token=context,
        message_type=1,
        message_id=message_id,
        client_id=None,
        create_time_ms=1,
        items=(MessageItem(type=1, text=text),),
    )


class FakeGenerator:
    def __init__(self, reply: GeneratedReply) -> None:
        self.reply = reply
        self.messages: list[str] = []
        self.histories: list[list[dict[str, str]]] = []

    def generate(self, text: str, history: list[dict[str, str]]) -> GeneratedReply:
        self.messages.append(text)
        self.histories.append(history)
        return self.reply


class ClearableFakeGenerator(FakeGenerator):
    def __init__(self, reply: GeneratedReply) -> None:
        super().__init__(reply)
        self.cleared = False

    def clear_context(self) -> None:
        self.cleared = True


class ResettableFakeGenerator(FakeGenerator):
    def __init__(self, reply: GeneratedReply) -> None:
        super().__init__(reply)
        self.reset = False

    def reset_all_ai_state(self) -> None:
        self.reset = True


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.sent_files: list[dict[str, object]] = []

    def send_text(self, **kwargs: str) -> None:
        self.sent.append(kwargs)

    def send_text_file(self, **kwargs: object) -> None:
        self.sent_files.append(kwargs)


class AutoReplyTests(unittest.TestCase):
    def test_split_reply_text_uses_numbered_bounded_parts(self) -> None:
        parts = split_reply_text(("第一段。\n\n第二段。" * 20), limit=120)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 120 for part in parts))
        self.assertTrue(parts[0].startswith(f"（1/{len(parts)}）\n"))
        self.assertTrue(parts[-1].startswith(f"（{len(parts)}/{len(parts)}）\n"))

    def test_split_reply_text_balances_code_fences_across_parts(self) -> None:
        parts = split_reply_text("```python\n" + "print('x')\n" * 30 + "```", limit=100)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part.count("```") % 2 == 0 for part in parts))

    def test_long_reply_uses_stable_part_ids_and_stops_after_send_error(self) -> None:
        class FailingClient(FakeClient):
            def send_text(self, **kwargs: str) -> None:
                self.sent.append(kwargs)
                if len(self.sent) == 2:
                    raise RuntimeError("发送结果不确定")

        with tempfile.TemporaryDirectory() as directory:
            client = FailingClient()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=FakeGenerator(GeneratedReply(True, "长回复。" * 1000)),
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )

            with self.assertRaisesRegex(RuntimeError, "不确定"):
                service.handle(message())

            self.assertEqual(len(client.sent), 2)
            self.assertTrue(client.sent[0]["client_id"].endswith(":part:1"))
            self.assertTrue(client.sent[1]["client_id"].endswith(":part:2"))

    def test_outbound_file_is_sent_without_second_text_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.txt"
            path.write_text("要发送的内容", encoding="utf-8")
            generator = FakeGenerator(GeneratedReply(
                False,
                "",
                outbound_file=path,
                outbound_name="notes.txt",
                outbound_sha256="digest",
            ))
            client = FakeClient()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )

            result = service.handle(message(text="把 notes.txt 发给我"))

            self.assertEqual(result, "sent")
            self.assertEqual(client.sent, [])
            self.assertEqual(client.sent_files[0]["file_name"], "notes.txt")
            self.assertEqual(client.sent_files[0]["expected_sha256"], "digest")

    def test_paired_user_can_send_text_file_without_extra_confirmation(self) -> None:
        class FileGenerator(FakeGenerator):
            def __init__(self) -> None:
                super().__init__(GeneratedReply(True, "附件回复"))
                self.local_file: InboundTextFile | None = None

            def generate_message(
                self, _message: WeixinMessage, *,
                local_image: Path | None = None,
                local_file: InboundTextFile | None = None,
            ) -> GeneratedReply:
                self.local_file = local_file
                return self.reply

        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            generator = FileGenerator()
            client = FakeClient()
            service = AutoReplyService(
                client=client, generator=generator, store=store,  # type: ignore[arg-type]
            )
            service.handle(message(text="先配对"))
            attachment_path = Path(directory) / "attachment.txt"
            attachment_path.write_text("附件正文", encoding="utf-8")
            attachment = InboundTextFile(attachment_path, "attachment.txt")
            file_only = WeixinMessage(
                from_user_id="user-a",
                to_user_id="bot",
                context_token="ctx-file",
                message_type=1,
                message_id=2,
                client_id="file",
                create_time_ms=2,
                items=(MessageItem(type=4),),
            )

            result = service.handle(file_only, local_file=attachment)

            self.assertEqual(result, "sent")
            self.assertIs(generator.local_file, attachment)
            self.assertEqual(client.sent[-1]["text"], "附件回复")

    def test_paired_user_can_send_image_without_text(self) -> None:
        class ImageGenerator(FakeGenerator):
            def __init__(self) -> None:
                super().__init__(GeneratedReply(True, "图片回复"))
                self.local_image: Path | None = None

            def generate_message(
                self, _message: WeixinMessage, *, local_image: Path | None = None,
            ) -> GeneratedReply:
                self.local_image = local_image
                return self.reply

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AutoReplyStore(root / "auto.json")
            store.claim(message())
            image_path = root / "image.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            current = WeixinMessage(
                from_user_id="user-a",
                to_user_id="bot",
                context_token="ctx",
                message_type=1,
                message_id=2,
                client_id=None,
                create_time_ms=2,
                items=(MessageItem(type=2, image=ImageItem()),),
            )
            client = FakeClient()
            generator = ImageGenerator()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )

            result = service.handle(current, local_image=image_path)

            self.assertEqual(result, "sent")
            self.assertEqual(generator.local_image, image_path)
            self.assertEqual(client.sent[0]["text"], "图片回复")
    def test_pending_task_notification_is_sent_before_current_reply_and_acknowledged(self) -> None:
        class NotificationGenerator(FakeGenerator):
            def __init__(self) -> None:
                super().__init__(GeneratedReply(True, "当前回复"))
                now = time.time()
                self.task = ProjectTask(
                    "ABC123", "wxbot", "修改提示语", "failed", now, now,
                    result="任务因服务重启中断。", notification_pending=True,
                )
                self.acknowledged: list[str] = []

            def pending_notifications(self):
                return [self.task]

            @staticmethod
            def task_notice(task: ProjectTask) -> str:
                return task.result

            def acknowledge_notification(self, task_id: str) -> None:
                self.acknowledged.append(task_id)

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            generator = NotificationGenerator()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )

            service.handle(message())

            self.assertEqual([item["text"] for item in client.sent], [
                "任务因服务重启中断。", "当前回复",
            ])
            self.assertEqual(generator.acknowledged, ["ABC123"])

    def test_status_query_merges_matching_pending_notification(self) -> None:
        class StatusGenerator(FakeGenerator):
            def __init__(self) -> None:
                super().__init__(GeneratedReply(
                    True,
                    "wxbot 任务已取消，未生成待审批修改，真实项目未变化。",
                ))
                now = time.time()
                self.task = ProjectTask(
                    "ABC123", "wxbot", "修改提示语", "cancelled", now, now,
                    result="任务已取消。", notification_pending=True,
                )
                self.acknowledged: list[str] = []

            def pending_notifications(self):
                return [self.task]

            @staticmethod
            def suppress_pending_notification(_message, _task) -> bool:
                return True

            def acknowledge_notification(self, task_id: str) -> None:
                self.acknowledged.append(task_id)

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            generator = StatusGenerator()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )

            service.handle(message(text="当前任务怎么样了"))

            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0]["text"], generator.reply.reply)
            self.assertEqual(generator.acknowledged, ["ABC123"])

    def test_first_user_pairs_and_sends_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            generator = FakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )
            self.assertEqual(service.handle(message()), "paired-and-sent")
            self.assertEqual(service.handle(message()), "ignored")
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0]["text"], "自动回复")
            self.assertEqual(
                service.store.get_history(),
                [{"user": "你好", "assistant": "自动回复"}],
            )

    def test_auto_reply_client_id_is_stable_across_service_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sent_ids: list[str] = []
            for name in ("before-restart.json", "after-restart.json"):
                client = FakeClient()
                service = AutoReplyService(
                    client=client,  # type: ignore[arg-type]
                    generator=FakeGenerator(GeneratedReply(True, "自动回复")),  # type: ignore[arg-type]
                    store=AutoReplyStore(Path(directory) / name),
                )
                service.handle(message(message_id=42))
                sent_ids.append(client.sent[0]["client_id"])

            self.assertEqual(sent_ids, ["wxbot:auto:message-42"] * 2)

    def test_other_user_is_not_sent_to_codex_or_wechat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            generator = FakeGenerator(GeneratedReply(True, "reply"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )
            service.handle(message(user="allowed", message_id=1))
            self.assertEqual(service.handle(message(user="other", message_id=2)), "ignored")
            self.assertEqual(generator.messages, ["你好"])
            self.assertEqual(len(client.sent), 1)

    def test_whitelist_user_clears_history_in_wechat_without_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = FakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            service.handle(message(message_id=1))
            store.set_current_project("wxbot")
            store.append_history("旧问题", "旧回答")

            result = service.handle(message(message_id=2, text="清空上下文。"))

            state = store.load()
            self.assertEqual(result, "history-cleared")
            self.assertEqual(generator.messages, ["你好"])
            self.assertEqual(client.sent[-1]["text"], "对话上下文已清空，我们重新开始。")
            self.assertEqual(state.allowed_user_id, "user-a")
            self.assertEqual(state.current_project, "wxbot")
            self.assertEqual(state.history, [])
            self.assertEqual(state.processed_keys, ["message:1", "message:2"])

    def test_clear_phrase_notifies_session_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = ClearableFakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            service.handle(message(message_id=1))
            service.handle(message(message_id=2, text="清空上下文"))

            self.assertTrue(generator.cleared)

    def test_other_user_cannot_clear_history_in_wechat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = FakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            service.handle(message(user="allowed", message_id=1))

            result = service.handle(message(user="other", message_id=2, text="清空对话上下文"))

            self.assertEqual(result, "ignored")
            self.assertEqual(generator.messages, ["你好"])
            self.assertEqual(len(client.sent), 1)

    def test_reset_all_ai_state_requires_preview_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = ResettableFakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            service.handle(message(message_id=1))
            store.set_current_project("wxbot")
            store.append_history("旧问题", "旧回答")

            preview = service.handle(message(message_id=2, text="清空全部 AI 状态"))

            state = store.load()
            self.assertEqual(preview, "reset-previewed")
            self.assertEqual(state.allowed_user_id, "user-a")
            self.assertEqual(state.current_project, "wxbot")
            self.assertFalse(generator.reset)
            self.assertIn("将清空", client.sent[-1]["text"])
            self.assertIn("将保留", client.sent[-1]["text"])

            confirmed = service.handle(
                message(message_id=3, text="确认清空全部AI状态")
            )

            self.assertEqual(confirmed, "ai-state-reset")
            self.assertTrue(generator.reset)
            self.assertEqual(store.load(), AutoReplyStore(Path(directory) / "empty.json").load())
            self.assertIn("全部本地 AI 状态已清空", client.sent[-1]["text"])

    def test_reset_confirmation_without_preview_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = ResettableFakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            service.handle(message(message_id=1))

            result = service.handle(
                message(message_id=2, text="确认清空全部AI状态")
            )

            self.assertEqual(result, "reset-not-confirmed")
            self.assertFalse(generator.reset)
            self.assertEqual(store.load().allowed_user_id, "user-a")
            self.assertIn("请先发送", client.sent[-1]["text"])

    def test_expired_reset_preview_does_not_reset_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = ResettableFakeGenerator(GeneratedReply(True, "自动回复"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            service.handle(message(message_id=1))
            service.handle(message(message_id=2, text="清空全部AI状态"))
            service._pending_reset = ("user-a", -1)

            result = service.handle(
                message(message_id=3, text="确认清空全部AI状态")
            )

            self.assertEqual(result, "reset-not-confirmed")
            self.assertFalse(generator.reset)
            self.assertEqual(store.load().allowed_user_id, "user-a")

    def test_short_history_keeps_up_to_twenty_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            generator = FakeGenerator(GeneratedReply(True, "收到"))
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=generator,  # type: ignore[arg-type]
                store=store,
            )
            for message_id in range(1, 22):
                service.handle(message(message_id=message_id))
            self.assertEqual(generator.histories[0], [])
            self.assertEqual(len(generator.histories[-1]), 20)
            self.assertEqual(len(store.get_history()), 20)

    def test_history_uses_character_budget_and_keeps_latest_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            store.append_history("u1", "a" * 3000)
            store.append_history("u2", "b" * 3000)
            store.append_history("u3", "c" * 3000)
            self.assertEqual([turn["user"] for turn in store.get_history()], ["u3"])
            store.append_history("latest", "x" * 7000)
            self.assertEqual(store.get_history(), [{"user": "latest", "assistant": "x" * 7000}])

    def test_old_state_loads_and_clear_history_preserves_other_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto.json"
            path.write_text(
                json.dumps({"allowed_user_id": "allowed", "processed_keys": ["message:1"]}),
                encoding="utf-8",
            )
            store = AutoReplyStore(path)
            self.assertEqual(store.get_history(), [])
            store.append_history("问题", "回答")
            store.clear_history()
            state = store.load()
            self.assertEqual(state.allowed_user_id, "allowed")
            self.assertEqual(state.processed_keys, ["message:1"])
            self.assertEqual(state.history, [])

    def test_reset_all_replaces_corrupt_state_with_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto.json"
            path.write_text("{broken", encoding="utf-8")
            store = AutoReplyStore(path)

            store.reset_all()

            self.assertIsNone(store.load().allowed_user_id)
            self.assertEqual(store.load().processed_keys, [])

    def test_legacy_project_history_is_migrated_to_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto.json"
            history = [{"user": "问题", "assistant": "回答"}]
            path.write_text(
                json.dumps({"current_project": "wxbot", "history": history}),
                encoding="utf-8",
            )

            state = AutoReplyStore(path).load()

            self.assertEqual(state.conversation_mode, "project")
            self.assertEqual(state.history, history)
            self.assertEqual(state.project_histories, {"wxbot": history})
            self.assertEqual(state.chat_history, [])

    def test_project_switch_preserves_each_project_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            store.append_history("聊天问题", "聊天回答")
            store.set_current_project("wxbot")
            store.append_history("wxbot 问题", "wxbot 回答")
            store.set_current_project("demo")
            store.append_history("demo 问题", "demo 回答")
            store.set_current_project("wxbot")

            self.assertEqual(store.get_history(), [{"user": "wxbot 问题", "assistant": "wxbot 回答"}])
            store.clear_history()
            store.set_current_project("demo")
            self.assertEqual(store.get_history(), [{"user": "demo 问题", "assistant": "demo 回答"}])
            store.set_chat_mode()
            self.assertEqual(store.get_history(), [{"user": "聊天问题", "assistant": "聊天回答"}])

    def test_corrupt_state_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto.json"
            original = b"{not-json"
            path.write_bytes(original)
            store = AutoReplyStore(path)
            with self.assertRaises(AutoReplyStateError):
                store.clear_history()
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_state_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto.json"
            path.write_text(json.dumps({"processed_keys": "not-a-list"}), encoding="utf-8")
            with self.assertRaises(AutoReplyStateError):
                AutoReplyStore(path).load()

    def test_concurrent_stores_do_not_lose_history_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auto.json"
            stores = [AutoReplyStore(path) for _ in range(5)]
            barrier = threading.Barrier(len(stores))
            errors: list[Exception] = []

            def append(index: int) -> None:
                try:
                    barrier.wait()
                    stores[index].append_history(f"u{index}", f"a{index}")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(len(stores))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(AutoReplyStore(path).get_history()), len(stores))

    def test_declined_reply_is_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=FakeGenerator(GeneratedReply(False, "")),  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
            )
            self.assertEqual(service.handle(message()), "declined")
            self.assertEqual(client.sent, [])

    def test_background_task_deferral_is_not_reported_as_declined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            store = AutoReplyStore(Path(directory) / "auto.json")
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=FakeGenerator(GeneratedReply(False, "", deferred=True)),  # type: ignore[arg-type]
                store=store,
            )
            self.assertEqual(service.handle(message()), "deferred")
            self.assertEqual(client.sent, [])
            self.assertEqual(store.get_history(), [])


class RecordingTyping:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def start(self, user_id: str, context_token: str) -> None:
        self.started.append((user_id, context_token))

    def stop(self, user_id: str) -> None:
        self.stopped.append(user_id)


class TypingLifecycleTests(unittest.TestCase):
    def test_handle_starts_and_stops_typing_for_whitelist_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            typing = RecordingTyping()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=FakeGenerator(GeneratedReply(True, "回复")),  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
                typing=typing,  # type: ignore[arg-type]
            )
            service.handle(message())
            self.assertEqual(typing.started, [("user-a", "ctx")])
            self.assertEqual(typing.stopped, ["user-a"])

    def test_non_whitelist_message_does_not_trigger_typing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            store.claim(message(user="user-owner"))
            client = FakeClient()
            typing = RecordingTyping()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=FakeGenerator(GeneratedReply(True, "回复")),  # type: ignore[arg-type]
                store=store,
                typing=typing,  # type: ignore[arg-type]
            )
            self.assertEqual(service.handle(message(user="user-other")), "ignored")
            self.assertEqual(typing.started, [])
            self.assertEqual(typing.stopped, [])

    def test_duplicate_message_does_not_trigger_typing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutoReplyStore(Path(directory) / "auto.json")
            client = FakeClient()
            typing = RecordingTyping()
            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=FakeGenerator(GeneratedReply(True, "回复")),  # type: ignore[arg-type]
                store=store,
                typing=typing,  # type: ignore[arg-type]
            )
            service.handle(message())
            self.assertEqual(service.handle(message()), "ignored")
            self.assertEqual(typing.started, [("user-a", "ctx")])
            self.assertEqual(typing.stopped, ["user-a"])

    def test_generator_failure_still_stops_typing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            typing = RecordingTyping()

            class ExplodingGenerator(FakeGenerator):
                def generate(self, text: str, history: list[dict[str, str]]) -> GeneratedReply:
                    raise RuntimeError("model failed")

            service = AutoReplyService(
                client=client,  # type: ignore[arg-type]
                generator=ExplodingGenerator(GeneratedReply(True, "")),  # type: ignore[arg-type]
                store=AutoReplyStore(Path(directory) / "auto.json"),
                typing=typing,  # type: ignore[arg-type]
            )
            with self.assertRaises(RuntimeError):
                service.handle(message())
            self.assertEqual(typing.started, [("user-a", "ctx")])
            self.assertEqual(typing.stopped, ["user-a"])


if __name__ == "__main__":
    unittest.main()
