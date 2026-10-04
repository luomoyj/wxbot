from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MAX_ROADMAP_BYTES = 256_000


class ProjectStatusError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectStatus:
    stage: str
    in_progress: str
    blocked: str
    next_step: str
    incomplete: tuple[str, ...]
    main_task: str = ""
    main_task_status: str = ""
    acceptance: str = ""
    inserted_task: str = ""
    inserted_task_status: str = ""
    resume_after_insert: str = ""

    def render(self, intent: str) -> str:
        if intent in {"next", "priority"}:
            lines = [f"当前应继续：{self.next_step}"]
            if self.main_task and self.next_step != self.main_task:
                status = self.main_task_status.rstrip("。")
                lines.append(f"主任务：{self.main_task}（{status}）")
            if self.acceptance and self.main_task_status.rstrip("。") == "待验收":
                lines.append(f"验收条件：{self.acceptance}")
            lines.append("当前无阻塞。" if self._is_none(self.blocked) else f"当前阻塞：{self.blocked}")
            return "\n".join(lines)
        if intent == "task":
            if self._is_none(self.in_progress):
                return f"当前没有进行中的项目任务。\n正式下一步：{self.next_step}"
            return f"当前项目任务：{self.in_progress}\n正式下一步：{self.next_step}"
        if intent == "blocked":
            return "当前无阻塞。" if self._is_none(self.blocked) else f"当前阻塞：{self.blocked}"
        if intent == "incomplete":
            if not self.incomplete:
                return "ROADMAP.md中没有未完成事项。"
            return "尚未完成：\n" + "\n".join(
                f"{index}. {item}" for index, item in enumerate(self.incomplete[:5], 1)
            )
        return "\n".join((
            f"当前阶段：{self.stage}",
            f"进行中：{self.in_progress}",
            f"阻塞：{self.blocked}",
            f"下一步：{self.next_step}",
        ))

    @staticmethod
    def _is_none(value: str) -> bool:
        return value.strip().rstrip("。") in {"无", "没有", "暂无"}


class ProjectStatusReader:
    ACTIVE_STATUSES = {"进行中", "待验收", "被打断"}

    def record_acceptance(self, task_id: str) -> None:
        if re.fullmatch(r"[A-Fa-f0-9]{6}", task_id) is None:
            raise ProjectStatusError("任务编号无效，未写入验收记录")
        self.read()
        try:
            text = self.roadmap_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectStatusError("无法读取 ROADMAP验收记录") from exc
        entry = f"- 任务 `{task_id}`：主人通过 `/accept` 明确确认验收通过。"
        if entry in text:
            return
        heading = "## 微信任务验收记录"
        if heading not in text:
            text = text.rstrip() + "\n\n" + heading + "\n\n" + entry + "\n"
        else:
            position = text.index(heading) + len(heading)
            text = text[:position] + "\n\n" + entry + text[position:]
        try:
            self.roadmap_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise ProjectStatusError("无法写入 ROADMAP验收记录") from exc

    @staticmethod
    def _normalized_status(value: str) -> str:
        return value.strip().rstrip("。")
    INTENT_PATTERNS = (
        ("next", re.compile(
            r"(?:正式)?下一步(?:做什么|干什么|怎么做|是什么)?"
            r"|接下来(?:做什么|干什么|怎么做|是什么)", re.I,
        )),
        ("priority", re.compile(
            r"(?:当前|现在)?最优先(?:的)?(?:任务|事项)?(?:是什么|做什么|干什么)?", re.I,
        )),
        ("progress", re.compile(
            r"(?:项目当前|当前项目|当前)进展(?:是什么|怎么样)?"
            r"|(?:项目)?现在做到哪(?:一步)?了?"
            r"|项目进展(?:是什么|怎么样)?", re.I,
        )),
        ("task", re.compile(
            r"项目当前任务(?:是什么|怎么样)?"
            r"|当前项目任务(?:是什么|怎么样)?"
            r"|当前(?:的)?任务(?:怎么样了|完成了吗|状态)?", re.I,
        )),
        ("incomplete", re.compile(
            r"还有(?:什么|哪些)(?:没|没有|未)完成(?:的)?(?:任务|事项)?"
            r"|还有什么(?:需要|要)做", re.I,
        )),
        ("blocked", re.compile(
            r"(?:当前|现在)?有没有阻塞(?:项)?"
            r"|(?:当前|项目)?阻塞(?:是什么|有哪些)?", re.I,
        )),
    )

    def __init__(self, roadmap_path: Path) -> None:
        self.roadmap_path = roadmap_path

    @classmethod
    def classify(cls, message: str) -> str | None:
        command = re.sub(r"\s+", "", message.strip()).rstrip("。！？!?")
        for intent, pattern in cls.INTENT_PATTERNS:
            if pattern.fullmatch(command):
                return intent
        return None

    def read(self) -> ProjectStatus:
        try:
            size = self.roadmap_path.stat().st_size
        except OSError as exc:
            raise ProjectStatusError("当前项目缺少 ROADMAP.md") from exc
        if size > MAX_ROADMAP_BYTES:
            raise ProjectStatusError("ROADMAP.md过大，已停止读取")
        try:
            text = self.roadmap_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ProjectStatusError("ROADMAP.md不是有效 UTF-8文本") from exc
        except OSError as exc:
            raise ProjectStatusError("ROADMAP.md读取失败") from exc
        section_match = re.search(
            r"^##\s+当前状态\s*$\n(?P<body>.*?)(?=^##\s+|\Z)", text, re.M | re.S,
        )
        if section_match is None:
            raise ProjectStatusError("ROADMAP.md缺少“当前状态”章节")
        fields: dict[str, str] = {}
        legacy_labels = {
            "当前阶段": "stage", "进行中": "in_progress", "阻塞": "blocked", "下一步": "next_step",
        }
        current_labels = {
            "当前阶段": "stage",
            "主任务": "main_task",
            "主任务状态": "main_task_status",
            "验收条件": "acceptance",
            "当前插入任务": "inserted_task",
            "插入任务状态": "inserted_task_status",
            "插入任务完成后恢复": "resume_after_insert",
            "阻塞": "blocked",
            "正式下一步": "formal_next_step",
        }
        for line in section_match.group("body").splitlines():
            match = re.match(r"^-\s*([^：:]+?)\s*[:：]\s*(.+?)\s*$", line)
            if match:
                label = match.group(1).strip()
                key = current_labels.get(label) or legacy_labels.get(label)
                if key:
                    fields[key] = match.group(2).strip()

        uses_current_schema = any(
            key in fields
            for key in ("main_task", "main_task_status", "inserted_task", "formal_next_step")
        )
        if uses_current_schema:
            missing = [
                label for label, key in current_labels.items() if not fields.get(key)
            ]
            if missing:
                raise ProjectStatusError("ROADMAP.md当前状态缺少字段：" + "、".join(missing))
            inserted_active = (
                self._normalized_status(fields["inserted_task_status"]) in self.ACTIVE_STATUSES
                and not ProjectStatus._is_none(fields["inserted_task"])
            )
            main_active = (
                self._normalized_status(fields["main_task_status"]) in self.ACTIVE_STATUSES
            )
            if inserted_active:
                fields["in_progress"] = fields["inserted_task"]
                fields["next_step"] = fields["inserted_task"]
            elif main_active:
                fields["in_progress"] = fields["main_task"]
                fields["next_step"] = fields["main_task"]
            else:
                fields["in_progress"] = "无"
                fields["next_step"] = fields["formal_next_step"]
            fields.pop("formal_next_step")
        else:
            missing = [
                label for label, key in legacy_labels.items() if not fields.get(key)
            ]
            if missing:
                raise ProjectStatusError("ROADMAP.md当前状态缺少字段：" + "、".join(missing))
        incomplete = tuple(
            match.group(1).strip()
            for match in re.finditer(r"^-\s*\[\s\]\s+(.+?)\s*$", text, re.M)
        )
        return ProjectStatus(incomplete=incomplete, **fields)

    def answer(self, message: str) -> str | None:
        intent = self.classify(message)
        return self.read().render(intent) if intent is not None else None
