from __future__ import annotations

import queue
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from wxbot.api.models import WeixinMessage
from wxbot.message.auto_reply import AutoReplyStore
from wxbot.message.inbox import InboxStore
from wxbot.message.media import (
    InboundImageManager,
    InboundTextFile,
    InboundTextFileManager,
    PermanentMediaError,
    TransientMediaError,
    VoiceProbeStore,
)
from wxbot.message.poller import message_key
from wxbot.message.commands import parse_command
from wxbot.notices import timed_terminal_notice
from wxbot.replies import AppServerReplyGenerator


MessageHandler = Callable[..., bool]
IMAGE_TEXT_MERGE_SECONDS = 1.5
TEXT_BURST_MERGE_SECONDS = 2.0


@dataclass
class _PendingImage:
    image_message: WeixinMessage
    local_image: Path | None
    inbox_keys: tuple[str, ...]
    text_message: WeixinMessage | None = None
    timer: threading.Timer | None = None


@dataclass
class _PendingText:
    message: WeixinMessage
    inbox_keys: tuple[str, ...]
    timer: threading.Timer | None = None


class MessageDispatcher:
    def __init__(
        self, *, handler: MessageHandler, generator: AppServerReplyGenerator,
        inbox: InboxStore | None = None,
        image_manager: InboundImageManager | None = None,
        file_manager: InboundTextFileManager | None = None,
        voice_probe: VoiceProbeStore | None = None,
        auto_reply_store: AutoReplyStore | None = None,
        image_text_merge_seconds: float = IMAGE_TEXT_MERGE_SECONDS,
        text_merge_seconds: float = TEXT_BURST_MERGE_SECONDS,
    ) -> None:
        self.handler = handler
        self.generator = generator
        self.inbox = inbox
        self.image_manager = image_manager
        self.file_manager = file_manager
        self.voice_probe = voice_probe
        self.auto_reply_store = auto_reply_store
        self.image_text_merge_seconds = image_text_merge_seconds
        self.text_merge_seconds = text_merge_seconds
        self._pending_lock = threading.RLock()
        self._pending_images: dict[
            tuple[str, str | None], _PendingImage
        ] = {}
        self._pending_texts: dict[
            tuple[str, str | None], _PendingText
        ] = {}
        self._queue: queue.Queue[
            tuple[
                WeixinMessage, str | None, Path | None, InboundTextFile | None,
                tuple[str, ...]
            ] | None
        ] = queue.Queue()
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="wxbot-message-worker", daemon=True
        )
        self._thread.start()

    def submit(self, message: WeixinMessage) -> None:
        if (
            any(item.type == 3 for item in message.items)
            and self.voice_probe is not None
            and self.auto_reply_store is not None
            and self.auto_reply_store.load().allowed_user_id == message.from_user_id
        ):
            self.voice_probe.record(message)
        try:
            if message.images and message.files:
                raise PermanentMediaError("mixed-media-unsupported")
            local_image = self._prepare_image(message)
            local_file = self._prepare_file(message)
        except PermanentMediaError as exc:
            print(timed_terminal_notice(f"媒体消息未处理：{exc}"), file=sys.stderr)
            return
        if (
            (message.images or message.files)
            and local_image is None
            and local_file is None
            and not message.text
        ):
            return
        if self.inbox is not None and not self.inbox.enqueue(message):
            if self.image_manager is not None:
                self.image_manager.cleanup(local_image)
            if self.file_manager is not None:
                self.file_manager.cleanup(local_file)
            return
        self._route_prepared(
            message, local_image, local_file, (message_key(message),),
        )

    def restore(self, messages: list[WeixinMessage]) -> None:
        for message in messages:
            key = message_key(message)
            try:
                if message.images and message.files:
                    raise PermanentMediaError("mixed-media-unsupported")
                local_image = self._prepare_image(message)
                local_file = self._prepare_file(message)
            except PermanentMediaError:
                if self.inbox is not None:
                    self.inbox.complete(key)
                continue
            except TransientMediaError:
                if message.files:
                    threading.Thread(
                        target=self._retry_restored_file,
                        args=(message, key),
                        name="wxbot-file-restore",
                        daemon=True,
                    ).start()
                    continue
                route = self._conversation_key(message)
                self._hold_image(message, None, (key,))
                threading.Thread(
                    target=self._retry_restored_image,
                    args=(message, route, key),
                    name="wxbot-image-restore",
                    daemon=True,
                ).start()
                continue
            if message.text and not message.images and not message.files:
                self._dispatch(message, local_image, local_file, (key,))
            else:
                self._route_prepared(message, local_image, local_file, (key,))

    def _retry_restored_file(self, message: WeixinMessage, key: str) -> None:
        delay = 2
        while not self._closed.wait(delay):
            try:
                local_file = self._prepare_file(message)
            except TransientMediaError:
                delay = min(delay * 2, 30)
                continue
            except PermanentMediaError:
                if self.inbox is not None:
                    self.inbox.complete(key)
                return
            self._dispatch(message, None, local_file, (key,))
            return

    def _prepare_image(self, message: WeixinMessage) -> Path | None:
        if self.image_manager is None or not message.images:
            return None
        if self.auto_reply_store is None:
            return None
        allowed = self.auto_reply_store.load().allowed_user_id
        if allowed is None or allowed != message.from_user_id:
            return None
        return self.image_manager.prepare(message)

    def _prepare_file(self, message: WeixinMessage) -> InboundTextFile | None:
        if self.file_manager is None or not message.files:
            return None
        if self.auto_reply_store is None:
            return None
        allowed = self.auto_reply_store.load().allowed_user_id
        if allowed is None or allowed != message.from_user_id:
            return None
        return self.file_manager.prepare(message)

    def _dispatch(
        self, message: WeixinMessage, local_image: Path | None = None,
        local_file: InboundTextFile | None = None,
        inbox_keys: tuple[str, ...] | None = None,
    ) -> None:
        keys = inbox_keys or (message_key(message),)
        if self.generator.is_immediate_control(message.text or ""):
            threading.Thread(
                target=self._handle,
                args=(message, None, local_image, local_file, keys),
                name="wxbot-immediate-control", daemon=True,
            ).start()
            return
        project = self.generator.begin_analysis()
        self._queue.put((message, project, local_image, local_file, keys))

    def close(self) -> None:
        self._closed.set()
        with self._pending_lock:
            pending = list(self._pending_images.values())
            self._pending_images.clear()
            pending_texts = list(self._pending_texts.values())
            self._pending_texts.clear()
        for item in pending:
            if item.timer is not None:
                item.timer.cancel()
            if self.image_manager is not None:
                self.image_manager.cleanup(item.local_image)
        for item in pending_texts:
            if item.timer is not None:
                item.timer.cancel()
        self._queue.put(None)
        self._thread.join(timeout=3)

    def _retry_restored_image(
        self,
        message: WeixinMessage,
        route: tuple[str, str | None],
        key: str,
    ) -> None:
        delay = 2
        while not self._closed.wait(delay):
            try:
                local_image = self._prepare_image(message)
            except TransientMediaError:
                delay = min(delay * 2, 30)
                continue
            except PermanentMediaError:
                self._fail_pending_image(route, key)
                return
            self._pending_image_ready(route, key, local_image)
            return

    def _route_prepared(
        self,
        message: WeixinMessage,
        local_image: Path | None,
        local_file: InboundTextFile | None,
        inbox_keys: tuple[str, ...],
    ) -> None:
        if message.images and not message.text and local_image is not None:
            self._hold_image(message, local_image, inbox_keys)
            return
        if (
            message.text
            and not message.images
            and not message.files
            and self._attach_text(message, inbox_keys)
        ):
            return
        if message.text and not message.images and not message.files:
            route = self._conversation_key(message)
            if self._is_text_merge_boundary(message.text):
                self._release_pending_text(route)
                self._dispatch(message, local_image, local_file, inbox_keys)
            else:
                self._hold_text(route, message, inbox_keys)
            return
        self._dispatch(message, local_image, local_file, inbox_keys)

    @staticmethod
    def _is_routing_control(message: str) -> bool:
        if parse_command(message) is not None:
            return True
        command = message.strip()
        return bool(
            re.search(r"有哪些项目|列出(?:所有)?项目|项目列表", command)
            or re.fullmatch(
                r"(?:切换到|使用|进入)\s*[A-Za-z0-9_.-]+(?:\s*项目)?[。！？!?]?",
                command,
                re.I,
            )
        )

    def _is_text_merge_boundary(self, message: str) -> bool:
        return bool(
            self.generator.is_immediate_control(message)
            or self._is_routing_control(message)
        )

    def _hold_text(
        self,
        route: tuple[str, str | None],
        message: WeixinMessage,
        inbox_keys: tuple[str, ...],
    ) -> None:
        if self.text_merge_seconds <= 0:
            self._dispatch(message, None, None, inbox_keys)
            return
        with self._pending_lock:
            previous = self._pending_texts.get(route)
            if previous is not None and previous.timer is not None:
                previous.timer.cancel()
            combined = replace(
                message,
                items=(previous.message.items if previous is not None else ())
                + message.items,
            )
            pending = _PendingText(
                combined,
                (previous.inbox_keys if previous is not None else ()) + inbox_keys,
            )
            timer = threading.Timer(
                self.text_merge_seconds,
                self._release_pending_text,
                args=(route,),
            )
            timer.daemon = True
            pending.timer = timer
            self._pending_texts[route] = pending
            timer.start()

    def _release_pending_text(
        self, route: tuple[str, str | None],
    ) -> None:
        with self._pending_lock:
            pending = self._pending_texts.pop(route, None)
            if pending is not None and pending.timer is not None:
                pending.timer.cancel()
                pending.timer = None
        if pending is not None and not self._closed.is_set():
            self._dispatch(pending.message, None, None, pending.inbox_keys)

    def _hold_image(
        self,
        message: WeixinMessage,
        local_image: Path | None,
        inbox_keys: tuple[str, ...],
    ) -> None:
        route = self._conversation_key(message)
        current = _PendingImage(message, local_image, inbox_keys)
        with self._pending_lock:
            previous = self._pending_images.get(route)
            if previous is not None and previous.local_image is None:
                self._dispatch(message, local_image, None, inbox_keys)
                return
            previous = self._pending_images.pop(route, None)
            self._pending_images[route] = current
            if local_image is not None:
                self._start_pending_timer(route, current)
        if previous is not None:
            if previous.timer is not None:
                previous.timer.cancel()
            self._dispatch_pending(previous)

    def _attach_text(
        self, message: WeixinMessage, inbox_keys: tuple[str, ...],
    ) -> bool:
        if self._is_text_merge_boundary(message.text or ""):
            return False
        route = self._conversation_key(message)
        with self._pending_lock:
            pending = self._pending_images.get(route)
            if pending is None:
                return False
            if pending.timer is not None:
                pending.timer.cancel()
                pending.timer = None
            pending.text_message = message
            pending.inbox_keys += inbox_keys
            if pending.local_image is None:
                return True
            self._pending_images.pop(route, None)
        self._dispatch_pending(pending)
        return True

    def _start_pending_timer(
        self,
        route: tuple[str, str | None],
        pending: _PendingImage,
    ) -> None:
        timer = threading.Timer(
            self.image_text_merge_seconds,
            self._release_pending_image,
            args=(route,),
        )
        timer.daemon = True
        pending.timer = timer
        timer.start()

    def _release_pending_image(
        self, route: tuple[str, str | None],
    ) -> None:
        with self._pending_lock:
            pending = self._pending_images.pop(route, None)
        if pending is not None and not self._closed.is_set():
            self._dispatch_pending(pending)

    def _pending_image_ready(
        self,
        route: tuple[str, str | None],
        key: str,
        local_image: Path | None,
    ) -> None:
        if local_image is None:
            return
        dispatch = False
        with self._pending_lock:
            pending = self._pending_images.get(route)
            if pending is None or key not in pending.inbox_keys:
                if self.image_manager is not None:
                    self.image_manager.cleanup(local_image)
                return
            pending.local_image = local_image
            if pending.text_message is not None:
                self._pending_images.pop(route, None)
                dispatch = True
            else:
                self._start_pending_timer(route, pending)
        if dispatch:
            self._dispatch_pending(pending)

    def _fail_pending_image(
        self, route: tuple[str, str | None], key: str,
    ) -> None:
        with self._pending_lock:
            pending = self._pending_images.get(route)
            if pending is None or key not in pending.inbox_keys:
                return
            self._pending_images.pop(route, None)
        if self.inbox is not None:
            self.inbox.complete(key)
        if pending.text_message is not None:
            text_keys = tuple(item for item in pending.inbox_keys if item != key)
            self._dispatch(pending.text_message, None, None, text_keys)

    def _dispatch_pending(self, pending: _PendingImage) -> None:
        if pending.local_image is None:
            return
        message = pending.image_message
        if pending.text_message is not None:
            message = replace(
                pending.text_message,
                items=pending.image_message.items + pending.text_message.items,
            )
        self._dispatch(message, pending.local_image, None, pending.inbox_keys)

    @staticmethod
    def _conversation_key(
        message: WeixinMessage,
    ) -> tuple[str, str | None]:
        return message.from_user_id, message.to_user_id

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            message, project, local_image, local_file, inbox_keys = item
            self._handle(message, project, local_image, local_file, inbox_keys)

    def _handle(
        self, message: WeixinMessage, project: str | None,
        local_image: Path | None, local_file: InboundTextFile | None,
        inbox_keys: tuple[str, ...],
    ) -> None:
        if self.inbox is not None:
            for key in inbox_keys:
                self.inbox.start(key)
        completed = False
        try:
            if self.image_manager is not None and local_image is not None:
                try:
                    self.image_manager.validate(local_image)
                except PermanentMediaError as exc:
                    print(timed_terminal_notice(f"图片消息未处理：{exc}"), file=sys.stderr)
                    completed = True
                    return
            if self.file_manager is not None and local_file is not None:
                try:
                    self.file_manager.validate(local_file)
                except PermanentMediaError as exc:
                    print(timed_terminal_notice(f"文件消息未处理：{exc}"), file=sys.stderr)
                    completed = True
                    return
            if local_file is not None:
                completed = self.handler(message, local_file=local_file)
            elif local_image is not None:
                completed = self.handler(message, local_image)
            else:
                completed = self.handler(message)
        finally:
            try:
                if self.inbox is not None:
                    for key in inbox_keys:
                        if completed:
                            self.inbox.complete(key)
                        else:
                            self.inbox.uncertain(key)
            finally:
                if project is not None:
                    self.generator.end_analysis(project)
                if self.image_manager is not None:
                    self.image_manager.cleanup(local_image)
                if self.file_manager is not None:
                    self.file_manager.cleanup(local_file)
