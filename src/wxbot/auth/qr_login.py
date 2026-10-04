from __future__ import annotations

import time
from collections.abc import Callable

import qrcode

from wxbot.api.client import DEFAULT_BASE_URL, ILinkClient, ILinkError
from wxbot.storage.session_store import SessionState


def display_qrcode(content: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def login(
    *,
    display: Callable[[str], None] = display_qrcode,
    read_verify_code: Callable[[str], str] = input,
    max_seconds: int = 300,
    existing_state: SessionState | None = None,
) -> SessionState:
    current_base_url = DEFAULT_BASE_URL
    started_at = time.monotonic()
    with ILinkClient(base_url=current_base_url) as client:
        local_tokens = [existing_state.bot_token] if existing_state is not None else []
        result = client.create_qrcode(local_tokens)
        qrcode_id = result.get("qrcode")
        qrcode_content = result.get("qrcode_img_content")
        if not isinstance(qrcode_id, str) or not qrcode_id:
            raise ILinkError("二维码响应缺少 qrcode")
        if not isinstance(qrcode_content, str) or not qrcode_content:
            raise ILinkError("二维码响应缺少 qrcode_img_content")
        display(qrcode_content)
        verify_code: str | None = None
        qrcode_count = 1
        while time.monotonic() - started_at < max_seconds:
            status = client.qrcode_status(qrcode_id, verify_code)
            state = status.get("status")
            verify_code = None
            if state in ("wait", "scaned"):
                continue
            if state == "binded_redirect":
                if existing_state is not None:
                    return existing_state
                raise ILinkError("该微信 Bot已连接，但本机没有可复用的登录凭证")
            if state == "scaned_but_redirect":
                redirect_host = status.get("redirect_host")
                if not isinstance(redirect_host, str) or not redirect_host:
                    raise ILinkError("扫码重定向响应缺少 redirect_host")
                redirect_host = redirect_host.strip()
                if "://" in redirect_host:
                    if not redirect_host.startswith("https://"):
                        raise ILinkError("扫码重定向地址不是 HTTPS")
                    redirect_host = redirect_host.removeprefix("https://")
                client.base_url = f"https://{redirect_host.rstrip('/')}"
                continue
            if state == "need_verifycode":
                verify_code = read_verify_code("请输入微信显示的配对码：").strip()
                if not verify_code:
                    raise ILinkError("配对码不能为空")
                continue
            if state in ("expired", "verify_code_blocked"):
                qrcode_count += 1
                if qrcode_count > 3:
                    raise ILinkError("二维码多次失效，连接流程已停止")
                result = client.create_qrcode(local_tokens)
                qrcode_id = result.get("qrcode")
                qrcode_content = result.get("qrcode_img_content")
                if not isinstance(qrcode_id, str) or not qrcode_id:
                    raise ILinkError("二维码响应缺少 qrcode")
                if not isinstance(qrcode_content, str) or not qrcode_content:
                    raise ILinkError("二维码响应缺少 qrcode_img_content")
                verify_code = None
                display(qrcode_content)
                continue
            if state == "confirmed":
                token = status.get("bot_token")
                bot_id = status.get("ilink_bot_id")
                base_url = status.get("baseurl") or client.base_url
                if not all(isinstance(value, str) and value for value in (token, bot_id, base_url)):
                    raise ILinkError("登录确认响应缺少凭证字段")
                return SessionState.create(
                    bot_token=token,
                    bot_id=bot_id,
                    base_url=base_url,
                    ilink_user_id=status.get("ilink_user_id"),
                )
            raise ILinkError(f"未知扫码状态：{state!r}")
    raise ILinkError("二维码登录超过 5 分钟，请重新登录")
