from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


# Keep help and argument errors tied to the same supported command surface.
COMMANDS = {
    "help": ("/help", "显示命令帮助"),
    "projects": ("/projects", "列出项目"),
    "project": ("/project [项目名 | add 路径]", "查看、切换或登记项目"),
    "chat": ("/chat", "切回普通聊天"),
    "progress": ("/progress", "项目正式进度"),
    "next": ("/next", "项目正式下一步"),
    "tasks": ("/tasks（别名 /agents）", "运行任务和等待队列"),
    "task": ("/task", "当前或最近任务结果"),
    "stop": ("/stop", "取消最新排队任务，无排队则取消运行任务"),
    "error": ("/error", "最近任务失败原因"),
    "accept": ("/accept", "确认最近待验收任务通过"),
    "sessions": ("/sessions [search 关键词 | show 编号]", "列表、搜索或预览会话"),
    "resume": ("/resume 编号", "切换到最近列表中的会话"),
    "status": ("/status", "当前会话信息"),
    "model": ("/model", "当前模型和推理等级"),
    "usage": ("/usage", "Token用量"),
    "context": ("/context", "上下文窗口信息"),
    "new": ("/new（别名 /reset）", "重新开始当前会话"),
    "compress": ("/compress", "压缩当前上下文"),
    "reset-ai-state": ("/reset-ai-state [confirm]", "预览或确认清空全部 AI状态"),
    "diff": ("/diff [--stat | 文件名]", "最近任务原始差异、摘要或单文件修改"),
    "rollback": ("/rollback [latest | confirm]", "恢复记录列表、预览或确认恢复最近任务"),
    "send": ("/send 文件名", "发送项目内允许的文件"),
}
ALIASES = {"reset": "new", "agents": "tasks"}


@dataclass(frozen=True)
class SlashCommand:
    name: str
    args: tuple[str, ...] = ()
    error: str = ""


def command_help() -> str:
    return "微信工作台指令：\n\n" + "\n\n".join(
        f"{usage}：{description}" for usage, description in COMMANDS.values()
    ) + (
        '\n\n含空格的文件名请加引号，例如 /send "my notes.md"。'
        "\n普通咨询和工作要求直接发送自然语言。"
        "例如：修改 README并运行测试；提交代码（默认提交并推送）。"
        "\n/reset只清空当前会话；全部 AI状态清理与文件恢复仍需先预览、再确认。"
        "\n/stop不停止后台服务。旧中文快捷说法仍可用。"
        "\n恢复步骤：/rollback latest 预览，然后 /rollback confirm 确认。"
    )


def parse_command(text: str) -> SlashCommand | None:
    text = text.strip()
    if not text.startswith("/"):
        return None
    name = text.split(maxsplit=1)[0][1:].lower()
    name = ALIASES.get(name, name)
    if name not in COMMANDS:
        return SlashCommand(name, error="未知命令，请发送 /help 查看用法。")
    error = "用法：" + COMMANDS[name][0]
    if "\n" in text or "\r" in text:
        return SlashCommand(name, error=error)
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return SlashCommand(name, error=error + "；文件名引号必须配对。")
    args = tuple(
        value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value
        for value in tokens[1:]
    )
    valid = not args
    if name == "project":
        valid = not args or (
            len(args) == 1 and args[0] not in {".", ".."}
            and bool(args[0]) and re.search(r'[\\/:*?"<>|\x00-\x1f]', args[0]) is None
        ) or (len(args) == 2 and args[0] == "add" and bool(args[1]))
    elif name == "sessions":
        valid = not args or (
            len(args) >= 2 and args[0] == "search" and bool(" ".join(args[1:]).strip())
        ) or (len(args) == 2 and args[0] == "show" and _position(args[1]))
    elif name == "resume":
        valid = len(args) == 1 and _position(args[0])
    elif name == "reset-ai-state":
        valid = not args or args == ("confirm",)
    elif name == "rollback":
        valid = not args or args in {("latest",), ("confirm",)}
    elif name == "diff":
        valid = not args or (len(args) == 1 and bool(args[0]) and (
            args[0] == "--stat" or not args[0].startswith("-")
        ))
    elif name == "send":
        valid = len(args) == 1 and bool(args[0]) and not args[0].startswith("-")
    return SlashCommand(name, args, "" if valid else error)


def _position(value: str) -> bool:
    return re.fullmatch(r"[1-9][0-9]{0,3}", value) is not None
