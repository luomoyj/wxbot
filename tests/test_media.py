from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wxbot.api.models import (
    CDNMedia, FileItem, ImageItem, MessageItem, VoiceItem, WeixinMessage,
)
from wxbot.message.media import (
    InboundImageManager,
    InboundTextFileManager,
    PermanentMediaError,
    TransientMediaError,
    VoiceProbeStore,
    decrypt_aes_ecb,
    image_extension,
    parse_aes_key,
    prepare_outbound_audio_file,
    prepare_outbound_text_file,
)


KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def encrypt(content: bytes) -> bytes:
    padding = 16 - len(content) % 16
    padded = content + bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def image_message(
    *,
    media: CDNMedia | None = None,
    aeskey: str | None = KEY.hex(),
) -> WeixinMessage:
    return WeixinMessage(
        from_user_id="wxid_test_owner",
        to_user_id="wxid_test_bot",
        context_token="test-context",
        message_type=1,
        message_id=7,
        client_id="test-client",
        create_time_ms=1000,
        items=(
            MessageItem(
                type=2,
                msg_id="image-item",
                image=ImageItem(
                    media=media or CDNMedia(encrypt_query_param="a+b/c="),
                    aeskey=aeskey,
                ),
            ),
        ),
    )


def file_message(
    *, name: str = "notes.txt", content_length: str = "12",
) -> WeixinMessage:
    return WeixinMessage(
        from_user_id="wxid_test_owner",
        to_user_id="wxid_test_bot",
        context_token="test-context",
        message_type=1,
        message_id=8,
        client_id="test-file-client",
        create_time_ms=1001,
        items=(
            MessageItem(
                type=4,
                msg_id="file-item",
                file=FileItem(
                    media=CDNMedia(
                        encrypt_query_param="file-query",
                        aes_key=base64.b64encode(KEY).decode("ascii"),
                    ),
                    file_name=name,
                    length=content_length,
                ),
            ),
        ),
    )


class MediaTests(unittest.TestCase):
    def test_prepare_outbound_audio_file_accepts_project_audio_and_rejects_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "reply.mp3"
            audio.write_bytes(b"ID3-test-audio")

            prepared = prepare_outbound_audio_file(audio, root)

            self.assertEqual(prepared.display_name, "reply.mp3")
            self.assertEqual(prepared.size, len(b"ID3-test-audio"))
            disallowed = root / "reply.exe"
            disallowed.write_bytes(b"binary")
            with self.assertRaises(PermanentMediaError):
                prepare_outbound_audio_file(disallowed, root)
            sensitive = root / "data"
            sensitive.mkdir()
            hidden_audio = sensitive / "reply.mp3"
            hidden_audio.write_bytes(b"ID3-private")
            with self.assertRaises(PermanentMediaError):
                prepare_outbound_audio_file(hidden_audio, root)

    def test_voice_model_and_probe_store_keep_only_safe_metadata(self) -> None:
        message = WeixinMessage.from_dict({
            "from_user_id": "wxid_private_owner",
            "item_list": [{
                "type": 3,
                "voice_item": {
                    "text": "不要写入诊断文件的语音正文",
                    "encode_type": 6,
                    "sample_rate": 24000,
                    "playtime": 1800,
                    "media": {
                        "encrypt_query_param": "private-cdn-param",
                        "aes_key": "private-aes-key",
                    },
                },
            }],
        })
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.voices, (
            VoiceItem(
                media=CDNMedia(
                    encrypt_query_param="private-cdn-param",
                    aes_key="private-aes-key",
                ),
                text="不要写入诊断文件的语音正文",
                encode_type=6,
                sample_rate=24000,
                playtime=1800,
            ),
        ))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice_probe.json"
            VoiceProbeStore(path).record(message)
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)

        self.assertEqual(set(data), {
            "observed_at", "item_types", "voice_count", "has_voice_item",
            "has_text", "text_length", "encode_type", "sample_rate", "playtime",
        })
        self.assertEqual(data["item_types"], [3])
        self.assertTrue(data["has_voice_item"])
        self.assertTrue(data["has_text"])
        self.assertEqual(data["text_length"], len("不要写入诊断文件的语音正文"))
        self.assertNotIn("wxid_private_owner", raw)
        self.assertNotIn("private-cdn-param", raw)
        self.assertNotIn("private-aes-key", raw)
        self.assertNotIn("不要写入诊断文件的语音正文", raw)

    def test_voice_probe_records_type_three_without_voice_item(self) -> None:
        message = WeixinMessage(
            from_user_id="wxid_private_owner",
            to_user_id="bot",
            context_token="private-context",
            message_type=1,
            message_id=1,
            client_id="voice",
            create_time_ms=1,
            items=(MessageItem(type=3),),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice_probe.json"
            VoiceProbeStore(path).record(message)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["item_types"], [3])
        self.assertEqual(data["voice_count"], 1)
        self.assertFalse(data["has_voice_item"])
        self.assertFalse(data["has_text"])

    def test_prepare_outbound_text_file_rejects_sensitive_or_changed_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normal = root / "notes.txt"
            normal.write_text("普通内容", encoding="utf-8")

            prepared = prepare_outbound_text_file(normal, root)

            self.assertEqual(prepared.display_name, "notes.txt")
            self.assertEqual(prepared.size, len("普通内容".encode("utf-8")))
            secret = root / "secret.txt"
            secret.write_text("token=secret-value", encoding="utf-8")
            with self.assertRaises(PermanentMediaError):
                prepare_outbound_text_file(secret, root)
            outside = root.parent / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            try:
                with self.assertRaises(PermanentMediaError):
                    prepare_outbound_text_file(outside, root)
            finally:
                outside.unlink(missing_ok=True)

    def test_message_model_parses_file_fields(self) -> None:
        message = WeixinMessage.from_dict({
            "from_user_id": "wxid_test_owner",
            "item_list": [{
                "type": 4,
                "file_item": {
                    "file_name": "notes.txt",
                    "len": "12",
                    "media": {
                        "encrypt_query_param": "query",
                        "aes_key": base64.b64encode(KEY).decode("ascii"),
                    },
                },
            }],
        })

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(len(message.files), 1)
        self.assertEqual(message.files[0].file_name, "notes.txt")
        self.assertEqual(message.files[0].length, "12")

    def test_text_file_manager_downloads_validates_and_cleans(self) -> None:
        content = "第一行\n第二行".encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            manager = InboundTextFileManager(
                Path(directory) / "files",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, content=encrypt(content)),
                ),
            )
            prepared = manager.prepare(file_message(
                content_length=str(len(content)),
            ))

            self.assertEqual(prepared.display_name, "notes.txt")
            self.assertEqual(manager.read_text(prepared), "第一行\n第二行")
            self.assertNotEqual(prepared.path.name, prepared.display_name)
            manager.cleanup(prepared)
            manager.close()

            self.assertFalse(prepared.path.exists())

    def test_text_file_manager_rejects_unsafe_or_non_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = InboundTextFileManager(
                Path(directory),
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, content=encrypt(b"\x00binary")),
                ),
            )
            for name in ("..\\secret.txt", "payload.exe", "CON.txt"):
                with self.subTest(name=name), self.assertRaises(PermanentMediaError):
                    manager.prepare(file_message(name=name))
            with self.assertRaises(PermanentMediaError):
                manager.prepare(file_message())
            manager.close()

    def test_message_model_parses_image_fields(self) -> None:
        message = WeixinMessage.from_dict({
            "from_user_id": "wxid_test_owner",
            "message_type": 1,
            "item_list": [{
                "type": 2,
                "image_item": {
                    "aeskey": KEY.hex(),
                    "mid_size": 123,
                    "media": {
                        "encrypt_query_param": "query",
                        "aes_key": base64.b64encode(KEY).decode("ascii"),
                    },
                },
            }],
        })

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(len(message.images), 1)
        self.assertEqual(message.images[0].mid_size, 123)
        self.assertEqual(parse_aes_key(message.images[0]), KEY)

    def test_parse_aes_key_accepts_official_encodings(self) -> None:
        raw = ImageItem(media=CDNMedia(
            aes_key=base64.b64encode(KEY).decode("ascii"),
        ))
        encoded_hex = ImageItem(media=CDNMedia(
            aes_key=base64.b64encode(KEY.hex().encode("ascii")).decode("ascii"),
        ))

        self.assertEqual(parse_aes_key(raw), KEY)
        self.assertEqual(parse_aes_key(encoded_hex), KEY)
        with self.assertRaises(PermanentMediaError):
            parse_aes_key(ImageItem(aeskey="not-a-key"))

    def test_decrypts_aes_ecb_pkcs7_and_checks_format(self) -> None:
        decrypted = decrypt_aes_ecb(encrypt(PNG), KEY)

        self.assertEqual(decrypted, PNG)
        self.assertEqual(image_extension(decrypted), ".png")
        with self.assertRaises(PermanentMediaError):
            decrypt_aes_ecb(encrypt(PNG), b"short")
        with self.assertRaises(PermanentMediaError):
            image_extension(b"not-an-image")

    def test_manager_downloads_decrypts_and_cleans_only_owned_file(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=encrypt(PNG))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "inbound"
            manager = InboundImageManager(
                root, transport=httpx.MockTransport(handler),
            )
            path = manager.prepare(image_message())
            assert path is not None
            unrelated = path.parent / "user-file.txt"
            unrelated.write_text("keep", encoding="utf-8")

            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(
                requests[0].url.params["encrypted_query_param"],
                "a+b/c=",
            )
            self.assertTrue(Path(f"{path}.wxbot-media").exists())
            manager.validate(path)
            path.write_bytes(PNG + b"changed")
            with self.assertRaises(PermanentMediaError):
                manager.validate(path)
            manager.cleanup(path)
            manager.close()

            self.assertFalse(path.exists())
            self.assertFalse(Path(f"{path}.wxbot-media").exists())
            self.assertTrue(unrelated.exists())

    def test_stale_cleanup_requires_owned_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "inbound"
            folder = root / "message"
            folder.mkdir(parents=True)
            owned = folder / "owned.png"
            owned.write_bytes(PNG)
            marker = Path(f"{owned}.wxbot-media")
            marker.touch()
            unrelated = folder / "unrelated.png"
            unrelated.write_bytes(PNG)
            os.utime(marker, (0, 0))
            os.utime(owned, (0, 0))
            os.utime(unrelated, (0, 0))

            manager = InboundImageManager(
                root, transport=httpx.MockTransport(
                    lambda _request: httpx.Response(500),
                ),
                now=lambda: 100_000,
            )
            manager.close()

            self.assertFalse(owned.exists())
            self.assertTrue(unrelated.exists())

    def test_manager_rejects_untrusted_url_and_classifies_server_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = InboundImageManager(
                Path(directory),
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(503),
                ),
            )
            with self.assertRaises(PermanentMediaError):
                manager.prepare(image_message(media=CDNMedia(
                    full_url="https://example.com/image",
                )))
            with self.assertRaises(TransientMediaError):
                manager.prepare(image_message())
            manager.close()
