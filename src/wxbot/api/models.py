from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class CDNMedia:
    encrypt_query_param: str | None = None
    aes_key: str | None = None
    full_url: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> "CDNMedia | None":
        if not isinstance(value, dict):
            return None
        return cls(
            encrypt_query_param=_optional_str(value.get("encrypt_query_param")),
            aes_key=_optional_str(value.get("aes_key")),
            full_url=_optional_str(value.get("full_url")),
        )


@dataclass(frozen=True)
class ImageItem:
    media: CDNMedia | None = None
    aeskey: str | None = None
    mid_size: int | None = None

    @classmethod
    def from_dict(cls, value: object) -> "ImageItem | None":
        if not isinstance(value, dict):
            return None
        return cls(
            media=CDNMedia.from_dict(value.get("media")),
            aeskey=_optional_str(value.get("aeskey")),
            mid_size=_optional_int(value.get("mid_size")),
        )


@dataclass(frozen=True)
class FileItem:
    media: CDNMedia | None = None
    file_name: str | None = None
    md5: str | None = None
    length: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> "FileItem | None":
        if not isinstance(value, dict):
            return None
        return cls(
            media=CDNMedia.from_dict(value.get("media")),
            file_name=_optional_str(value.get("file_name")),
            md5=_optional_str(value.get("md5")),
            length=_optional_str(value.get("len")),
        )


@dataclass(frozen=True)
class VoiceItem:
    media: CDNMedia | None = None
    text: str | None = None
    encode_type: int | None = None
    sample_rate: int | None = None
    playtime: int | None = None

    @classmethod
    def from_dict(cls, value: object) -> "VoiceItem | None":
        if not isinstance(value, dict):
            return None
        return cls(
            media=CDNMedia.from_dict(value.get("media")),
            text=_optional_str(value.get("text")),
            encode_type=_optional_int(value.get("encode_type")),
            sample_rate=_optional_int(value.get("sample_rate")),
            playtime=_optional_int(value.get("playtime")),
        )


@dataclass(frozen=True)
class MessageItem:
    type: int | None = None
    msg_id: str | None = None
    text: str | None = None
    image: ImageItem | None = None
    file: FileItem | None = None
    voice: VoiceItem | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MessageItem":
        text_item = value.get("text_item")
        text = (
            _optional_str(text_item.get("text"))
            if isinstance(text_item, dict)
            else None
        )
        return cls(
            type=_optional_int(value.get("type")),
            msg_id=_optional_str(value.get("msg_id")),
            text=text,
            image=ImageItem.from_dict(value.get("image_item")),
            file=FileItem.from_dict(value.get("file_item")),
            voice=VoiceItem.from_dict(value.get("voice_item")),
        )


@dataclass(frozen=True)
class WeixinMessage:
    from_user_id: str
    to_user_id: str | None
    context_token: str | None
    message_type: int | None
    message_id: int | None
    client_id: str | None
    create_time_ms: int | None
    items: tuple[MessageItem, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WeixinMessage | None":
        from_user_id = value.get("from_user_id")
        if not isinstance(from_user_id, str) or not from_user_id:
            return None
        raw_items = value.get("item_list")
        items = tuple(
            MessageItem.from_dict(item)
            for item in raw_items or []
            if isinstance(item, dict)
        )
        return cls(
            from_user_id=from_user_id,
            to_user_id=_optional_str(value.get("to_user_id")),
            context_token=_optional_str(value.get("context_token")),
            message_type=_optional_int(value.get("message_type")),
            message_id=_optional_int(value.get("message_id")),
            client_id=_optional_str(value.get("client_id")),
            create_time_ms=_optional_int(value.get("create_time_ms")),
            items=items,
        )

    @property
    def text(self) -> str | None:
        texts = [item.text for item in self.items if item.type == 1 and item.text]
        return "\n".join(texts) if texts else None

    @property
    def images(self) -> tuple[ImageItem, ...]:
        return tuple(
            item.image for item in self.items
            if item.type == 2 and item.image is not None
        )

    @property
    def files(self) -> tuple[FileItem, ...]:
        return tuple(
            item.file for item in self.items
            if item.type == 4 and item.file is not None
        )

    @property
    def voices(self) -> tuple[VoiceItem, ...]:
        return tuple(
            item.voice for item in self.items
            if item.type == 3 and item.voice is not None
        )


@dataclass(frozen=True)
class Updates:
    messages: tuple[WeixinMessage, ...]
    cursor: str
    timeout_ms: int
