from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from wxbot.api.models import CDNMedia, FileItem, ImageItem, MessageItem, WeixinMessage
from wxbot.message.inbox import InboxStateError, InboxStore
from wxbot.message.poller import message_key


def message(message_id: int, text: str = "测试消息") -> WeixinMessage:
    return WeixinMessage(
        from_user_id="wxid_test_owner",
        to_user_id="wxid_test_bot",
        context_token="test-context-token",
        message_type=1,
        message_id=message_id,
        client_id=f"test-client-{message_id}",
        create_time_ms=1000 + message_id,
        items=(MessageItem(type=1, msg_id=f"item-{message_id}", text=text),),
    )


class InboxStoreTests(unittest.TestCase):
    def test_file_message_round_trip_keeps_nested_protocol_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboxStore(Path(directory) / "inbox.json")
            current = message(17)
            current = WeixinMessage(
                **{
                    **current.__dict__,
                    "items": (
                        MessageItem(
                            type=4,
                            file=FileItem(
                                media=CDNMedia(
                                    encrypt_query_param="file-query",
                                    aes_key="dGVzdC1rZXk=",
                                ),
                                file_name="notes.txt",
                                length="12",
                            ),
                        ),
                    ),
                }
            )
            store.enqueue(current)

            recovered, _ = store.recover()

            self.assertEqual(recovered[0].files[0].file_name, "notes.txt")
            self.assertEqual(
                recovered[0].files[0].media.encrypt_query_param,  # type: ignore[union-attr]
                "file-query",
            )
            store.complete(message_key(current))
            payload = (Path(directory) / "inbox.json").read_text(encoding="utf-8")
            self.assertNotIn("file-query", payload)
    def test_image_message_round_trip_keeps_nested_protocol_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboxStore(Path(directory) / "inbox.json")
            current = WeixinMessage(
                from_user_id="wxid_test_owner",
                to_user_id="wxid_test_bot",
                context_token="test-context-token",
                message_type=1,
                message_id=10,
                client_id="test-client-10",
                create_time_ms=1010,
                items=(MessageItem(
                    type=2,
                    msg_id="image-item",
                    image=ImageItem(
                        media=CDNMedia(
                            encrypt_query_param="query",
                            aes_key="encoded-key",
                        ),
                        aeskey="00112233445566778899aabbccddeeff",
                        mid_size=123,
                    ),
                ),),
            )

            self.assertTrue(store.enqueue(current))
            self.assertEqual(InboxStore(store.path).records()[0].message, current)

    def test_enqueue_is_durable_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.json"
            store = InboxStore(path)
            current = message(1)

            self.assertTrue(store.enqueue(current))
            self.assertFalse(store.enqueue(current))

            loaded = InboxStore(path).records()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].status, "queued")
            self.assertEqual(loaded[0].message, current)

    def test_recover_requeues_only_tasks_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboxStore(Path(directory) / "inbox.json")
            queued = message(1, "尚未处理")
            interrupted = message(2, "处理中断")
            completed = message(3, "已经完成")
            store.enqueue(queued)
            store.enqueue(interrupted)
            store.start("message:2")
            store.enqueue(completed)
            store.start("message:3")
            store.complete("message:3")

            recovered, uncertain_count = InboxStore(store.path).recover()

            self.assertEqual(recovered, [queued])
            self.assertEqual(uncertain_count, 1)
            statuses = {record.key: record.status for record in store.records()}
            self.assertEqual(statuses, {
                "message:1": "queued",
                "message:2": "uncertain",
                "message:3": "completed",
            })
            terminal = {
                record.key: record for record in store.records()
                if record.status in {"completed", "uncertain"}
            }
            self.assertIsNone(terminal["message:2"].message)
            self.assertIsNone(terminal["message:3"].message)

    def test_completed_message_is_not_enqueued_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboxStore(Path(directory) / "inbox.json")
            current = message(1)
            store.enqueue(current)
            store.start("message:1")
            store.complete("message:1")
            self.assertFalse(InboxStore(store.path).enqueue(current))
            raw = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertNotIn("message", raw[0])
            self.assertNotIn("wxid_test_owner", store.path.read_text(encoding="utf-8"))
            self.assertNotIn("test-context-token", store.path.read_text(encoding="utf-8"))

    def test_recover_migrates_legacy_terminal_record_without_message_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.json"
            path.write_text(json.dumps([{
                "key": "message:1",
                "status": "completed",
                "message": asdict(message(1, "旧格式敏感正文")),
            }], ensure_ascii=False), encoding="utf-8")

            recovered, uncertain_count = InboxStore(path).recover()

            self.assertEqual(recovered, [])
            self.assertEqual(uncertain_count, 0)
            migrated = path.read_text(encoding="utf-8")
            self.assertNotIn("message", json.loads(migrated)[0])
            self.assertNotIn("旧格式敏感正文", migrated)
            self.assertNotIn("test-context-token", migrated)

    def test_terminal_records_expire_and_do_not_block_same_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.json"
            store = InboxStore(path, retention_seconds=1)
            current = message(1)
            store.enqueue(current)
            store.start("message:1")
            store.complete("message:1")
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw[0]["updated_at"] = 0
            path.write_text(json.dumps(raw), encoding="utf-8")

            self.assertEqual(store.records(), [])
            self.assertTrue(store.enqueue(current))

    def test_terminal_record_limit_preserves_active_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboxStore(Path(directory) / "inbox.json", max_records=2)
            for message_id in range(1, 4):
                store.enqueue(message(message_id))
                store.start(f"message:{message_id}")
                store.complete(f"message:{message_id}")
            store.enqueue(message(4))

            records = store.records()
            self.assertEqual([record.key for record in records], [
                "message:2", "message:3", "message:4",
            ])
            self.assertEqual(store.counts(), {
                "queued": 1, "processing": 0, "completed": 2, "uncertain": 0,
            })

    def test_corrupt_state_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(InboxStateError):
                InboxStore(path).recover()
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_invalid_message_shape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.json"
            path.write_text(json.dumps([
                {"key": "message:1", "status": "queued", "message": {"from_user_id": 1}},
            ]), encoding="utf-8")
            with self.assertRaises(InboxStateError):
                InboxStore(path).records()


if __name__ == "__main__":
    unittest.main()
