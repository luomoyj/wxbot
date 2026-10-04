from __future__ import annotations

import base64
import hashlib
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wxbot import __version__
from wxbot.api.models import Updates, WeixinMessage

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_HOST = "novac2c.cdn.weixin.qq.com"
MAX_OUTBOUND_FILE_BYTES = 20 * 1024 * 1024


class ILinkError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def random_wechat_uin() -> str:
    value = secrets.randbits(32)
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def encoded_client_version(version: str = __version__) -> int:
    parts = version.split(".")
    numbers = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    numbers.extend([0] * (3 - len(numbers)))
    major, minor, patch = (number & 0xFF for number in numbers)
    return (major << 16) | (minor << 8) | patch


class ILinkClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = httpx.Client(transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ILinkClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def base_info() -> dict[str, str]:
        return {"channel_version": __version__, "bot_agent": f"wxbot/{__version__}"}

    @staticmethod
    def common_headers() -> dict[str, str]:
        return {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(encoded_client_version()),
        }

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_wechat_uin(),
            **self.common_headers(),
        }
        if self.token and self.token.strip():
            headers["Authorization"] = f"Bearer {self.token.strip()}"
        return headers

    def _url(self, endpoint: str) -> str:
        return urljoin(f"{self.base_url}/", endpoint)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = 15.0,
        business_headers: bool = True,
    ) -> dict[str, Any]:
        try:
            response = self._http.request(
                method,
                self._url(endpoint),
                headers=self.headers() if business_headers else self.common_headers(),
                json=body,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            raise
        except httpx.RequestError as exc:
            raise ILinkError("iLink 网络请求失败") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ILinkError(f"iLink HTTP {response.status_code}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ILinkError("iLink 返回了无效 JSON") from exc
        if not isinstance(data, dict):
            raise ILinkError("iLink 返回结构不是对象")
        return data

    def create_qrcode(self, local_tokens: list[str] | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "ilink/bot/get_bot_qrcode?bot_type=3",
            body={"local_token_list": local_tokens or []},
            timeout=None,
        )

    def qrcode_status(self, qrcode: str, verify_code: str | None = None) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        try:
            return self._request(
                "GET", endpoint, timeout=35.0, business_headers=False,
            )
        except httpx.TimeoutException:
            return {"status": "wait"}

    def get_updates(self, cursor: str, timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS) -> Updates:
        try:
            data = self._request(
                "POST",
                "ilink/bot/getupdates",
                body={"get_updates_buf": cursor, "base_info": self.base_info()},
                timeout=(timeout_ms + 5_000) / 1000,
            )
        except httpx.TimeoutException:
            return Updates(messages=(), cursor=cursor, timeout_ms=timeout_ms)
        code = data.get("errcode") or data.get("ret")
        if code not in (None, 0):
            raise ILinkError(str(data.get("errmsg") or f"iLink error {code}"), code=code)
        parsed = []
        for raw in data.get("msgs") or []:
            if isinstance(raw, dict):
                message = WeixinMessage.from_dict(raw)
                if message is not None:
                    parsed.append(message)
        next_cursor = data.get("get_updates_buf", cursor)
        if not isinstance(next_cursor, str):
            next_cursor = cursor
        return Updates(
            messages=tuple(parsed),
            cursor=next_cursor,
            timeout_ms=data.get("longpolling_timeout_ms") or timeout_ms,
        )

    def get_config(self, user_id: str, context_token: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"ilink_user_id": user_id, "base_info": self.base_info()}
        if context_token:
            body["context_token"] = context_token
        return self._request("POST", "ilink/bot/getconfig", body=body, timeout=10.0)

    def send_typing(self, *, to: str, typing_ticket: str, status: int) -> None:
        body = {
            "ilink_user_id": to,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": self.base_info(),
        }
        data = self._request("POST", "ilink/bot/sendtyping", body=body, timeout=10.0)
        code = data.get("errcode") or data.get("ret")
        if code not in (None, 0):
            raise ILinkError(str(data.get("errmsg") or f"iLink error {code}"), code=code)

    def send_text(self, *, to: str, text: str, context_token: str, client_id: str) -> None:
        if not context_token:
            raise ValueError("缺少 context_token，拒绝发送")
        if not text:
            raise ValueError("回复内容不能为空")
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
                "context_token": context_token,
            },
            "base_info": self.base_info(),
        }
        data = self._request("POST", "ilink/bot/sendmessage", body=body)
        code = data.get("errcode") or data.get("ret")
        if code not in (None, 0):
            raise ILinkError(str(data.get("errmsg") or f"iLink error {code}"), code=code)

    def send_text_file(
        self,
        *,
        to: str,
        path: Path,
        file_name: str,
        expected_sha256: str,
        context_token: str,
        client_id: str,
    ) -> None:
        if not context_token:
            raise ValueError("缺少 context_token，拒绝发送")
        plaintext = path.read_bytes()
        if hashlib.sha256(plaintext).hexdigest() != expected_sha256:
            raise ValueError("文件发送前已发生变化")
        if not plaintext or len(plaintext) > MAX_OUTBOUND_FILE_BYTES:
            raise ValueError("文件大小不在允许范围内")
        key = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        padding = 16 - len(plaintext) % 16
        padded = plaintext + bytes([padding]) * padding
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        upload = self._request(
            "POST",
            "ilink/bot/getuploadurl",
            body={
                "filekey": filekey,
                "media_type": 3,
                "to_user_id": to,
                "rawsize": len(plaintext),
                "rawfilemd5": hashlib.md5(plaintext).hexdigest(),
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": key.hex(),
                "base_info": self.base_info(),
            },
        )
        code = upload.get("errcode") or upload.get("ret")
        if code not in (None, 0):
            raise ILinkError(
                str(upload.get("errmsg") or f"iLink error {code}"), code=code,
            )
        upload_url = self._upload_url(
            upload.get("upload_full_url"), upload.get("upload_param"), filekey,
        )
        try:
            response = self._http.post(
                upload_url,
                content=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
                timeout=15.0,
            )
        except httpx.TimeoutException as exc:
            raise ILinkError("文件上传结果不确定") from exc
        except httpx.RequestError as exc:
            raise ILinkError("文件上传网络请求失败") from exc
        if response.status_code != 200:
            raise ILinkError(f"文件上传 HTTP {response.status_code}")
        download_param = response.headers.get("x-encrypted-param", "").strip()
        if not download_param:
            raise ILinkError("文件上传响应缺少下载参数")
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": [{
                    "type": 4,
                    "file_item": {
                        "media": {
                            "encrypt_query_param": download_param,
                            "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
                            "encrypt_type": 1,
                        },
                        "file_name": file_name,
                        "len": str(len(plaintext)),
                    },
                }],
                "context_token": context_token,
            },
            "base_info": self.base_info(),
        }
        data = self._request("POST", "ilink/bot/sendmessage", body=body)
        code = data.get("errcode") or data.get("ret")
        if code not in (None, 0):
            raise ILinkError(str(data.get("errmsg") or f"iLink error {code}"), code=code)

    @staticmethod
    def _upload_url(
        full_url: object, upload_param: object, filekey: str,
    ) -> str:
        if isinstance(full_url, str) and full_url.strip():
            url = full_url.strip()
        elif isinstance(upload_param, str) and upload_param:
            query = urlencode({
                "encrypted_query_param": upload_param,
                "filekey": filekey,
            })
            url = f"{CDN_BASE_URL}/upload?{query}"
        else:
            raise ILinkError("文件上传地址缺失")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != CDN_HOST
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ILinkError("文件上传地址不可信")
        return url
