from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wxbot.api.models import CDNMedia, FileItem, ImageItem, WeixinMessage


CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_HOST = "novac2c.cdn.weixin.qq.com"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_AUDIO_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_FILE_CHARS = 200_000
TEXT_FILE_EXTENSIONS = {
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".diff", ".patch",
}
AUDIO_FILE_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".silk"}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
SENSITIVE_FILE_NAMES = {
    ".env", ".env.local", ".env.production", "credentials.json", "session.json",
}
SENSITIVE_FILE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SENSITIVE_FILE_PARTS = {
    ".git", "data", "tmp", ".codex", ".ssh", ".gnupg",
}


class PermanentMediaError(RuntimeError):
    pass


class TransientMediaError(RuntimeError):
    pass


class VoiceProbeStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, message: WeixinMessage) -> None:
        voice_items = [item for item in message.items if item.type == 3]
        voices = message.voices
        if not voice_items:
            return
        voice = voices[0] if voices else None
        text = voice.text.strip() if voice is not None and voice.text else ""
        data = {
            "observed_at": int(time.time()),
            "item_types": [item.type for item in message.items],
            "voice_count": len(voice_items),
            "has_voice_item": bool(voices),
            "has_text": bool(text),
            "text_length": len(text),
            "encode_type": voice.encode_type if voice is not None else None,
            "sample_rate": voice.sample_rate if voice is not None else None,
            "playtime": voice.playtime if voice is not None else None,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)


def parse_aes_key(image: ImageItem) -> bytes:
    if image.aeskey is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", image.aeskey):
            raise PermanentMediaError("invalid-image-key")
        return bytes.fromhex(image.aeskey)
    encoded = image.media.aes_key if image.media is not None else None
    if not encoded:
        raise PermanentMediaError("missing-image-key")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PermanentMediaError("invalid-image-key") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise PermanentMediaError("invalid-image-key")


def parse_file_aes_key(file: FileItem) -> bytes:
    return _parse_media_aes_key(file.media, "file")


def _parse_media_aes_key(media: CDNMedia | None, label: str) -> bytes:
    encoded = media.aes_key if media is not None else None
    if not encoded:
        raise PermanentMediaError(f"missing-{label}-key")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PermanentMediaError(f"invalid-{label}-key") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise PermanentMediaError(f"invalid-{label}-key")


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise PermanentMediaError("invalid-image-ciphertext")
    if len(key) != 16:
        raise PermanentMediaError("invalid-image-key")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    padding = padded[-1]
    if padding < 1 or padding > 16 or padded[-padding:] != bytes([padding]) * padding:
        raise PermanentMediaError("invalid-image-padding")
    plaintext = padded[:-padding]
    if not plaintext or len(plaintext) > MAX_IMAGE_BYTES:
        raise PermanentMediaError("invalid-image-size")
    return plaintext


def image_extension(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    raise PermanentMediaError("unsupported-image-format")


@dataclass(frozen=True)
class InboundTextFile:
    path: Path
    display_name: str


@dataclass(frozen=True)
class OutboundTextFile:
    path: Path
    display_name: str
    sha256: str
    size: int


def prepare_outbound_text_file(path: Path, project_root: Path) -> OutboundTextFile:
    import hashlib

    root = project_root.resolve()
    original = path.absolute()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise PermanentMediaError("outbound-file-outside-project") from exc
    current = root
    has_link = False
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            has_link = True
            break
    if (
        original.is_symlink()
        or has_link
        or resolved.is_symlink()
        or not resolved.is_file()
        or any(part.lower() in SENSITIVE_FILE_PARTS for part in relative.parts)
        or resolved.name.lower() in SENSITIVE_FILE_NAMES
        or resolved.suffix.lower() in SENSITIVE_FILE_SUFFIXES
        or resolved.suffix.lower() not in TEXT_FILE_EXTENSIONS
    ):
        raise PermanentMediaError("outbound-file-not-allowed")
    content = resolved.read_bytes()
    if not content or len(content) > MAX_TEXT_FILE_BYTES or b"\x00" in content:
        raise PermanentMediaError("outbound-file-invalid-size-or-content")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PermanentMediaError("outbound-file-not-utf8") from exc
    sensitive_patterns = (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"(?i)\b(?:password|passwd|token|secret|context_token)\s*[:=]\s*\S+",
        r"(?i)\bwxid_[A-Za-z0-9_-]+\b",
    )
    if any(re.search(pattern, text) for pattern in sensitive_patterns):
        raise PermanentMediaError("outbound-file-sensitive-content")
    return OutboundTextFile(
        path=resolved,
        display_name=resolved.name,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )


def prepare_outbound_audio_file(path: Path, project_root: Path) -> OutboundTextFile:
    import hashlib

    root = project_root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise PermanentMediaError("outbound-audio-outside-project") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PermanentMediaError("outbound-audio-not-allowed")
    if (
        path.absolute().is_symlink()
        or resolved.is_symlink()
        or not resolved.is_file()
        or any(part.lower() in SENSITIVE_FILE_PARTS for part in relative.parts)
        or resolved.name.lower() in SENSITIVE_FILE_NAMES
        or resolved.suffix.lower() in SENSITIVE_FILE_SUFFIXES
        or resolved.suffix.lower() not in AUDIO_FILE_EXTENSIONS
    ):
        raise PermanentMediaError("outbound-audio-not-allowed")
    content = resolved.read_bytes()
    if not content or len(content) > MAX_AUDIO_FILE_BYTES:
        raise PermanentMediaError("outbound-audio-invalid-size")
    return OutboundTextFile(
        path=resolved,
        display_name=resolved.name,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )


def _safe_file_name(value: str | None) -> str:
    if not value or value != Path(value).name or "/" in value or "\\" in value:
        raise PermanentMediaError("invalid-file-name")
    if any(ord(character) < 32 for character in value) or value.endswith((" ", ".")):
        raise PermanentMediaError("invalid-file-name")
    if ":" in value or Path(value).stem.upper() in WINDOWS_RESERVED_NAMES:
        raise PermanentMediaError("invalid-file-name")
    if Path(value).suffix.lower() not in TEXT_FILE_EXTENSIONS:
        raise PermanentMediaError("unsupported-file-type")
    return value


def _decrypt_text_file(ciphertext: bytes, key: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise PermanentMediaError("invalid-file-ciphertext")
    if len(key) != 16:
        raise PermanentMediaError("invalid-file-key")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    padding = padded[-1]
    if padding < 1 or padding > 16 or padded[-padding:] != bytes([padding]) * padding:
        raise PermanentMediaError("invalid-file-padding")
    plaintext = padded[:-padding]
    if not plaintext or len(plaintext) > MAX_TEXT_FILE_BYTES:
        raise PermanentMediaError("invalid-file-size")
    return plaintext


class InboundTextFileManager:
    def __init__(
        self,
        root: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = root.resolve()
        self._http = httpx.Client(transport=transport, follow_redirects=False)
        self._now = now
        self._cleanup_stale()

    def close(self) -> None:
        self._http.close()

    def prepare(self, message: WeixinMessage) -> InboundTextFile:
        if len(message.files) != 1:
            raise PermanentMediaError("multiple-files-unsupported")
        file = message.files[0]
        display_name = _safe_file_name(file.file_name)
        media = file.media
        if media is None or (not media.full_url and not media.encrypt_query_param):
            raise PermanentMediaError("missing-file-reference")
        url = InboundImageManager._download_url(
            media.full_url, media.encrypt_query_param,
        )
        ciphertext = self._download(url)
        plaintext = _decrypt_text_file(ciphertext, parse_file_aes_key(file))
        if file.length is not None:
            if not file.length.isdecimal() or int(file.length) != len(plaintext):
                raise PermanentMediaError("invalid-file-size")
        if b"\x00" in plaintext:
            raise PermanentMediaError("unsupported-file-content")
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PermanentMediaError("unsupported-file-encoding") from exc
        if len(text) > MAX_TEXT_FILE_CHARS:
            raise PermanentMediaError("file-text-too-large")
        digest_dir = self.root / _safe_message_digest(message)
        digest_dir.mkdir(parents=True, exist_ok=True)
        path = (digest_dir / f"{secrets.token_hex(12)}.txt").resolve()
        if not path.is_relative_to(self.root) or path.is_symlink():
            raise PermanentMediaError("invalid-file-path")
        marker = self._marker_path(path)
        try:
            marker.write_text(str(len(plaintext)), encoding="ascii")
            with path.open("xb") as stream:
                stream.write(plaintext)
        except OSError as exc:
            self.cleanup(InboundTextFile(path, display_name))
            raise TransientMediaError("file-storage-temporary") from exc
        return InboundTextFile(path, display_name)

    def read_text(self, prepared: InboundTextFile) -> str:
        self.validate(prepared)
        try:
            return prepared.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PermanentMediaError("invalid-file-content") from exc

    def validate(self, prepared: InboundTextFile) -> None:
        path = prepared.path.resolve()
        marker = self._marker_path(path)
        if (
            not path.is_relative_to(self.root)
            or path.is_symlink()
            or not path.is_file()
            or marker.is_symlink()
            or not marker.is_file()
        ):
            raise PermanentMediaError("invalid-file-path")
        try:
            expected_size = int(marker.read_text(encoding="ascii"))
            actual_size = path.stat().st_size
        except (OSError, ValueError) as exc:
            raise PermanentMediaError("invalid-file-path") from exc
        if expected_size < 1 or expected_size > MAX_TEXT_FILE_BYTES or actual_size != expected_size:
            raise PermanentMediaError("invalid-file-size")

    def cleanup(self, prepared: InboundTextFile | None) -> None:
        if prepared is None:
            return
        path = prepared.path.resolve()
        if not path.is_relative_to(self.root) or path.is_symlink():
            return
        try:
            path.unlink(missing_ok=True)
            self._marker_path(path).unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            pass

    def _download(self, url: str) -> bytes:
        try:
            with self._http.stream("GET", url, timeout=httpx.Timeout(15.0)) as response:
                if response.status_code >= 500:
                    raise TransientMediaError("file-cdn-temporary")
                if response.status_code != 200:
                    raise PermanentMediaError("file-cdn-rejected")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_TEXT_FILE_BYTES + 16:
                        raise PermanentMediaError("file-too-large")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.RequestError as exc:
            raise TransientMediaError("file-cdn-temporary") from exc

    def _cleanup_stale(self) -> None:
        if not self.root.exists():
            return
        cutoff = self._now() - 24 * 60 * 60
        for marker in self.root.glob("*/*.wxbot-file"):
            try:
                if marker.is_file() and not marker.is_symlink() and marker.stat().st_mtime < cutoff:
                    path = Path(str(marker)[:-len(".wxbot-file")])
                    self.cleanup(InboundTextFile(path, "stale.txt"))
            except OSError:
                continue

    @staticmethod
    def _marker_path(path: Path) -> Path:
        return Path(f"{path}.wxbot-file")


class InboundImageManager:
    def __init__(
        self,
        root: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = root.resolve()
        self._http = httpx.Client(transport=transport, follow_redirects=False)
        self._now = now
        self._cleanup_stale()

    def close(self) -> None:
        self._http.close()

    def prepare(self, message: WeixinMessage) -> Path | None:
        if not message.images:
            return None
        if len(message.images) != 1:
            raise PermanentMediaError("multiple-images-unsupported")
        image = message.images[0]
        media = image.media
        if media is None or (not media.full_url and not media.encrypt_query_param):
            raise PermanentMediaError("missing-image-reference")
        url = self._download_url(media.full_url, media.encrypt_query_param)
        ciphertext = self._download(url)
        plaintext = decrypt_aes_ecb(ciphertext, parse_aes_key(image))
        extension = image_extension(plaintext)
        digest_dir = self.root / _safe_message_digest(message)
        digest_dir.mkdir(parents=True, exist_ok=True)
        path = digest_dir / f"{secrets.token_hex(12)}{extension}"
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved.is_symlink():
            raise PermanentMediaError("invalid-image-path")
        marker = self._marker_path(resolved)
        try:
            marker.write_text(str(len(plaintext)), encoding="ascii")
            with resolved.open("xb") as stream:
                stream.write(plaintext)
        except OSError as exc:
            self.cleanup(resolved)
            raise TransientMediaError("image-storage-temporary") from exc
        return resolved

    def validate(self, path: Path) -> None:
        resolved = path.resolve()
        marker = self._marker_path(resolved)
        if (
            not resolved.is_relative_to(self.root)
            or resolved.is_symlink()
            or not resolved.is_file()
            or marker.is_symlink()
            or not marker.is_file()
        ):
            raise PermanentMediaError("invalid-image-path")
        try:
            expected_size = int(marker.read_text(encoding="ascii"))
            actual_size = resolved.stat().st_size
        except (OSError, ValueError) as exc:
            raise PermanentMediaError("invalid-image-path") from exc
        if (
            expected_size < 1
            or expected_size > MAX_IMAGE_BYTES
            or actual_size != expected_size
        ):
            raise PermanentMediaError("invalid-image-size")

    def cleanup(self, path: Path | None) -> None:
        if path is None:
            return
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved.is_symlink():
            return
        try:
            resolved.unlink(missing_ok=True)
            self._marker_path(resolved).unlink(missing_ok=True)
            resolved.parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def _download_url(full_url: str | None, encrypted_query_param: str | None) -> str:
        if full_url:
            parsed = urlparse(full_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != CDN_HOST
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise PermanentMediaError("untrusted-image-url")
            return full_url
        if not encrypted_query_param:
            raise PermanentMediaError("missing-image-reference")
        return (
            f"{CDN_BASE_URL}/download?encrypted_query_param="
            f"{quote(encrypted_query_param, safe='')}"
        )

    def _download(self, url: str) -> bytes:
        try:
            with self._http.stream("GET", url, timeout=httpx.Timeout(15.0)) as response:
                if response.status_code >= 500:
                    raise TransientMediaError("image-cdn-temporary")
                if response.status_code != 200:
                    raise PermanentMediaError("image-cdn-rejected")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise PermanentMediaError("image-too-large")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.RequestError as exc:
            raise TransientMediaError("image-cdn-temporary") from exc

    def _cleanup_stale(self) -> None:
        if not self.root.exists():
            return
        cutoff = self._now() - 24 * 60 * 60
        for marker in self.root.glob("*/*.wxbot-media"):
            try:
                if (
                    marker.is_file()
                    and not marker.is_symlink()
                    and marker.stat().st_mtime < cutoff
                ):
                    image_path = Path(str(marker)[:-len(".wxbot-media")])
                    self.cleanup(image_path)
            except OSError:
                continue

    @staticmethod
    def _marker_path(path: Path) -> Path:
        return Path(f"{path}.wxbot-media")


def _safe_message_digest(message: WeixinMessage) -> str:
    import hashlib

    material = f"{message.message_id or ''}\x1f{message.client_id or ''}\x1f{message.create_time_ms or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
