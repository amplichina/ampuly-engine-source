"""企业微信客服知识解析：将项目知识库读为问答、需求提问和欢迎语。

Pure functions over the text of the project's knowledge file (``wecom_channel.KNOWLEDGE_FILE``,
``project-materials/common/wecom-kf-knowledge.md``); nothing here reads a file, the clock or the network.
The following sections are read from the text:

- **entries** -- a customer question and the answer the business sends for it, word for word. Two ways to write one:

      **问：你们提供什么服务？**               ## 问：你们提供什么服务？
      答：项目咨询。……                        答：项目咨询。……（the body, up to the next heading）

  In the first form the answer is the 答 line and the lines right under it, up to a blank line, a heading, a rule
  (``---``) or the next question. In the second it is the whole body up to the next heading or rule; a leading 答：
  is dropped.
- **## 需求提问** -- one line per start field, ``- <field id>：<question>``: what the customer is asked for that field
  (the field's label works as the key too). A field with no line here is asked 「麻烦告诉我们：<its label without its
  bracketed notes>」.
- **## 欢迎语** -- the text under it: the greeting sent when a customer only says hello or thanks.

Never sent, and counted so the owner's card can say so: an answer that still holds an unfilled 【...】 (``unfilled``);
a question longer than 200 characters or an answer longer than 1000 characters or than one 微信客服 text message
(``too_long``); sendable entries past the first 60 (``over_cap``); and, when the text was cut at the reading limit,
whatever it ended in the middle of (``cut``) -- a cut answer is never sent as if it were whole.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ampuly.plugins.wecom_service.wecom_kf import TEXT_MAX_BYTES

MAX_ENTRIES = 60
MAX_ANSWER_CHARS = 1000
MAX_QUESTION_CHARS = 200
MAX_FIELD_QUESTION_CHARS = 200
MAX_WELCOME_CHARS = 500
# One 微信客服 text message: a prepared answer is sent whole or not at all (fit_text would cut it with an ellipsis).
MESSAGE_MAX_BYTES = TEXT_MAX_BYTES

FIELD_SECTION = "需求提问"
WELCOME_SECTION = "欢迎语"
FALLBACK_FIELD_QUESTION = "麻烦告诉我们：{label}"
# How many unfilled questions the owner's card may name (their own words, shortened).
UNFILLED_NAMED = 10

_HEADING = re.compile(r"^\s{0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_QUESTION = re.compile(r"^(?:\*\*)?\s*问\s*[：:]\s*(?:\*\*)?\s*(.+?)\s*(?:\*\*)?$")
_BOLD_QUESTION = re.compile(r"^\s*\*\*\s*问\s*[：:]\s*(?:\*\*)?\s*(.+?)\s*(?:\*\*)?\s*$")
_ANSWER = re.compile(r"^\s*(?:\*\*)?\s*答\s*[：:]\s*(?:\*\*)?\s*(.*)$")
_FIELD_LINE = re.compile(r"^\s*[-*+]\s+`?([^`：:]+?)`?\s*[：:]\s*(.+?)\s*$")
_NOTES = re.compile(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^\]]*\]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def entry_id(question: str) -> str:
    """A stable id for a prepared answer: its question's digest (the calibration log names entries by it)."""
    return "kb-" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:10]


def unfilled(text: str) -> bool:
    """Whether the owner still has something to fill in: any 【 or 】 left in the text."""
    return "【" in text or "】" in text


def plain_label(label: Any, fallback: str = "") -> str:
    """A start field's label without its bracketed notes: 「Slogan 完整原文（没有则填无；…）」 -> 「Slogan 完整原文」."""
    text = _NOTES.sub("", str(label or "")).strip(" 　：:")
    return text or str(fallback or "").strip()


@dataclass(frozen=True)
class Entry:
    """One prepared answer: sent word for word when JEV judges it answers the customer."""

    id: str
    question: str
    answer: str


@dataclass
class Knowledge:
    """What the knowledge file holds for the JEV turn, and what it holds that is never sent (counted)."""

    entries: tuple[Entry, ...] = ()
    field_questions: dict[str, str] = field(default_factory=dict)
    welcome: str = ""
    unfilled: int = 0
    too_long: int = 0
    over_cap: int = 0
    cut: int = 0
    unfilled_questions: tuple[str, ...] = ()
    welcome_unfilled: bool = False

    def question_for(self, row: Mapping[str, Any]) -> str:
        """What the customer is asked for one start field (``{id, label}``): the owner's line, else the fallback."""
        field_id = str(row.get("id", "") or "").strip()
        label = str(row.get("label", "") or "").strip()
        for key in (field_id, label, plain_label(label)):
            if key and self.field_questions.get(key):
                return self.field_questions[key]
        return FALLBACK_FIELD_QUESTION.format(label=plain_label(label, field_id) or field_id)

    def counts(self) -> dict[str, Any]:
        """For the owner's card: how many answers can go out, how many are not filled in yet, and the rest."""
        return {
            "entries": len(self.entries),
            "unfilled": self.unfilled,
            "field_prompts": len(self.field_questions),
            "too_long": self.too_long,
            "over_cap": self.over_cap,
            "cut": self.cut,
            "welcome": bool(self.welcome),
            "welcome_unfilled": self.welcome_unfilled,
            "unfilled_questions": list(self.unfilled_questions),
        }


def _clean(line: str) -> str:
    return _CONTROL.sub("", line).rstrip()


def _title(text: str) -> str:
    return text.replace("*", "").replace("`", "").strip(" 　：:")


def parse(text: Any, *, truncated: bool = False) -> Knowledge:
    """The knowledge file's text -> ``Knowledge``. ``truncated``: the text was cut at the reading limit, so whatever it
    ends in the middle of (the last line, an open answer, the welcome) is dropped and counted, never read as whole."""
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cut = 0
    if truncated and source and not source.endswith("\n"):
        source = source[: source.rfind("\n") + 1]
        cut = 1
    raw_entries: list[tuple[str, str]] = []
    field_questions: dict[str, str] = {}
    welcome_lines: list[str] = []
    section = "faq"
    # The entry being read: [question, answer lines, form ("bold" / "heading"), answering?]
    current: list[Any] | None = None

    def finish() -> None:
        nonlocal current
        if current is not None:
            answer = "\n".join(current[1]).strip()
            answer = re.sub(r"\n{3,}", "\n\n", answer)
            if answer:
                raw_entries.append((current[0], answer))
        current = None

    for raw in source.split("\n"):
        line = _clean(raw)
        heading = _HEADING.match(line)
        if heading:
            finish()
            title = heading.group(2) or ""
            asked = _QUESTION.match(title)
            if asked:
                current = [_title(asked.group(1)), [], "heading", True]
                section = "faq"
                continue
            name = _title(title)
            section = "fields" if name == FIELD_SECTION else "welcome" if name == WELCOME_SECTION else "faq"
            continue
        if _RULE.match(line):
            # A rule ends an answer and a 需求提问 / 欢迎语 section alike.
            finish()
            section = "faq"
            continue
        bold = _BOLD_QUESTION.match(line)
        if bold:
            finish()
            current = [_title(bold.group(1)), [], "bold", False]
            continue
        if current is not None:
            if current[2] == "heading":
                answered = _ANSWER.match(line) if not any(part.strip() for part in current[1]) else None
                current[1].append(answered.group(1) if answered else line)
                continue
            if not current[3]:
                answered = _ANSWER.match(line)
                if answered:
                    current[3] = True
                    current[1].append(answered.group(1))
                    continue
                if not line.strip():
                    continue  # a blank line between the question and its 答 is allowed
                current = None  # a question with no 答 line: nothing to send
            elif not line.strip():
                finish()
                continue
            else:
                current[1].append(line.strip())
                continue
        if section == "fields":
            asked_field = _FIELD_LINE.match(line)
            if asked_field:
                key, question = asked_field.group(1).strip(), asked_field.group(2).strip()
                if key and question and not unfilled(question) and len(question) <= MAX_FIELD_QUESTION_CHARS:
                    field_questions.setdefault(key, question)
        elif section == "welcome":
            welcome_lines.append(line)
    if current is not None:
        if truncated:
            cut = 1  # ended in the middle of an answer: not sent as if it were whole
            current = None
        finish()
    welcome = "\n".join(welcome_lines).strip()
    welcome_unfilled = False
    if welcome and truncated and section == "welcome":
        welcome, cut = "", 1
    if welcome and unfilled(welcome):
        welcome, welcome_unfilled = "", True
    if len(welcome) > MAX_WELCOME_CHARS or len(welcome.encode("utf-8")) > MESSAGE_MAX_BYTES:
        welcome = ""
    entries: list[Entry] = []
    seen: set[str] = set()
    counted = {"unfilled": 0, "too_long": 0, "over_cap": 0}
    unfilled_named: list[str] = []
    for question, answer in raw_entries:
        ident = entry_id(question)
        if not question or ident in seen:
            continue
        seen.add(ident)
        if unfilled(answer):
            counted["unfilled"] += 1
            if len(unfilled_named) < UNFILLED_NAMED:
                unfilled_named.append(question[:30] + ("…" if len(question) > 30 else ""))
            continue
        if (len(question) > MAX_QUESTION_CHARS or len(answer) > MAX_ANSWER_CHARS
                or len(answer.encode("utf-8")) > MESSAGE_MAX_BYTES):
            counted["too_long"] += 1
            continue
        if len(entries) >= MAX_ENTRIES:
            counted["over_cap"] += 1
            continue
        entries.append(Entry(ident, question, answer))
    return Knowledge(
        entries=tuple(entries), field_questions=field_questions, welcome=welcome, cut=cut,
        unfilled_questions=tuple(unfilled_named), welcome_unfilled=welcome_unfilled, **counted,
    )
