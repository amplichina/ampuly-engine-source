"""Human gates in a Feishu group: the card, the reply, the record.

Design: the module contract. Deliverables stay in the run's
workspace; the group gets a mirror card per version with the node's
standard as a checklist, an automatic pre-check line, the images, and two
buttons. A person answers by pressing 放行 / 打回 or by replying in the
card's thread; a reject carries a structured revision sheet that is written
verbatim to the node's revision path so the upstream worker reads a file,
not a paraphrase. Every step is recorded in business.db (sent, read,
viewed, decided) with who and when.

This module is transport-agnostic: :class:`GateService` needs a
:class:`FeishuGateClient` (httpx, REST) to send and a source of inbound
events (the long-connection adapter in :mod:`feishu_events`, or a webhook)
to receive. Everything that decides is a pure function here and tested
without Feishu.
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

import httpx

from ampuly.plugins.office_ui.order_folder import OUTSIDE, named_by_any
from ampuly.plugins.office_ui.order_folder import match as match_deliverable

FEISHU_API = "https://open.feishu.cn/open-apis"
APPROVE_WORDS = ("放行", "通过", "approve", "ok", "同意")
REJECT_WORDS = ("打回", "驳回", "reject", "不通过", "拒绝", "不允许")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_CARD_IMAGES = 9
# A decision claimed but never finished (the process died while the runtime
# was being told) stops blocking the gate after this long.
DECISION_CLAIM_STALE_SECONDS = 600.0
# Events that say "somebody looked at this version" -- counted only when the
# somebody is the gate's assignee or a person allowed to decide it.
SEEN_EVENT_KINDS = frozenset({"read", "viewed"})
# Telling the runtime can be cut off part-way (shutdown, a dropped
# connection) with nobody knowing whether the runtime took the answer.  It
# may well have: then the gate is answered, the console no longer offers it,
# and a 打回 whose sheet was never written would be lost.  So the claim is
# kept, the whole answer -- a 打回's full revision sheet -- is recorded under
# the first kind, and a read-only look at the checkpoint settles it (the
# second kind): at once, on the next click while the gate still waits, or by
# the duty watch's pass (GateService.settle_cut_off_answers).  Only that pass
# reaches a gate the runtime has already answered -- no click can, the gate
# is no longer offered.  An answer whose fate is not known is never sent
# again (the rule for an unknown outcome, C10).
OUTCOME_UNKNOWN_EVENT = "reply_outcome_unknown"
OUTCOME_SETTLED_EVENT = "reply_outcome_settled"
# A look that does not answer in this long counts as "cannot tell": it may
# run while a cancellation or a shutdown is waiting on it.
LOOK_BACK_TIMEOUT_SECONDS = 10.0


# ── what a gate looks like ────────────────────────────────────────────────


@dataclass
class GateSpec:
    checkpoint_id: str
    checkpoint_type: str
    task_id: str            # UI anchor task (the order)
    session_id: str         # UI anchor session
    waiting_task_id: str
    order_id: str
    order_title: str
    node_id: str
    node_name: str
    node_type: str          # human_review | human_do | task_user_input
    assignee: str
    version: int
    standard: list[str]
    deliverables: list[str]     # workspace-relative paths declared by the node
    revision_path: str          # where a reject's sheet is written
    workspace_root: str
    prompt: str = ""
    precheck: list[str] = field(default_factory=list)   # problems found
    present: list[str] = field(default_factory=list)    # deliverables that exist
    text_previews: dict[str, str] = field(default_factory=dict)  # text deliverables, shown inline
    project_id: str = ""          # owning project for a console deep link
    # What the order was placed with (the intake fields), shown on the card so the reviewer sees the order, not
    # only the node's standard (owner's rule, : a card carries the order, its state and its deliverables).
    order_inputs: dict[str, str] = field(default_factory=dict)
    # "review": a node's deliverable to pass or send back.  "permission": an AI employee asking to use a tool
    # (checkpoint type tool_permission); it has no deliverable, no standard and no revision sheet, and its two
    # answers are the checkpoint's own options (see ``permission_details``).  "lesson": the lesson card
    # (company_delivery_feedback, 留点经验卡) -- 学一轮（会花钱） or 不用了, no deliverable and no revision sheet
    # (see ``build_lesson_card``).  "order_close": an order in 客户验收中 with one button, 确认收货并结案 (FC-1); no
    # checkpoint behind it -- its key is ``order_close_key`` and its answer closes the order (``GateService.close_order``).
    card_kind: str = "review"
    permission: dict[str, Any] = field(default_factory=dict)
    # A console extension's own card kind (``extensions.FeishuCardKind``: ``card_kind`` names it) carries what its
    # card shows here -- e.g. an order's delivery to its customer: the customer in words, the files, the package.
    delivery: dict[str, Any] = field(default_factory=dict)

    @property
    def is_permission(self) -> bool:
        return self.card_kind == "permission"

    @property
    def extension_card(self) -> Any:
        """The registered extension card kind this card is (``extensions.FeishuCardKind``); None for the console's own
        kinds and for a kind no extension registered."""
        if self.card_kind in _CONSOLE_CARD_KINDS:
            return None
        from ampuly.plugins.office_ui.extensions import feishu_card_kind

        return feishu_card_kind(self.card_kind)

    @property
    def is_lesson(self) -> bool:
        return self.card_kind == "lesson"

    @property
    def is_order_close(self) -> bool:
        return self.card_kind == ORDER_CLOSE_KIND

    @property
    def title(self) -> str:
        if self.is_order_close:
            return f"【{self.order_id or self.order_title[:24]} · {ORDER_CLOSE_LABEL}】"
        extension = self.extension_card
        if extension is not None:
            return f"【{self.order_id or self.order_title[:24]} · {extension.label}】"
        if self.is_permission:
            return f"【{self.order_id or self.order_title[:24]} · {self.node_name} · 请求权限】"
        if self.is_lesson:
            parts = [self.order_id or self.order_title[:24], self.node_name, "留点经验"]
            return "【" + " · ".join(part for part in parts if part) + "】"
        return f"【{self.order_id or self.order_title[:24]} · {self.node_name} · 第 {self.version} 版】"


TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
TEXT_PREVIEW_CHARS = 2500


def revision_sheet_count(workspace_root: str, revision_path: str) -> int:
    """How many revision sheets this gate has already produced (latest + archived).

    Each rejection writes ``revision_path`` and archives the previous one next
    to it, so the count is the number of rejections so far -- and the next
    gate version is one more than that.  Zero when nothing was rejected yet.
    """
    path = _workspace_path(workspace_root, revision_path)
    if path is None or not path.parent.is_dir():
        return 0
    stem, suffix = path.stem, path.suffix
    try:
        return sum(
            1
            for p in path.parent.iterdir()
            if p.is_file() and (p.name == path.name or (p.name.startswith(stem + "-") and p.suffix == suffix))
        )
    except OSError:
        return 0


def _workspace_path(workspace_root: str, relative_path: str) -> Path | None:
    """Resolve one gate artifact without ever leaving its order workspace."""
    if not workspace_root or not relative_path:
        return None
    try:
        root = Path(workspace_root).resolve()
        candidate = (root / str(relative_path).replace("/", os.sep)).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def text_previews_for(workspace_root: str, present: list[str]) -> dict[str, str]:
    """The reviewer reads a form in the group, not in a file browser: inline text deliverables."""
    out: dict[str, str] = {}
    root = Path(workspace_root) if workspace_root else None
    if root is None:
        return out
    for rel in present:
        if Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            path = _workspace_path(str(root), rel)
            if path is None:
                continue
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                text = handle.read(TEXT_PREVIEW_CHARS + 1).strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > TEXT_PREVIEW_CHARS:
            text = text[:TEXT_PREVIEW_CHARS] + chr(10) + "…（已截断，点「查看全部文件」可在本话题下载全文）"
        out[rel] = text
    return out


def gate_detail(spec: GateSpec, *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The console's gate panel: the same checklist, deliverables and fields as the card."""
    import base64

    root = Path(spec.workspace_root) if spec.workspace_root else None
    files: list[dict[str, Any]] = []
    for rel in spec.present:
        entry: dict[str, Any] = {"path": rel, "kind": "binary"}
        if root is not None:
            path = _workspace_path(str(root), rel)
            if path is None:
                files.append(entry)
                continue
            suffix = path.suffix.lower()
            try:
                if suffix in IMAGE_SUFFIXES and path.stat().st_size <= 6_000_000:
                    entry["kind"] = "image"
                    entry["data_url"] = f"data:image/{suffix.lstrip('.')};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
                elif suffix in TEXT_SUFFIXES:
                    entry["kind"] = "text"
                    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                        entry["content"] = handle.read(20000)
                entry["size"] = path.stat().st_size
            except OSError:
                pass
        files.append(entry)
    return {
        "checkpoint_id": spec.checkpoint_id,
        "checkpoint_type": spec.checkpoint_type,
        "task_id": spec.task_id,
        "session_id": spec.session_id,
        "order_id": spec.order_id,
        "order_title": spec.order_title,
        "node_id": spec.node_id,
        "node_name": spec.node_name,
        "node_type": spec.node_type,
        "assignee": spec.assignee,
        "version": spec.version,
        "standard": list(spec.standard),
        "deliverables": list(spec.deliverables),
        # A declared pattern (``board/*.png``, ``round1/``) is delivered when a file it names is present -- by the
        # order-folder rule, rather than a literal string comparison with the pattern.
        "missing": [d for d in spec.deliverables if not named_by_any(d, spec.present)],
        "precheck": list(spec.precheck),
        "revision_path": spec.revision_path,
        "prompt": spec.prompt,
        "files": files,
        "events": list(events or []),
    }


def gate_root_candidates(workspace_root: str, output_root: str) -> list[str]:
    """The roots a gate looks in, the output root first (data phase 1).

    ``pick_gate_root`` keeps the first candidate on a tie, so an order with
    nothing delivered yet writes its revision sheet into the output root --
    where the rework step's relative read (``feedback/修改单.md``) finds it --
    instead of the order folder beside it, which a step can no longer write.
    """
    roots: list[str] = []
    for value in (output_root, workspace_root):
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


def pick_gate_root(candidates: list[str], deliverables: list[str]) -> str:
    """The root the node actually delivered into.

    A company run has two roots: the order workspace and its ``output/``
    sub-root that workers use as their working directory. A designer that
    wrote ``round1/*.png`` under ``output/`` was reported as "还没有交付物"
    because the gate only looked under the order root. Prefer
    the candidate where the declared deliverables are found; fall back to
    the first non-empty candidate so previews, uploads and revision sheets
    all use one root.
    """
    roots = [str(c or "").strip() for c in candidates if str(c or "").strip()]
    if not roots:
        return ""
    best, best_count = roots[0], -1
    for root in roots:
        present, _problems = precheck_deliverables(root, deliverables)
        if len(present) > best_count:
            best, best_count = root, len(present)
    return best


def precheck_deliverables(workspace_root: str, deliverables: list[str]) -> tuple[list[str], list[str]]:
    """(present files, problems). A declared file that is missing is a problem;
    a generated image without its prompt record is a problem too.

    Which files a declared path names is the order-folder rule (``order_folder.match``, C5-1): ``*`` stays within one
    name, ``dir/`` is the files directly inside minus the prompt records, hidden files and links never count.
    """
    present: list[str] = []
    problems: list[str] = []
    root = Path(workspace_root) if workspace_root else None
    for rel in deliverables:
        clean = str(rel or "").strip().replace(chr(92), "/")
        if not clean or root is None:
            continue
        found = match_deliverable(root, clean)
        if found.problem == OUTSIDE:
            problems.append(f"路径越界，拒绝读取：{clean}")
            continue
        if found.problem:
            # e.g. "**" inside a path component: a malformed pattern is a problem to report, not a crash
            # that keeps the whole card from being sent.
            problems.append(f"交付物路径写法不对：{clean}")
            continue
        if not found.files:
            problems.append(f"缺 {clean}（目录为空或不存在）" if found.pattern.folder else f"缺 {clean}")
        present.extend(found.files)
        if found.truncated:
            problems.append(f"交付物文件太多，没有查完：{clean}")
    for rel in list(present):
        path = _workspace_path(str(root), rel) if root else None
        if path is not None and path.suffix.lower() in IMAGE_SUFFIXES and not path.with_name(path.name + ".prompt.json").is_file():
            problems.append(f"{rel} 没有提示词记录")
    return present, problems


# ── the card ──────────────────────────────────────────────────────────────


def _reject_form_is_visual(spec: GateSpec) -> bool:
    """Whether the node hands over pictures -- then the design-brief fields (保留什么 / 新增删除元素) earn their place.

    A step that looks up a tracking number delivers a table, not a drawing; asking what to keep of its shapes and
    colours sends the reviewer the wrong way.  Vector and PDF files count as pictures here even though the card
    cannot show them inline (IMAGE_SUFFIXES is what the card can upload).
    """
    visual = IMAGE_SUFFIXES | {".gif", ".svg", ".pdf"}
    # A declared glob such as ``round1/*.png`` keeps its suffix, so it counts like a delivered file.
    return any(PurePosixPath(str(path or "")).suffix.lower() in visual for path in [*spec.deliverables, *spec.present])


def build_reject_form(spec: GateSpec) -> dict[str, Any]:
    """The revision sheet as a Feishu form card: the same fields the console panel has.

    A typed template in a thread reply works but is easy to get wrong; the
    form makes each field explicit and submits them in one action.

    The form is the plain three -- 哪里不对 / 要改成什么 / 补充说明 -- and they land in the sheet's existing
    columns (不符合哪条标准 / 修改什么 / 可选方向), so the file the upstream role reads keeps its shape.  Only a
    node whose deliverables are pictures also asks 保留什么 and 新增/删除元素.
    """
    value_base = {"gate": spec.checkpoint_id, "version": spec.version, "action": "reject_submit"}
    visual = _reject_form_is_visual(spec)
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**打回 · {spec.node_name} · 第 {spec.version} 版**\n填完点「提交打回」，修改单会写进 {spec.revision_path or 'feedback/修改单.md'}。"},
    ]
    # JSON 1.0 card: inputs and the form_submit button share a named form.
    # A plain callback button outside it does not submit the input values.
    form_elements: list[dict[str, Any]] = []
    if spec.standard:
        form_elements.append({
            "tag": "select_static",
            "name": "failed_standards",
            "placeholder": {"tag": "plain_text", "content": "不符合哪条标准"},
            "options": [
                {"text": {"tag": "plain_text", "content": item[:60]}, "value": str(index)}
                for index, item in enumerate(spec.standard[:10])
            ],
        })
    if len(spec.present) > 1:
        form_elements.append({
            "tag": "select_static",
            "name": "chosen",
            "placeholder": {"tag": "plain_text", "content": "选中稿（若适用）"},
            "options": [
                {"text": {"tag": "plain_text", "content": rel[:60]}, "value": rel}
                for rel in spec.present[:10]
            ],
        })
    form_elements.append(
        {"tag": "input", "name": "where_wrong", "label": {"tag": "plain_text", "content": "哪里不对"},
         "placeholder": {"tag": "plain_text", "content": "这一版错在哪、缺了什么"}},
    )
    if visual:
        form_elements.append(
            {"tag": "input", "name": "keep", "label": {"tag": "plain_text", "content": "保留什么"},
             "placeholder": {"tag": "plain_text", "content": "这一版哪里对，下一版别丢"}},
        )
    form_elements.append(
        {"tag": "input", "name": "change", "required": True, "label": {"tag": "plain_text", "content": "要改成什么（必填）"},
         "placeholder": {"tag": "plain_text", "content": "具体到形状、颜色、比例、位置" if visual else "说清楚改成什么样、按什么来"}},
    )
    if visual:
        form_elements.append({"tag": "input", "name": "add_remove", "label": {"tag": "plain_text", "content": "新增 / 删除元素"}})
    form_elements.extend([
        {"tag": "input", "name": "directions", "label": {"tag": "plain_text", "content": "补充说明（可不填）"},
         "placeholder": {"tag": "plain_text", "content": "还有什么要交代的；几条就用；隔开"}},
        {"tag": "button", "name": "submit_revision", "action_type": "form_submit",
         "text": {"tag": "plain_text", "content": "提交打回"}, "type": "danger", "value": value_base},
    ])
    elements.extend([
        {"tag": "form", "name": "revision_sheet", "elements": form_elements},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "也可以直接在本话题里回一句「打回：…」。"}]},
    ])
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": f"打回 · {spec.order_id or spec.order_title}"}, "template": "red"},
        "elements": elements,
    }


def sheet_from_form(spec: GateSpec, values: dict[str, Any]) -> RevisionSheet:
    """Form values -> the same revision sheet the console builds."""
    def _text(key: str) -> str:
        return str(values.get(key, "") or "").strip()

    failed: list[str] = []
    raw_failed = values.get("failed_standards")
    for token in (raw_failed if isinstance(raw_failed, list) else [raw_failed]):
        try:
            index = int(str(token))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(spec.standard):
            failed.append(spec.standard[index])
    # 哪里不对, in the reviewer's own words, sits beside the ticked standard in the sheet's 不符合哪条标准 column.
    where_wrong = _text("where_wrong")
    if where_wrong:
        failed.append(where_wrong)
    directions = [part.strip() for part in re.split(r"[；;\n]", _text("directions")) if part.strip()][:3]
    return RevisionSheet(
        failed_standards=failed,
        chosen=_text("chosen"),
        keep=_text("keep"),
        change=_text("change"),
        add_remove=_text("add_remove"),
        directions=directions,
    )


def console_deep_link(base_url: str, spec: GateSpec) -> str:
    """Build a stable, scoped console URL for one gate card.

    Feishu's card button used to point at the console root.  That forced a
    reviewer to pick the project and hunt through the approval list again.
    The link is still only a navigation hint (auth remains the console's
    responsibility), but it carries the exact project and checkpoint so the
    UI can land on the same decision surface.  Existing query parameters are
    preserved for deployments that use a reverse proxy or a locale switch.
    """
    raw = str(base_url or "").strip()
    checkpoint_id = str(getattr(spec, "checkpoint_id", "") or "").strip()
    if not raw or not checkpoint_id:
        return raw
    try:
        parsed = urlsplit(raw)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = "approvals"
        query["checkpoint_id"] = checkpoint_id
        project_id = str(getattr(spec, "project_id", "") or "").strip()
        if project_id:
            query["project_id"] = project_id
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        # A malformed configured URL must not make a gate card disappear.
        return raw


ORDER_INPUT_HIDDEN_KEYS = frozenset({"project_id", "customer_id", "environment_id", "org_id", "workflow_id"})


def order_inputs_from_fields(fields: dict[str, Any], *, limit: int = 8, width: int = 40) -> dict[str, str]:
    """The intake fields as the person filled them, for a card line: scoping ids and empty values left out."""
    out: dict[str, str] = {}
    for key, value in dict(fields or {}).items():
        name = str(key or "").strip()
        if not name or name in ORDER_INPUT_HIDDEN_KEYS or name.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            try:
                shown = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                shown = str(value)
        else:
            shown = str(value if value is not None else "")
        shown = " ".join(shown.split())
        if not shown:
            continue
        out[name] = shown if len(shown) <= width else shown[: width - 1] + "…"
        if len(out) >= limit:
            break
    return out


def order_line(spec: GateSpec) -> str:
    """"订单：wo-…（张记）· 下单填写：品牌名=张记茶楼、…" -- the order behind the node, as the person placed it."""
    label = spec.order_id or spec.order_title
    if not label:
        return ""
    line = f"订单：{label}"
    if spec.order_id and spec.order_title:
        line += f"（{spec.order_title}）"
    filled = "、".join(f"{key}={value}" for key, value in spec.order_inputs.items() if str(value or "").strip())
    if filled:
        # After a "（张记）" the dot follows the bracket; after a bare id it needs its own space ("wo-1 · 下单填写").
        line += ("· " if line.endswith("）") else " · ") + "下单填写：" + filled
    return line


CANNOT_PASS_NOTE = "交付物还没齐，这一版不能放行；请点「打回」写明要什么。"

# What a tool does, in the reviewer's words.  Path tools take ``path``; the verb is what the person would call it.
_PATH_TOOL_VERBS = {
    "list_dir": "查看", "glob": "查找", "grep": "搜索", "file_search": "搜索",
    "file_read": "读取", "read_file": "读取",
    "file_write": "写入", "write_file": "写入", "file_edit": "修改", "edit_file": "修改", "apply_patch": "修改",
}
_URL_TOOLS = {"browser_navigate": "打开网页", "web_fetch": "抓取网页"}
_COMMAND_TOOLS = {"bash", "powershell", "shell_exec", "shell", "execute", "run_command"}
_RISK_WORDS = {"low": "低", "medium": "中", "high": "高", "critical": "极高"}
OUTSIDE_ORDER_FOLDER = "订单文件夹外面的"


def describe_path(raw: Any, workspace_root: str) -> tuple[str, bool]:
    """(what to show, whether it leaves the order folder).

    Inside the order folder the path is shown relative to it (the folder itself as 订单文件夹); outside, the
    resolved place with the word 订单文件夹外面的 in front -- never the ``..\\..\\..`` the tool call carried, which
    the owner could not read (seen on the B5 run).
    """
    text = str(raw or "").strip()
    if not text:
        return "", False
    root = ""
    if workspace_root:
        try:
            root = os.path.normpath(os.path.abspath(str(workspace_root)))
        except (OSError, ValueError):
            root = ""
    candidate = text
    if root and not os.path.isabs(text):
        candidate = os.path.join(root, text)
    try:
        resolved = os.path.normpath(os.path.abspath(candidate)) if root or os.path.isabs(text) else os.path.normpath(text)
    except (OSError, ValueError):
        resolved = text
    if root:
        try:
            if os.path.commonpath([root, resolved]) == root:
                relative = os.path.relpath(resolved, root).replace(os.sep, "/")
                return ("订单文件夹" if relative == "." else relative), False
        except ValueError:
            pass  # different drives: outside
        return f"{OUTSIDE_ORDER_FOLDER} {resolved}", True
    # No root known: at least strip the leading ../ so the line reads as a place, not as a puzzle.
    parts = [part for part in re.split(r"[\\/]+", resolved) if part]
    ups = 0
    while parts and parts[0] == "..":
        parts.pop(0)
        ups += 1
    shown = "/".join(parts) or resolved
    if ups:
        return f"{OUTSIDE_ORDER_FOLDER} {shown}", True
    return shown, False


# The only argument keys whose values an unknown tool's card may show: a place (a url, a path, a selector) or what
# was searched for.  Anything else -- a token, a header, a body, a password -- is named on the card and never quoted.
_SHOWN_ARGUMENT_KEYS = frozenset({"url", "path", "query", "selector"})


def _argument_summary(arguments: dict[str, Any], *, limit: int = 200) -> str:
    parts: list[str] = []
    for key, value in arguments.items():
        name = str(key)
        if name not in _SHOWN_ARGUMENT_KEYS:
            parts.append(name)
            continue
        if isinstance(value, (dict, list)):
            try:
                shown = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                shown = str(value)
        else:
            shown = str(value)
        shown = shown.strip()
        parts.append(f"{name}={shown[:80]}" if shown else name)
    return "、".join(parts)[:limit]


def describe_permission(tool_name: str, arguments: dict[str, Any], workspace_root: str) -> tuple[str, bool]:
    """One sentence in the reviewer's words for a tool call: (sentence, whether it leaves the order folder)."""
    name = str(tool_name or "").strip()
    arguments = dict(arguments or {})
    if name in _URL_TOOLS:
        url = str(arguments.get("url") or arguments.get("target") or "").strip()
        return (f"{_URL_TOOLS[name]} {url}" if url else _URL_TOOLS[name]), False
    if name in _PATH_TOOL_VERBS:
        raw = arguments.get("path") or arguments.get("directory") or arguments.get("file_path") or arguments.get("pattern") or ""
        shown, outside = describe_path(raw, workspace_root)
        verb = _PATH_TOOL_VERBS[name]
        return (f"{verb} {shown}" if shown else f"{verb}文件"), outside
    if name in _COMMAND_TOOLS:
        command = arguments.get("command") or arguments.get("cmd") or arguments.get("argv") or arguments.get("script") or ""
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        command = str(command or "").strip()
        return (f"运行命令 {command[:200]}" if command else "运行一条命令"), False
    summary = _argument_summary(arguments)
    return (f"使用工具 {name}" + (f"（{summary}）" if summary else "")) if name else "使用一个工具", False


def permission_details(payload: dict[str, Any], workspace_root: str) -> dict[str, Any]:
    """The permission a checkpoint asks for, read from its payload, in the shape ``GateSpec.permission`` carries.

    ``tool_call`` names the tool and its arguments; ``approval`` carries the risk level and the reason; the
    interaction's options are the checkpoint's own answers, so the card's two buttons map to the option ids the
    runtime validates (``approve_once`` / ``deny`` -- found by id, never assumed).
    """
    payload = dict(payload or {})
    tool_call = dict(payload.get("tool_call", {}) or {})
    approval = dict(payload.get("approval", {}) or {})
    interaction = dict(payload.get("interaction", {}) or {})
    name = str(tool_call.get("name") or approval.get("action_name") or "").strip()
    arguments = tool_call.get("arguments")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    summary, outside = describe_permission(name, arguments, workspace_root)
    prompt = str(interaction.get("prompt", "") or "")
    risk = _RISK_WORDS.get(str(approval.get("risk_level", "") or "").strip().lower(), str(approval.get("risk_level", "") or "").strip())
    reason = str(approval.get("rationale", "") or "").strip()
    for line in prompt.splitlines():
        line = line.strip()
        if not risk and line.startswith("风险等级："):
            risk = line[len("风险等级："):].strip()
        elif not reason and line.startswith("原因："):
            reason = line[len("原因："):].strip()
    option_ids = [
        str(option.get("id", "") or "").strip()
        for option in list(interaction.get("options", []) or [])
        if isinstance(option, dict) and str(option.get("id", "") or "").strip()
    ]
    # "" when the checkpoint offers no approve-like option: the card then has no 允许这一次 button (an id the
    # runtime does not list would be refused as option_id_required anyway), only 拒绝, and says so.
    approve_option = next((oid for oid in option_ids if oid == "approve_once"), "") or next(
        (oid for oid in option_ids if "approve" in oid and "deny" not in oid), "")
    deny_option = next((oid for oid in option_ids if oid == "deny"), "") or next((oid for oid in option_ids if "deny" in oid), "deny")
    return {
        "tool": name,
        "arguments": {str(key): (value if isinstance(value, (str, int, float, bool)) else str(value)) for key, value in arguments.items()},
        "summary": summary,
        "outside": outside,
        "risk": risk,
        "reason": reason[:600],
        "approve_option": approve_option,
        "deny_option": deny_option,
    }


NO_APPROVE_OPTION_NOTE = "这次请求没有给出「允许」的选项，这里只能拒绝。"


def build_permission_card(spec: GateSpec, *, status_line: str = "") -> dict[str, Any]:
    """The card for an AI employee's permission request: what it wants, in words, and two answers on the card.

    No button leaves Feishu (owner's rule, : a request is settled where it is shown). The answers are
    the checkpoint's own options; ``GateService.on_event`` maps the button values onto them.
    """
    details = dict(spec.permission or {})
    summary = str(details.get("summary", "") or "") or (f"使用工具 {details.get('tool', '')}".strip())
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**这名 AI 员工想要：**{summary}"},
    ]
    risk_bits = []
    if details.get("risk"):
        risk_bits.append(f"风险等级：{details['risk']}")
    if details.get("reason"):
        risk_bits.append(f"原因：{details['reason']}")
    if risk_bits:
        elements.append({"tag": "markdown", "content": " · ".join(risk_bits)})
    order = order_line(spec)
    if order:
        elements.append({"tag": "markdown", "content": order})
    if status_line:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": status_line}]})
        header_color = "grey"
    else:
        value_base = {"gate": spec.checkpoint_id, "version": spec.version}
        actions: list[dict[str, Any]] = []
        if details.get("approve_option"):
            actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "允许这一次"}, "type": "primary", "value": {**value_base, "action": "permit_once"}})
        actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "拒绝"}, "type": "danger", "value": {**value_base, "action": "deny"}})
        elements.append({"tag": "action", "actions": actions})
        note = NO_APPROVE_OPTION_NOTE if not details.get("approve_option") else "允许只对这一次有效；拒绝后它不会做这件事。"
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
        header_color = "orange"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": spec.title}},
        "elements": elements,
    }


def build_card(spec: GateSpec, image_keys: list[str], *, console_url: str = "", status_line: str = "") -> dict[str, Any]:
    """Feishu interactive card (schema 1.0) for one gate version.

    ``console_url`` is kept for callers but no longer becomes a button: the review is settled on the card (owner's
    rule, 2026-09-25), so nothing on it leads out of Feishu.
    """
    del console_url
    standard_lines = "\n".join(f"☐ {item}" for item in spec.standard) or "（本节点未声明标准）"
    check_line = "自动检查：" + (f"{len(spec.present)} 个文件齐全" if not spec.precheck else "；".join(spec.precheck))
    elements: list[dict[str, Any]] = []
    order = order_line(spec)
    if order:
        elements.append({"tag": "markdown", "content": order})
    elements.extend([
        {"tag": "markdown", "content": f"**标准（本节点交付标准）**\n{standard_lines}"},
        {"tag": "markdown", "content": ("✅ " if not spec.precheck else "⚠️ ") + check_line},
    ])
    if spec.precheck and not status_line:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": CANNOT_PASS_NOTE}]})
    if spec.prompt:
        elements.append({"tag": "markdown", "content": spec.prompt[:1500]})
    for key in image_keys[:MAX_CARD_IMAGES]:
        elements.append({"tag": "img", "img_key": key, "alt": {"tag": "plain_text", "content": ""}, "mode": "fit_horizontal", "preview": True})
    for rel, text in list(spec.text_previews.items())[:3]:
        elements.append({"tag": "markdown", "content": f"**{rel}**\n{text}"})
    others = [p for p in spec.present if Path(p).suffix.lower() not in IMAGE_SUFFIXES and p not in spec.text_previews]
    if others:
        elements.append({"tag": "markdown", "content": "附件：" + "、".join(others[:12])})
    if status_line:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": status_line}]})
        header_color = "grey"
    else:
        value_base = {"gate": spec.checkpoint_id, "version": spec.version}
        actions: list[dict[str, Any]] = []
        if spec.present:
            actions.append({"tag": "button", "text": {"tag": "plain_text", "content": f"查看全部文件（{len(spec.present)}）"}, "type": "default", "value": {**value_base, "action": "view"}})
        if not spec.precheck:
            actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "放行"}, "type": "primary", "value": {**value_base, "action": "approve"}})
        actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "打回"}, "type": "danger", "value": {**value_base, "action": "reject"}})
        elements.append({"tag": "action", "actions": actions})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "点「打回」直接填写修改单；也可在本卡片话题回复一句「打回：……」。"}]})
        header_color = "orange" if spec.precheck else "blue"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": spec.title}},
        "elements": elements,
    }


LESSON_LEARN_LABEL = "学一轮（会花钱）"
LESSON_SKIP_LABEL = "不用了"


def lesson_card_text(spec: GateSpec) -> str:
    """The lesson card's question in its own buttons' words, with what learning costs (policy)."""
    what = "这一步的交付" if "这一步的交付" in str(spec.prompt or "") else "这次交付"
    return (
        f"{what}想给 AI 员工留点经验吗？点「学一轮」，AI 员工会花一轮时间学习{what}"
        "（最长约 40 分钟，会产生模型费用）；点「不用了」不花钱。"
    )


def build_lesson_card(spec: GateSpec, *, status_line: str = "") -> dict[str, Any]:
    """The lesson card (留点经验卡): two answers on the card, and what the first one costs.

    「学一轮（会花钱）」 is 完全同意 and starts the paid learning pass (up to about 40 minutes on a model); 「不用了」
    is 忽略 and costs nothing. There is no 打回 (policy, runtime contract: a send-back
    used to start the same paid pass as feedback). ``GateService.on_event`` maps the button values onto them.
    """
    elements: list[dict[str, Any]] = []
    order = order_line(spec)
    if order:
        elements.append({"tag": "markdown", "content": order})
    elements.append({"tag": "markdown", "content": lesson_card_text(spec)})
    if status_line:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": status_line}]})
        header_color = "grey"
    else:
        value_base = {"gate": spec.checkpoint_id, "version": spec.version}
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": LESSON_LEARN_LABEL}, "type": "primary", "value": {**value_base, "action": "learn"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": LESSON_SKIP_LABEL}, "type": "default", "value": {**value_base, "action": "no_lesson"}},
        ]})
        header_color = "blue"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": spec.title}},
        "elements": elements,
    }


# ── an order's 确认收货并结案 card (FC-1, policy) ──
#
# Once an order is in 客户验收中 (delivering) the group gets one card for it with one button, 确认收货并结案: the
# owner closes the order on the card, the same all-or-nothing close as the console's button
# (``acceptance_round.confirm_receipt_and_close``).  There is no checkpoint behind it: its gate_cards row is keyed
# ``order-close:<order_id>:<n>``, n counting the order's arrivals in 客户验收中 (a rework brings it back: a new card).
# The card is retired when the order is closed or cancelled, or leaves 客户验收中 (``order_close_retire_reason``).
ORDER_CLOSE_KIND = "order_close"
ORDER_CLOSE_PREFIX = "order-close:"
ORDER_CLOSE_LABEL = "确认收货并结案"
ORDER_CLOSE_DECISION = "close_order"
ORDER_CLOSE_NOTE = (
    "结案要么全部做完，要么什么都不记。"
    "结案时，这一单还开着的「留点经验」卡会一起关掉：不留经验，也不花钱。只有老板能点。"
)
ORDER_CLOSE_TYPED_REPLY = "这张卡只认「确认收货并结案」按钮，在话题里回复不会处理。"
ORDER_CLOSE_OWNER_ONLY = "只有老板能确认收货并结案。老板的飞书账号要先在设置里填好，否则飞书上谁都结不了案。"
ORDER_CLOSE_BUSY = "上一次点击还在处理，请稍等再看。"
ORDER_CLOSE_FAILED = "结案时出错了，这次可能没有做完；再点一次「确认收货并结案」就行。"
ORDER_CLOSE_UNAVAILABLE = "现在没法在飞书上结案，这次什么都没有记下。"
# What a press on a card whose order has moved on is told, by the reason the card is retired with.
ORDER_CLOSE_MOVED_ON = {
    "order_closed": "这张工单已经结案了。",
    "order_cancelled": "这张工单已经取消了。",
    "superseded": "这张卡已作废，请看新卡。",
    "void": "这张工单已经不在客户验收中了。",
}
# A press is handled under a claim on the card. A console stopped in the middle of one leaves the claim behind, and a
# claimed card is never retired; once the claim is older than DECISION_CLAIM_STALE_SECONDS and the order has moved on,
# it is given back and the card retired (``GateService.release_left_order_close_claim``). The card's record says so.
ORDER_CLOSE_CLAIM_RELEASED_EVENT = "claim_released"
ORDER_CLOSE_CLAIM_RELEASED = "上一次点击没有处理完（控制台中途停了），这张卡的占用已放开"


def order_close_key(order_id: str, spell: int = 1) -> str:
    """The gate_cards key of an order's close card for its ``spell``-th arrival in 客户验收中."""
    return f"{ORDER_CLOSE_PREFIX}{order_id}:{max(1, int(spell or 1))}"


def order_close_target(checkpoint_id: str) -> tuple[str, int] | None:
    """(order_id, spell) of an order's close-card key; None for any other card."""
    text = str(checkpoint_id or "")
    if not text.startswith(ORDER_CLOSE_PREFIX):
        return None
    order_id, _sep, spell = text[len(ORDER_CLOSE_PREFIX):].rpartition(":")
    if not order_id.strip() or not spell.isdigit():
        return None
    return order_id, int(spell)


def delivery_spell(events: list[dict[str, Any]] | None) -> int:
    """How many times the order has entered 客户验收中 (its ``delivering`` events), at least 1."""
    return max(1, sum(1 for event in list(events or []) if str(dict(event).get("kind", "") or "") == "delivering"))


def order_close_spec(order: Any, record: Any = None, *, spell: int = 1) -> GateSpec:
    """The close card of ``order`` (a work order) for its ``spell``-th arrival in 客户验收中; ``record`` is its acceptance
    record when it has one (the criteria are shown on the card)."""
    order_id = str(getattr(order, "order_id", "") or "")
    criteria = [str(item) for item in list(getattr(record, "criteria", None) or []) if str(item or "").strip()]
    return GateSpec(
        checkpoint_id=order_close_key(order_id, spell),
        checkpoint_type=ORDER_CLOSE_KIND,
        task_id=str(getattr(order, "launched_task_id", "") or ""),
        session_id="",
        waiting_task_id="",
        order_id=order_id,
        order_title=str(getattr(order, "customer_name", "") or getattr(order, "brand_name", "") or ""),
        node_id="",
        node_name=ORDER_CLOSE_LABEL,
        node_type=ORDER_CLOSE_KIND,
        assignee="",
        version=max(1, int(spell or 1)),
        standard=criteria,
        deliverables=[],
        revision_path="",
        workspace_root="",
        project_id=str(getattr(order, "project_id", "") or "default"),
        order_inputs=order_inputs_from_fields(dict(getattr(order, "fields", None) or {})),
        card_kind=ORDER_CLOSE_KIND,
    )


def build_order_close_card(spec: GateSpec, *, status_line: str = "") -> dict[str, Any]:
    """The close card: the order, its acceptance criteria, one button 「确认收货并结案」 and what pressing it does.

    Nothing on it leads out of Feishu (owner's rule); ``GateService.on_event`` maps the button onto the
    close. With ``status_line`` (closed on the card, or retired) the button goes and the line says why.
    """
    elements: list[dict[str, Any]] = []
    order = order_line(spec)
    if order:
        elements.append({"tag": "markdown", "content": order})
    if not status_line:
        elements.append({"tag": "markdown", "content": "**现在：客户验收中。**确认客户已经收到、没有问题，就点下面的按钮结案。"})
    if spec.standard:
        elements.append({"tag": "markdown", "content": "**验收标准**\n" + "\n".join(f"☐ {item}" for item in spec.standard)})
    if status_line:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": status_line}]})
        header_color = "grey"
    else:
        value = {"gate": spec.checkpoint_id, "version": spec.version, "action": ORDER_CLOSE_DECISION}
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": ORDER_CLOSE_LABEL}, "type": "primary", "value": value},
        ]})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": ORDER_CLOSE_NOTE}]})
        header_color = "green"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": spec.title}},
        "elements": elements,
    }


# ── a console extension's own card kind (``extensions.FeishuCardKind``) ──
#
# The console's own kinds: a node review, a permission request, a lesson card, an order's close card. Any other
# ``card_kind`` is a registered extension's (its builder, its buttons, its press handler, its retire rule).
_CONSOLE_CARD_KINDS = frozenset({"review", "permission", "lesson", ORDER_CLOSE_KIND})


def card_for(spec: GateSpec, image_keys: list[str], *, status_line: str = "") -> dict[str, Any]:
    """The right card for the spec: a permission request, a lesson card, an order's close card, a registered console
    extension's own card (``extensions.FeishuCardKind.build_card``), or a node review."""
    if spec.is_permission:
        return build_permission_card(spec, status_line=status_line)
    if spec.is_lesson:
        return build_lesson_card(spec, status_line=status_line)
    if spec.is_order_close:
        return build_order_close_card(spec, status_line=status_line)
    extension = spec.extension_card
    if extension is not None:
        return extension.build_card(spec, image_keys, status_line=status_line)
    return build_card(spec, image_keys, status_line=status_line)


# A decision's word for the person: the review card passes or sends back, the permission card allows or refuses,
# the lesson card learns or not.
DECISION_WORDS = {
    "approve": "放行", "reject": "打回", "permit_once": "允许", "deny": "拒绝", "learn": "学一轮", "no_lesson": "不用了",
    ORDER_CLOSE_DECISION: ORDER_CLOSE_LABEL,
}
# The answers that the runtime reads as "yes"; anything else it reads as "no".
YES_DECISIONS = frozenset({"approve", "permit_once", "learn"})
# The lesson card's two answers.
LESSON_DECISIONS = frozenset({"learn", "no_lesson"})


def decision_word(kind: str) -> str:
    """The owner's word for a decision: the console's own, or a registered extension card's button."""
    clean = str(kind or "")
    if clean in DECISION_WORDS:
        return DECISION_WORDS[clean]
    from ampuly.plugins.office_ui.extensions import feishu_decisions

    return feishu_decisions().get(clean, clean)


# ── a card whose checkpoint closed (C4-A, policy, Q9/Q12) ─
#
# When an owner card closes for any reason and nobody decided it on its Feishu card, the group card is retired: its
# buttons go and one line says why.  A reply or an upload on it is answered with RETIRED_REPLY and taken nowhere.
RETIRE_STATUS_LINES = {
    "answered": "已在控制台处理",
    "order_cancelled": "这张卡已作废：订单已取消",
    "order_closed": "这张卡已作废：订单已结案",
    "superseded": "这张卡已作废，请看新卡",
    "void": "这张卡已作废",
}
RETIRED_REPLY = "这张卡已作废"
CARD_RETIRED_EVENT = "card_retired"
CARD_RETIRE_FAILED_EVENT = "card_retire_failed"
# A card update Feishu refused (not a muted channel: that only waits) is tried on this many sweeps in all; after that
# the card stays retired here -- a reply on it is still answered 这张卡已作废 -- and its buttons stay in the group.
RETIRE_UPDATE_ATTEMPTS = 3
# Final statuses a checkpoint reaches only after someone answered it.
ANSWERED_CHECKPOINT_STATUSES = frozenset({"resolved", "outcome_unknown"})


def retire_reason(
    status: str,
    payload: dict[str, Any] | None,
    *,
    replaced_by: str = "",
    live_sibling: str = "",
    order_end: str = "",
) -> str:
    """Why a card whose checkpoint closed is retired: a key of ``RETIRE_STATUS_LINES``.

    In this order: the reason its closing wrote on it (the order cancelled, the order closed); a newer card named as
    its replacement (``replaced_by``: the checkpoint it was superseded by, or the card that asked it again after it was
    lost); an answer it holds (answered in the console or elsewhere); the order behind it has ended
    (``order_end``: "cancelled" / "closed"); another live card waiting on the same step (``live_sibling``); else void
    (stale, invalid, expired, timed out, or superseded with no newer card to point to).
    """
    from ampuly.plugins.office_ui.order_cancel import CANCELLED_PAUSE_REASON, CLOSED_ORDER_PAUSE_REASON

    data = dict(payload or {})
    said = {str(data.get(key, "") or "").strip() for key in ("checkpoint_resolution_reason", "administrative_reason")} - {""}
    if CANCELLED_PAUSE_REASON in said:
        return "order_cancelled"
    if any(text.startswith(CLOSED_ORDER_PAUSE_REASON) for text in said):
        return "order_closed"
    if str(replaced_by or "").strip():
        return "superseded"
    interaction = data.get("interaction")
    decision = interaction.get("decision") if isinstance(interaction, dict) else None
    if isinstance(decision, dict) or data.get("unclaimed_answer") or str(status or "").strip().lower() in ANSWERED_CHECKPOINT_STATUSES:
        return "answered"
    if order_end == "cancelled":
        return "order_cancelled"
    if order_end == "closed":
        return "order_closed"
    if str(live_sibling or "").strip():
        return "superseded"
    return "void"


def build_retired_card(title: str, status_line: str) -> dict[str, Any]:
    """A retired card when the gate can no longer be rebuilt (a restart, the task gone): its title and why, no buttons."""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": status_line}]}],
    }


# ── parsing what people send back ─────────────────────────────────────────


@dataclass
class RevisionSheet:
    failed_standards: list[str] = field(default_factory=list)
    chosen: str = ""
    keep: str = ""
    change: str = ""
    add_remove: str = ""
    directions: list[str] = field(default_factory=list)
    verbal: str = ""     # a plain sentence, when no template was used

    @property
    def is_empty(self) -> bool:
        return not any([self.failed_standards, self.chosen, self.keep, self.change, self.add_remove, self.directions, self.verbal])


_FIELD_ALIASES = {
    "不符合": "failed_standards", "标准": "failed_standards",
    # The plain form's three names, so a typed reply can use the same words the card shows.
    "哪里不对": "failed_standards", "不对": "failed_standards", "问题": "failed_standards",
    "选中": "chosen", "选中稿": "chosen",
    "保留": "keep", "保留什么": "keep",
    "修改": "change", "修改什么": "change", "改": "change", "改成": "change", "要改成什么": "change", "改成什么": "change",
    "新增删除": "add_remove", "新增": "add_remove", "删除": "add_remove", "新增/删除": "add_remove",
    "方向": "directions", "可选方向": "directions", "补充": "directions", "补充说明": "directions", "说明": "directions",
}


def parse_reject_text(text: str) -> RevisionSheet:
    """``打回｜不符合：a、b｜保留：…｜修改：…｜新增删除：…｜方向：x；y`` or a plain sentence.

    The plain form's words work too: ``打回｜哪里不对：…｜要改成什么：…｜补充说明：…``.
    """
    sheet = RevisionSheet()
    body = str(text or "").strip()
    for word in REJECT_WORDS:
        if body.startswith(word):
            body = body[len(word):].lstrip("：: ，,")
            break
    parts = [p.strip() for p in re.split(r"[｜|]", body) if p.strip()]
    structured = False
    for part in parts:
        match = re.match(r"^([^：:]{1,6})[：:]\s*(.*)$", part, re.S)
        if not match or match.group(1).strip() not in _FIELD_ALIASES:
            continue
        structured = True
        key = _FIELD_ALIASES[match.group(1).strip()]
        value = match.group(2).strip()
        if key in {"failed_standards", "directions"}:
            items = [v.strip() for v in re.split(r"[、，,;；\n]", value) if v.strip()]
            getattr(sheet, key).extend(items)
        else:
            setattr(sheet, key, value)
    if not structured:
        sheet.verbal = body
    return sheet


CHANNEL_LABELS = {"feishu": "飞书", "console": "控制台", "dingtalk": "钉钉", "wecom": "企业微信"}


def channel_label(channel: str) -> str:
    """Human name of the entry point a decision came through; unknown ids pass through."""
    clean = str(channel or "").strip().lower()
    return CHANNEL_LABELS.get(clean, clean or "控制台")


def render_revision_sheet(spec: GateSpec, sheet: RevisionSheet, *, by: str, at: datetime | None = None, channel: str = "feishu") -> str:
    """The durable feedback sheet the upstream role reworks against.

    The channel line records where the decision was actually made -- the
    console approval center and the Feishu card share this renderer, and an
    audit that always says 飞书 is wrong for every console decision.
    """
    stamp = (at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 修改单（{spec.order_id or spec.order_title} · {spec.node_name} · 第 {spec.version} 版）",
        "",
        f"审核人：{by}　时间：{stamp}　渠道：{channel_label(channel)}",
        "",
        "| 字段 | 内容 |",
        "|---|---|",
        f"| 不符合哪条标准 | {'、'.join(sheet.failed_standards) or ('（口头打回）' if sheet.verbal else '无')} |",
        f"| 选中稿 | {sheet.chosen or '无'} |",
        f"| 保留什么 | {sheet.keep or '无'} |",
        f"| 修改什么 | {sheet.change or sheet.verbal or '无'} |",
        f"| 新增 / 删除元素 | {sheet.add_remove or '无'} |",
        f"| 可选方向 | {'；'.join(sheet.directions) or '无'} |",
        "",
        "本版交付物：" + ("、".join(spec.present) if spec.present else "（无）"),
        "",
    ]
    return chr(10).join(lines)


@dataclass
class GateAction:
    kind: str               # approve | reject | view | read
    checkpoint_id: str
    actor_open_id: str
    event_id: str
    version: int = 0
    text: str = ""
    message_id: str = ""
    form: dict[str, Any] = field(default_factory=dict)
    upload: dict[str, Any] = field(default_factory=dict)


def _extension_card_kinds() -> tuple[Any, ...]:
    """The registered extension card kinds (``extensions.FeishuCardKind``)."""
    from ampuly.plugins.office_ui.extensions import feishu_card_kinds

    return feishu_card_kinds()


def _extension_decisions() -> dict[str, str]:
    """The buttons of every registered extension card kind (their action ids)."""
    from ampuly.plugins.office_ui.extensions import feishu_decisions

    return feishu_decisions()


def parse_event(event: dict[str, Any], *, message_to_checkpoint: Callable[[str], tuple[str, int] | None]) -> GateAction | None:
    """Turn a Feishu event (card action, thread reply, read receipt) into a gate action.

    ``message_to_checkpoint`` resolves a card message id to (checkpoint_id, version)
    so a thread reply can be attributed to its card. Unknown events yield None.
    """
    kind = str(event.get("type") or event.get("header", {}).get("event_type") or "").strip()
    event_id = str(event.get("event_id") or event.get("header", {}).get("event_id") or "").strip()
    body = dict(event.get("event", {}) or {})
    if kind == "card.action.trigger":
        value = dict((body.get("action", {}) or {}).get("value", {}) or {})
        gate = str(value.get("gate", "") or "").strip()
        action = str(value.get("action", "") or "").strip()
        if not gate or action not in {"approve", "reject", "reject_submit", "view", "permit_once", "deny", "learn", "no_lesson",
                                      ORDER_CLOSE_DECISION, *_extension_decisions()}:
            return None
        form_values = dict((body.get("action", {}) or {}).get("form_value", {}) or {})
        operator = dict(body.get("operator", {}) or {})
        return GateAction(
            kind=action,
            checkpoint_id=gate,
            actor_open_id=str(operator.get("open_id", "") or "").strip(),
            event_id=event_id or str(event.get("token", "") or ""),
            version=int(value.get("version", 0) or 0),
            message_id=str((body.get("context", {}) or {}).get("open_message_id", "") or ""),
            form=form_values,
        )
    if kind == "im.message.receive_v1":
        message = dict(body.get("message", {}) or {})
        parent = str(message.get("parent_id") or message.get("root_id") or "").strip()
        if not parent:
            return None
        resolved = message_to_checkpoint(parent)
        if resolved is None:
            return None
        try:
            text = str(json.loads(message.get("content", "") or "{}").get("text", "") or "").strip()
        except (TypeError, ValueError):
            text = ""
        message_type = str(message.get("message_type", "") or "")
        if message_type in {"file", "image", "media"}:
            try:
                content = dict(json.loads(message.get("content", "") or "{}"))
            except (TypeError, ValueError):
                content = {}
            sender = dict((body.get("sender", {}) or {}).get("sender_id", {}) or {})
            checkpoint_id, version = resolved
            return GateAction(
                kind="deliver",
                checkpoint_id=checkpoint_id,
                actor_open_id=str(sender.get("open_id", "") or "").strip(),
                event_id=event_id or str(message.get("message_id", "") or ""),
                version=version,
                message_id=str(message.get("message_id", "") or ""),
                upload={
                    "message_id": str(message.get("message_id", "") or ""),
                    "file_key": str(content.get("file_key") or content.get("image_key") or ""),
                    "file_name": str(content.get("file_name") or ""),
                    "type": "image" if message_type == "image" else "file",
                },
            )
        text = re.sub(r"@_user_\d+\s*", "", text).strip()
        lowered = text.lower()
        if any(lowered.startswith(w) for w in APPROVE_WORDS):
            action = "approve"
        elif any(lowered.startswith(w) for w in REJECT_WORDS):
            action = "reject"
        else:
            return None
        sender = dict((body.get("sender", {}) or {}).get("sender_id", {}) or {})
        checkpoint_id, version = resolved
        return GateAction(
            kind=action,
            checkpoint_id=checkpoint_id,
            actor_open_id=str(sender.get("open_id", "") or "").strip(),
            event_id=event_id or str(message.get("message_id", "") or ""),
            version=version,
            text=text,
            message_id=str(message.get("message_id", "") or ""),
        )
    if kind == "im.message.message_read_v1":
        reader = dict(body.get("reader", {}) or {})
        for message_id in list(body.get("message_id_list", []) or []):
            resolved = message_to_checkpoint(str(message_id))
            if resolved is not None:
                checkpoint_id, version = resolved
                return GateAction(
                    kind="read",
                    checkpoint_id=checkpoint_id,
                    actor_open_id=str((reader.get("reader_id", {}) or {}).get("open_id", "") or ""),
                    event_id=event_id,
                    version=version,
                    message_id=str(message_id),
                )
    return None


# ── Feishu REST client ────────────────────────────────────────────────────


class FeishuGateClient:
    def __init__(self, app_id: str, app_secret: str, *, http: httpx.AsyncClient | None = None, outbound_allowed: Any = None) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.outbound_allowed = outbound_allowed
        self._http = http
        self._token = ""
        self._token_expiry = 0.0

    async def _client(self) -> httpx.AsyncClient:
        from ampuly.plugins.office_ui.notifier import require_outbound_enabled

        require_outbound_enabled("feishu", self.outbound_allowed)
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def _token_value(self) -> str:
        import time

        if self._token and time.time() < self._token_expiry:
            return self._token
        http = await self._client()
        resp = await http.post(f"{FEISHU_API}/auth/v3/tenant_access_token/internal", json={"app_id": self.app_id, "app_secret": self.app_secret})
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu token: {data.get('msg') or data.get('code')}")
        self._token = str(data.get("tenant_access_token", "") or "")
        self._token_expiry = time.time() + int(data.get("expire", 7200) or 7200) - 60
        return self._token

    async def _headers(self) -> dict[str, str]:
        from ampuly.plugins.office_ui.notifier import require_outbound_enabled

        token = await self._token_value()
        require_outbound_enabled("feishu", self.outbound_allowed)
        return {"Authorization": f"Bearer {token}"}

    async def upload_image(self, path: Path) -> str:
        http = await self._client()
        with open(path, "rb") as fh:
            resp = await http.post(
                f"{FEISHU_API}/im/v1/images",
                headers=await self._headers(),
                data={"image_type": "message"},
                files={"image": (path.name, fh, "application/octet-stream")},
            )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu image upload: {data.get('msg')}")
        return str(data["data"]["image_key"])

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        http = await self._client()
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        resp = await http.post(
            f"{FEISHU_API}/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers=await self._headers(),
            json={"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu send card: {data.get('msg')}")
        return str(data["data"]["message_id"])

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        http = await self._client()
        resp = await http.patch(
            f"{FEISHU_API}/im/v1/messages/{message_id}",
            headers=await self._headers(),
            json={"content": json.dumps(card, ensure_ascii=False)},
        )
        resp.raise_for_status()
        # Feishu answers a refused update with HTTP 200 and a non-zero code (a message it no longer lets the bot edit,
        # say): read it as send_card does, so a card that was not changed is never taken as changed.
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu update card: {data.get('msg') or data.get('code')}")

    async def upload_file(self, path: Path) -> str:
        """A non-image deliverable becomes a Feishu file the person opens in place."""
        http = await self._client()
        suffix = path.suffix.lower().lstrip(".")
        file_type = {"pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "xls", "xlsx": "xls", "ppt": "ppt", "pptx": "ppt", "mp4": "mp4", "opus": "opus"}.get(suffix, "stream")
        with open(path, "rb") as fh:
            resp = await http.post(
                f"{FEISHU_API}/im/v1/files",
                headers=await self._headers(),
                data={"file_type": file_type, "file_name": path.name},
                files={"file": (path.name, fh, "application/octet-stream")},
            )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu file upload: {data.get('msg')}")
        return str(data["data"]["file_key"])

    async def reply_media(self, message_id: str, *, image_key: str = "", file_key: str = "") -> None:
        http = await self._client()
        if image_key:
            body = {"msg_type": "image", "content": json.dumps({"image_key": image_key})}
        else:
            body = {"msg_type": "file", "content": json.dumps({"file_key": file_key})}
        resp = await http.post(f"{FEISHU_API}/im/v1/messages/{message_id}/reply", headers=await self._headers(), json=body)
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu reply media: {data.get('msg')}")

    async def download_message_file(self, message_id: str, file_key: str, *, kind: str = "file") -> bytes:
        """Fetch a file someone posted in the card's thread."""
        http = await self._client()
        resp = await http.get(
            f"{FEISHU_API}/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": "image" if kind == "image" else "file"},
            headers=await self._headers(),
        )
        resp.raise_for_status()
        return resp.content

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> None:
        http = await self._client()
        resp = await http.post(
            f"{FEISHU_API}/im/v1/messages/{message_id}/reply",
            headers=await self._headers(),
            json={"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"feishu reply card: {data.get('msg')}")

    async def reply_text(self, message_id: str, text: str) -> None:
        http = await self._client()
        resp = await http.post(
            f"{FEISHU_API}/im/v1/messages/{message_id}/reply",
            headers=await self._headers(),
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
        resp.raise_for_status()


# ── the service ───────────────────────────────────────────────────────────

ReplyFn = Callable[[GateSpec, str, str, str], Awaitable[bool]]   # (spec, decision, note, actor_login) -> accepted
ReplyWithIdentityFn = Callable[[GateSpec, str, str, dict[str, Any]], Awaitable[bool]]
EmployeeByOpenId = Callable[[str], dict[str, Any] | None]        # open_id -> {employee_id, name, login, role}
# (open_id, the gate's spec) -> the same person, as the organization of the gate's own project knows them (or an
# awaitable of it); None when they are nobody there. A card the duty round sent for a project not on screen is
# answered by the people of that project (P-a).
EmployeeForGate = Callable[[str, "GateSpec"], Any]
CanAnswer = Callable[[GateSpec, dict[str, Any]], bool]
# (spec, decision, note) -> True: the checkpoint already holds exactly this
# answer; False: it holds none while still waiting, or another one; None:
# cannot tell.  Read-only -- it never answers the checkpoint.
LookBackFn = Callable[[GateSpec, str, str], Awaitable["bool | None"]]
# (checkpoint_id, checkpoint_type) -> the gate's spec rebuilt from the
# checkpoint whatever its status (answered too), or None.
SpecForFn = Callable[[str, str], Awaitable["GateSpec | None"]]
# (close card's spec, the owner as the directory knows them) -> {"closed": True} when the order is closed now, or
# {"refused": "<why, in the owner's words>"} when nothing was recorded (FC-1; ws_handler._gate_close_order).
CloseOrderFn = Callable[[GateSpec, dict[str, Any]], Awaitable[dict[str, Any]]]


class GateService:
    """Announce gates as cards; turn people's replies into checkpoint answers."""

    def __init__(
        self,
        *,
        client: FeishuGateClient | None,
        chat_id: str,
        business: Any,
        reply: ReplyFn,
        employee_by_open_id: EmployeeByOpenId,
        can_answer: CanAnswer,
        reply_with_identity: ReplyWithIdentityFn | None = None,
        console_url: str = "",
        spec_loader: Any = None,
        look_back: LookBackFn | None = None,
        close_order: CloseOrderFn | None = None,
        employee_for_gate: EmployeeForGate | None = None,
        card_hooks: Any = (),
    ) -> None:
        self.client = client
        # The console-bound hooks of the registered extension card kinds (``extensions.feishu_card_hooks``), by kind:
        # each rebuilds its cards' specs (``load_spec``) and answers their buttons (``answer``); a kind with none here
        # rebuilds no card and its buttons do nothing but say so.
        self._card_hooks: dict[str, Any] = {
            str(getattr(hooks, "card_kind", "") or ""): hooks for hooks in list(card_hooks or ())
        }
        # Who a person is for one gate, in the gate's own project (P-a); without it the one directory answers.
        self._employee_for_gate = employee_for_gate
        # Closes an order from its 确认收货并结案 card (FC-1); without one the card's button closes nothing.
        self._close_order = close_order
        # Settles an answer cut off on its way to the runtime (see decide);
        # without one such an answer stays "not known" until a caller that
        # has one looks (the console's click, the duty watch's pass).
        self.look_back = look_back
        # The spec of each answer cut off in this process, by claim token: the
        # duty watch's pass writes a taken 打回's sheet with it.
        self._cut_off_specs: dict[str, GateSpec] = {}
        # Rebuilds a spec from durable state when this process never saw the
        # card go out (the console restarted between sending and the click).
        self._spec_loader = spec_loader
        self._reply_with_identity = reply_with_identity
        self.chat_id = chat_id
        self.business = business
        self._reply = reply
        self._employee_by_open_id = employee_by_open_id
        self._can_answer = can_answer
        self.console_url = console_url
        self._specs: dict[str, GateSpec] = {}
        self._seen_events: set[str] = set()
        # The last few people/chats that talked to the bot: the settings page
        # shows them so the owner can copy a chat id or an open_id instead of
        # hunting for it in the Feishu admin.
        self.recent_senders: list[dict[str, Any]] = []

    def remember(self, spec: GateSpec) -> None:
        self._specs[spec.checkpoint_id] = spec

    def spec_for(self, checkpoint_id: str) -> GateSpec | None:
        return self._specs.get(checkpoint_id)

    async def load_spec(self, checkpoint_id: str) -> GateSpec | None:
        spec = self._specs.get(checkpoint_id)
        if spec is None and order_close_target(checkpoint_id) is not None:
            # An order's close card has no checkpoint: it is rebuilt from the order, never looked for in a store.
            spec = self._order_close_spec_from_order(checkpoint_id)
            if spec is not None:
                self._specs[checkpoint_id] = spec
            return spec
        kind = self.extension_card_kind(checkpoint_id) if spec is None else None
        if kind is not None:
            # An extension's card has no checkpoint either: rebuilt by the hooks of its kind.
            loader = getattr(self._card_hooks.get(kind.name), "load_spec", None)
            if not callable(loader):
                return None
            try:
                spec = await loader(checkpoint_id)
            except Exception:
                logger.opt(exception=True).warning("feishu gate: a {} card could not be rebuilt", kind.name)
                return None
            if spec is not None:
                self._specs[checkpoint_id] = spec
            return spec
        if spec is not None or not callable(self._spec_loader):
            return spec
        try:
            spec = await self._spec_loader(checkpoint_id)
        except Exception:
            logger.opt(exception=True).warning("feishu gate: could not rebuild spec for checkpoint {}", checkpoint_id[:8])
            return None
        if spec is not None:
            self._specs[checkpoint_id] = spec
        return spec

    def message_to_checkpoint(self, message_id: str) -> tuple[str, int] | None:
        row = self.business.gate_card_by_message(message_id)
        if row is None:
            return None
        return str(row["checkpoint_id"]), int(row["version"] or 0)

    async def announce(self, spec: GateSpec) -> str:
        """Send the card for one gate version; idempotent per checkpoint."""
        self.remember(spec)
        existing = self.business.gate_card(spec.checkpoint_id)
        if existing is not None and existing.get("message_id"):
            return str(existing["message_id"])
        if self.client is None:
            raise RuntimeError("feishu gate client not configured")
        image_keys: list[str] = []
        root = Path(spec.workspace_root) if spec.workspace_root and not spec.is_permission else None
        for rel in spec.present:
            if root is None or Path(rel).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                image_keys.append(await self.client.upload_image(root / rel))
            except Exception:
                continue  # the card still goes out with the file listed
        card = card_for(spec, image_keys)
        message_id = await self.client.send_card(self.chat_id, card)
        self.business.record_gate_card(
            checkpoint_id=spec.checkpoint_id,
            message_id=message_id,
            chat_id=self.chat_id,
            task_id=spec.task_id,
            order_id=spec.order_id,
            version=spec.version,
        )
        self.business.record_gate_event(spec.checkpoint_id, "sent", actor="", note={"version": spec.version, "images": len(image_keys)})
        return message_id

    async def _person_for_gate(self, open_id: str, spec: GateSpec | None) -> dict[str, Any]:
        """Who ``open_id`` is for this gate: in the gate's own project when the console can say (``employee_for_gate``;
        P-a), else -- no spec, no such lookup, or a lookup that failed -- by the one directory, as before. {} = nobody."""
        import inspect

        if spec is not None and callable(self._employee_for_gate):
            try:
                found = self._employee_for_gate(open_id, spec)
                if inspect.isawaitable(found):
                    found = await found
                return dict(found or {})
            except Exception:
                logger.opt(exception=True).warning("feishu gate: who {} is in the project of checkpoint {} could not be read; the directory answers",
                                                   str(open_id or "")[:10], spec.checkpoint_id[:8])
        return dict(self._employee_by_open_id(open_id) or {})

    def _note_sender(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or event.get("header", {}).get("event_type") or "")
        body = dict(event.get("event", {}) or {})
        open_id, chat_id, text = "", "", ""
        if kind == "im.message.receive_v1":
            message = dict(body.get("message", {}) or {})
            chat_id = str(message.get("chat_id", "") or "")
            open_id = str((body.get("sender", {}) or {}).get("sender_id", {}).get("open_id", "") or "")
            try:
                text = str(json.loads(message.get("content", "") or "{}").get("text", "") or "")[:60]
            except (TypeError, ValueError):
                text = ""
        elif kind == "card.action.trigger":
            open_id = str((body.get("operator", {}) or {}).get("open_id", "") or "")
            chat_id = str((body.get("context", {}) or {}).get("open_chat_id", "") or "")
        if not open_id and not chat_id:
            return
        self.recent_senders.insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "open_id": open_id, "chat_id": chat_id, "text": text})
        del self.recent_senders[10:]

    async def on_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        self._note_sender(event)
        action = parse_event(event, message_to_checkpoint=self.message_to_checkpoint)
        if action is None:
            return None
        if action.event_id and action.event_id in self._seen_events:
            return {"ignored": "duplicate"}
        if action.event_id:
            self._seen_events.add(action.event_id)
        # A retired card is answered before anything else is looked up: no decision, no upload, no files (C4-A).
        retired = self._retired_card(action.checkpoint_id)
        if retired is not None:
            return await self._answer_retired(action, retired)
        spec = await self.load_spec(action.checkpoint_id)
        if spec is not None and spec.is_order_close:
            return await self._on_order_close_event(spec, action)
        extension = spec.extension_card if spec is not None else None
        if extension is not None:
            # An extension's own card: its press handler, with the hooks this console bound for its kind.
            return await extension.on_event(self, spec, action, self._card_hooks.get(extension.name))
        if action.kind == ORDER_CLOSE_DECISION:
            # 确认收货并结案 belongs to an order's close card only: on any other card it is never read as an answer.
            return {"ignored": "unknown_gate" if spec is None else "not_an_order_close_card"}
        owner = next((kind for kind in _extension_card_kinds() if action.kind in kind.decisions), None)
        if owner is not None:
            # An extension card's buttons belong to that card only: never read as an answer anywhere else.
            return {"ignored": "unknown_gate" if spec is None else owner.not_this_card}
        if action.kind == "read":
            # Everyone in the group gets the read receipt; only the person the
            # gate is assigned to (or someone allowed to decide it) having read
            # it counts as "looked at before deciding".  Check before recording.
            if spec is None:
                return {"ignored": "unknown_gate"}
            reader = await self._person_for_gate(action.actor_open_id, spec)
            if not reader or not self._can_answer(spec, reader):
                return {"ignored": "not_a_reviewer"}
            self.business.record_gate_event(action.checkpoint_id, "read", actor=action.actor_open_id)
            return {"recorded": "read"}
        if action.kind == "deliver":
            if spec is None:
                return {"ignored": "unknown_gate"}
            person = await self._person_for_gate(action.actor_open_id, spec)
            if not person or not self._can_answer(spec, person):
                self.business.record_gate_event(action.checkpoint_id, "unauthorized_attempt", actor=action.actor_open_id, note={"kind": "deliver"})
                return {"ignored": "unauthorized"}
            saved = await self.store_upload(spec, action)
            if not saved:
                return {"error": "upload_not_saved"}
            self.business.record_gate_event(action.checkpoint_id, "delivered_file", actor=str(person.get("employee_id") or person.get("name") or ""), note={"path": saved})
            if self.client is not None and action.message_id:
                try:
                    await self.client.reply_text(action.message_id, f"已收下：{saved}。交付齐了就点「放行」。")
                except Exception:
                    pass
            return {"stored": saved}
        if spec is None:
            logger.warning("feishu gate: {} on checkpoint {} ignored: no spec (checkpoint gone or never carded)", action.kind, action.checkpoint_id[:8])
            return {"ignored": "unknown_gate"}
        person = await self._person_for_gate(action.actor_open_id, spec)
        if not person or not self._can_answer(spec, person):
            self.business.record_gate_event(action.checkpoint_id, "unauthorized_attempt", actor=action.actor_open_id, note={"kind": action.kind})
            logger.warning("feishu gate: {} on checkpoint {} by {} refused: not the assignee or owner", action.kind, action.checkpoint_id[:8], action.actor_open_id[:10])
            return {"ignored": "unauthorized"}
        if spec.is_permission:
            # A permission request has two answers and no revision sheet: its buttons, and a 同意 / 打回 typed in
            # its thread, all land on the checkpoint's own options.
            if action.kind in {"view", "reject_submit"}:
                return {"ignored": "not_a_review"}
            kind = "permit_once" if action.kind in {"approve", "permit_once"} else "deny"
            return await self.decide(spec, kind, person=person, channel="feishu")
        if spec.is_lesson:
            # The lesson card: 学一轮（会花钱） / 不用了.  A 放行 / 同意 (typed in its thread, or the 放行 button of a card
            # sent Previously) is 学一轮; anything else, a 打回 included, is 不用了 -- never feedback.
            if action.kind in {"view", "reject_submit"}:
                return {"ignored": "not_a_review"}
            kind = "learn" if action.kind in YES_DECISIONS else "no_lesson"
            return await self.decide(spec, kind, person=person, channel="feishu")
        if action.kind in LESSON_DECISIONS:
            return {"ignored": "not_a_lesson_card"}
        if action.kind in {"permit_once", "deny"}:
            return {"ignored": "not_a_permission"}
        if action.kind == "view":
            # Viewing may upload and send local files. It needs the same
            # server-side identity/scope check as decisions, before any IO.
            self.business.record_gate_event(action.checkpoint_id, "viewed", actor=action.actor_open_id)
            sent = await self.send_deliverables(spec, self.business.gate_card(action.checkpoint_id) or {})
            return {"recorded": "viewed", "files_sent": sent}
        if action.kind == "reject" and not action.text and self.client is not None:
            # The button opens the form; the form's submit carries the fields.
            try:
                await self.client.reply_card(str(action.message_id or (self.business.gate_card(spec.checkpoint_id) or {}).get("message_id", "")), build_reject_form(spec))
                return {"opened": "reject_form"}
            except Exception:
                logger.opt(exception=True).warning("feishu gate: could not open the reject form; falling back to a text reply")
        if action.kind == "reject_submit":
            sheet = sheet_from_form(spec, action.form)
            if not sheet.change.strip():
                return {"ignored": "empty_sheet"}
            return await self.decide(spec, "reject", person=person, sheet=sheet, channel="feishu")
        sheet = (parse_reject_text(action.text) if action.text else RevisionSheet()) if action.kind == "reject" else None
        return await self.decide(spec, action.kind, person=person, sheet=sheet, channel="feishu")

    async def send_deliverables(self, spec: GateSpec, card_row: dict[str, Any]) -> int:
        """"查看交付物": every deliverable goes into the card's thread as an image or a file, once."""
        if self.client is None or not card_row.get("message_id"):
            return 0
        events = self.business.gate_events(spec.checkpoint_id)
        sent_paths = {
            str(e.get("note", {}).get("path", "") or "")
            for e in events
            if e.get("kind") == "file_sent"
        }
        legacy_complete = any(
            e.get("kind") == "files_sent" and not e.get("note", {}).get("paths")
            for e in events
        )
        if legacy_complete or (spec.present and all(rel in sent_paths for rel in spec.present)):
            try:
                await self.client.reply_text(str(card_row["message_id"]), "交付物已在本话题里（往上翻）。")
            except Exception:
                pass
            return 0
        root = Path(spec.workspace_root) if spec.workspace_root else None
        sent = 0
        for rel in spec.present:
            if rel in sent_paths:
                continue
            if root is None:
                break
            path = _workspace_path(str(root), rel)
            if path is None or not path.is_file():
                logger.warning("feishu gate: refused missing or out-of-workspace deliverable {}", rel)
                continue
            try:
                if path.suffix.lower() in IMAGE_SUFFIXES:
                    await self.client.reply_media(str(card_row["message_id"]), image_key=await self.client.upload_image(path))
                else:
                    await self.client.reply_media(str(card_row["message_id"]), file_key=await self.client.upload_file(path))
                sent += 1
                sent_paths.add(rel)
                self.business.record_gate_event(spec.checkpoint_id, "file_sent", actor="", note={"path": rel})
            except Exception:
                logger.opt(exception=True).warning("feishu gate: could not send deliverable {}", rel)
        if spec.present and all(rel in sent_paths for rel in spec.present):
            self.business.record_gate_event(
                spec.checkpoint_id,
                "files_sent",
                actor="",
                note={"count": len(sent_paths), "paths": sorted(sent_paths)},
            )
        return sent

    async def _seen_by_reviewer(self, spec: GateSpec, person: dict[str, Any]) -> bool:
        """Did the gate's assignee, or someone allowed to decide it, look at this version?

        Read receipts arrive for everyone in the group, and the console
        records whoever opens the panel.  Only the gate's own reviewers
        count toward "looked at before approving".  An event's actor is an
        open_id (Feishu) or an employee id / login (console).  Files the bot
        posted into the thread prove nothing by themselves.  An open_id is
        looked up in the gate's own project (``_person_for_gate``; P-a).
        """
        own = {str(person.get(key, "") or "").strip() for key in ("employee_id", "login")} - {""}
        assignee = str(spec.assignee or "").strip()
        for event in self.business.gate_events(spec.checkpoint_id):
            if event.get("kind") not in SEEN_EVENT_KINDS:
                continue
            who = str(event.get("actor", "") or "").strip()
            if who and (who in own or (assignee and who == assignee)):
                return True
            note = event.get("note") if isinstance(event.get("note"), dict) else {}
            role = str(note.get("role", "") or "").strip()
            if role:
                # The console records who looked -- the owner or an employee,
                # and which employee -- from the signed-in account.  A login
                # name alone cannot be matched back to a role, so without this
                # the owner's look never counted.
                viewer = {"role": role, "employee_id": str(note.get("employee_id", "") or "").strip(), "login": who}
                try:
                    if self._can_answer(spec, viewer):
                        return True
                except Exception:
                    logger.opt(exception=True).debug("gate: could not check viewer {} of checkpoint {}", who[:10], spec.checkpoint_id[:8])
                continue
            if not who:
                continue
            try:
                viewer = await self._person_for_gate(who, spec)
                if viewer and self._can_answer(spec, viewer):
                    return True
            except Exception:
                logger.opt(exception=True).debug("gate: could not resolve viewer {} of checkpoint {}", who[:10], spec.checkpoint_id[:8])
        return False

    async def decide(
        self,
        spec: GateSpec,
        kind: str,
        *,
        person: dict[str, Any],
        sheet: RevisionSheet | None = None,
        exception_note: str = "",
        channel: str = "console",
        look_back: LookBackFn | None = None,
    ) -> dict[str, Any]:
        """Approve or reject one gate version on behalf of ``person``; same path from the group and the console.

        The order is the fix for three live faults : a console
        gate could be approved twice (only a Feishu card row was ever marked
        decided), a double click could decide twice (read, then wait for the
        runtime, then write), and a reject the runtime refused had already
        overwritten the revision sheet on disk.

        1. Claim the decision in business.db with one conditional update.
           That update succeeding is the only "I am first"; it works with or
           without a card, so the console path is covered too. An earlier
           answer whose fate is not known is settled first (see step 2).
        2. Tell the runtime. A refusal gives the claim back, so the gate
           can still be decided by the next click. A call cut off part-way
           (an error, a cancelled connection, shutdown) does not: the
           runtime may have taken the answer. The whole answer -- a 打回's
           full sheet -- is recorded at once, then ``look_back`` reads the
           checkpoint: still waiting, the claim is given back; holding this
           very answer, it is finished as in step 3; cannot tell, the claim
           and the record stay. A later click looks again only while the
           gate still waits; a gate the runtime has answered is no longer
           offered, and the duty watch's pass settles it
           (``settle_cut_off_answers``). Nothing is sent again meanwhile.
        3. Only then write a reject's revision sheet, stamp the decision
           final, record it and update the card. A refused reject never
           touches the sheet on disk.

        Refusals: ``already_decided`` (a decision is final),
        ``decision_in_progress`` (another click's answer is with the runtime
        right now), ``reply_outcome_unknown`` (an answer was cut off and
        nobody can tell yet whether the runtime took it),
        ``checkpoint_reply_not_accepted`` (the runtime did not take it).
        """
        look = look_back or self.look_back
        actor = str(person.get("name") or person.get("login") or "")
        login = str(person.get("login", "") or actor)
        decided_by = str(person.get("employee_id") or actor)
        sheet_text = ""
        if spec.is_permission:
            # 允许这一次 / 拒绝: the runtime's yes / no on the checkpoint's own options; no sheet, no "looked at" rule.
            kind = "permit_once" if kind in YES_DECISIONS else "deny"
            answer = "approve" if kind == "permit_once" else "reject"
            note = exception_note or ""
        elif spec.is_lesson:
            # 学一轮 / 不用了: 完全同意 or 忽略 on the lesson card; no sheet, no "looked at" rule.  From the console's
            # gate panel too: its 打回 is 不用了 here, never feedback that would start the paid pass.
            kind = "learn" if kind in YES_DECISIONS else "no_lesson"
            answer = "approve" if kind == "learn" else "reject"
            note = exception_note or ""
        elif kind == "approve":
            answer = "approve"
            seen_read = await self._seen_by_reviewer(spec, person)
            note = exception_note or ("" if seen_read else "未查看即放行")
        else:
            kind, answer = "reject", "reject"
            sheet = sheet or RevisionSheet()
            sheet_text = render_revision_sheet(spec, sheet, by=actor, channel=channel)
            note = sheet.change or sheet.verbal or "见修改单"
        held = await self._settle_earlier_answer(spec, look)
        if held is not None:
            return held

        def claim() -> str:
            return self.business.claim_gate_decision(
                spec.checkpoint_id,
                decision=kind,
                decided_by=decided_by,
                note=note,
                task_id=spec.task_id,
                order_id=spec.order_id,
                version=spec.version,
                stale_after_seconds=DECISION_CLAIM_STALE_SECONDS,
            )

        token = claim()
        if not token:
            busy = self._why_not_free(spec.checkpoint_id)
            if busy is not None:
                return busy
            # The claim this one lost to was given back in between (the
            # runtime refused that answer): the gate is free, try once more.
            token = claim()
            if not token:
                return self._why_not_free(spec.checkpoint_id) or {"ignored": "decision_in_progress"}
        answered = {"decision": kind, "decision_note": note, "sheet": sheet_text, "actor": actor, "by": decided_by, "channel": channel}
        try:
            if self._reply_with_identity is not None:
                accepted = await self._reply_with_identity(spec, answer, note, dict(person))
            else:
                accepted = await self._reply(spec, answer, note, login)
        except BaseException as exc:
            # The runtime may have taken it: keep the claim and the whole answer first.
            self._record_cut_off(spec, token, answered, cause=type(exc).__name__)
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise  # the process is going down: nothing more may be awaited
            outcome = await self._settle_cut_off(spec, token, answered, look, update_card=isinstance(exc, Exception))
            if not isinstance(exc, Exception):
                raise  # cancelled: settled as far as a look could, the cancellation goes on
            logger.opt(exception=exc).warning(
                "gate: {} on checkpoint {} was cut off on its way to the runtime; settled as {}",
                kind, spec.checkpoint_id[:8], outcome.get("decided") or outcome.get("error") or outcome.get("ignored"),
            )
            return outcome
        if not accepted:
            self.business.release_gate_decision(spec.checkpoint_id, token)
            logger.warning("gate: {} on checkpoint {} by {} was not accepted by the runtime", kind, spec.checkpoint_id[:8], actor)
            return {"error": "checkpoint_reply_not_accepted"}
        return await self._finish(spec, token, answered)

    async def _finish(
        self, spec: GateSpec, token: str, answered: dict[str, Any], *, update_card: bool = True, settling: bool = False,
    ) -> dict[str, Any]:
        """The runtime holds this answer: write a 打回's sheet, stamp the decision final, record it, close the card.

        ``settling`` is an answer cut off on its way that a look has just
        found taken.  Two looks can land together, so there the claim is
        stamped final first and only the look that stamps it goes on -- one
        sheet, one record.
        """
        kind = str(answered.get("decision", "") or "")
        note = str(answered.get("decision_note", "") or "")
        actor = str(answered.get("actor", "") or "")
        decided_by = str(answered.get("by", "") or actor)
        channel = str(answered.get("channel", "") or "console")
        if settling and not self.business.finish_gate_decision(spec.checkpoint_id, token):
            # Another look settled it first: finished, or given back as not taken.
            return self._why_not_free(spec.checkpoint_id) or {"error": "checkpoint_reply_not_accepted"}
        at = datetime.now().strftime("%H:%M")
        written = ""
        if kind == "approve":
            status = f"已放行 · {actor} · {at}" + (f"（{note}）" if note else "")
        elif kind in {"permit_once", "deny"}:
            status = f"{'已允许这一次' if kind == 'permit_once' else '已拒绝'} · {actor} · {at}" + (f"（{note}）" if note else "")
        elif kind in LESSON_DECISIONS:
            chosen = "已选学一轮：AI 员工开始学习，会产生模型费用" if kind == "learn" else "已选不用了：不学，不花钱"
            status = f"{chosen} · {actor} · {at}" + (f"（{note}）" if note else "")
        else:
            # Settled late, a sheet written after this claim belongs to a later 打回: kept beside it, not over it.
            written = self._write_revision(
                spec, str(answered.get("sheet", "") or ""), not_newer_than=self._claimed_at(token) if settling else None,
            )
            status = f"已打回 · {actor} · {at} · " + ("修改单已写入 " + written if written else "修改单未能写入")
        if not settling and not self.business.finish_gate_decision(spec.checkpoint_id, token):
            # Only possible when the claim sat unfinished past the stale
            # limit and someone took it over; the runtime did accept this one.
            logger.warning("gate: {} on checkpoint {} accepted, but its claim was taken over meanwhile", kind, spec.checkpoint_id[:8])
        self.business.record_gate_event(spec.checkpoint_id, kind, actor=decided_by, note={"note": note, "channel": channel})
        if settling:
            if kind == "approve":
                told = "已核对：系统已收下这次放行"
            elif kind in {"permit_once", "deny"}:
                told = f"已核对：系统已收下这次{decision_word(kind)}"
            elif kind in LESSON_DECISIONS:
                told = f"已核对：系统已收下「{decision_word(kind)}」"
            elif not written:
                told = "已核对：系统已收下这次打回，修改单没能写入，全文仍在上一条记录里"
            elif written != spec.revision_path:
                told = f"已核对：系统已收下这次打回。之后已经有一份更新的修改单，这一份没有盖掉它，另存为 {written}"
            else:
                told = "已核对：系统已收下这次打回，修改单已补写"
            self.business.record_gate_event(spec.checkpoint_id, OUTCOME_SETTLED_EVENT, actor="", note={"claim": token, "taken": True, "note": told})
        card_row = self.business.gate_card(spec.checkpoint_id) or {}
        if update_card and self.client is not None and card_row.get("message_id"):
            try:
                await self.client.update_card(str(card_row["message_id"]), card_for(spec, [], status_line=status))
            except Exception:
                pass
        return {"decided": kind, "by": actor}

    def _claim_prefix(self) -> str:
        return str(getattr(self.business, "GATE_CLAIM_PREFIX", "") or "claimed:")

    def _claimed_at(self, token: str) -> float | None:
        """When a claim was taken, from its token (prefix, local timestamp, ":", random); None when unreadable."""
        body = str(token or "")[len(self._claim_prefix()):]
        try:
            return datetime.fromisoformat(body.rsplit(":", 1)[0]).timestamp()
        except (ValueError, OverflowError, OSError):
            return None

    def _record_cut_off(self, spec: GateSpec, token: str, answered: dict[str, Any], *, cause: str) -> None:
        """Keep the whole answer before anything else can fail: the runtime may have taken it."""
        decision = str(answered.get("decision", "") or "")
        is_reject = decision == "reject"
        self._cut_off_specs[token] = spec
        note = {
            "note": decision_word(decision) + "已提交，但还没能确认系统是否收下"
            + ("；修改单全文已保存在这条记录里，系统会自动核对" if is_reject else "，系统会自动核对"),
            "claim": token,
            **answered,
            "checkpoint_type": spec.checkpoint_type,
            "card_kind": spec.card_kind,
            "revision_path": spec.revision_path if is_reject else "",
            "version": spec.version,
            "cause": cause,
        }
        try:
            self.business.record_gate_event(spec.checkpoint_id, OUTCOME_UNKNOWN_EVENT, actor=str(answered.get("by", "") or ""), note=note)
        except Exception:
            logger.opt(exception=True).error("gate: could not record the cut-off answer on checkpoint {}", spec.checkpoint_id[:8])

    def _cut_off_answer(self, checkpoint_id: str, token: str) -> dict[str, Any] | None:
        """The answer recorded when the claim ``token`` was cut off -- while it is not settled yet."""
        found: dict[str, Any] | None = None
        for event in self.business.gate_events(checkpoint_id):
            note = event.get("note")
            if not isinstance(note, dict) or str(note.get("claim", "") or "") != token:
                continue
            if event.get("kind") == OUTCOME_UNKNOWN_EVENT:
                found = dict(note)
            elif event.get("kind") == OUTCOME_SETTLED_EVENT:
                found = None
        return found

    async def _look(self, spec: GateSpec, look: LookBackFn | None, answered: dict[str, Any]) -> bool | None:
        """Ask ``look`` (read-only) whether the checkpoint holds this answer; None when that cannot be told."""
        if look is None:
            return None
        decision = "approve" if answered.get("decision") in YES_DECISIONS else "reject"
        try:
            verdict = await asyncio.wait_for(look(spec, decision, str(answered.get("decision_note", "") or "")), LOOK_BACK_TIMEOUT_SECONDS)
        except Exception:
            logger.opt(exception=True).warning("gate: could not look at checkpoint {} to settle a cut-off answer", spec.checkpoint_id[:8])
            return None
        return verdict if verdict is True or verdict is False else None

    async def _settle_cut_off(
        self, spec: GateSpec, token: str, answered: dict[str, Any], look: LookBackFn | None, *, update_card: bool,
    ) -> dict[str, Any]:
        """Settle a cut-off answer by what the checkpoint holds; keep everything when that cannot be told."""
        verdict = await self._look(spec, look, answered)
        return await self._settle_by(verdict, spec, token, answered, update_card=update_card)

    async def _settle_by(
        self, verdict: bool | None, spec: GateSpec, token: str, answered: dict[str, Any], *, update_card: bool,
    ) -> dict[str, Any]:
        """Taken: finish it (a 打回's sheet written).  Not taken: give the gate back.  Cannot tell: keep everything."""
        if verdict is None:
            return {"error": "reply_outcome_unknown"}
        if verdict is True:
            outcome = await self._finish(spec, token, answered, update_card=update_card, settling=True)
        else:
            if self.business.release_gate_decision(spec.checkpoint_id, token):
                what = decision_word(str(answered.get("decision", "") or "")) or "决定"
                self.business.record_gate_event(spec.checkpoint_id, OUTCOME_SETTLED_EVENT, actor="", note={
                    "claim": token, "taken": False, "note": f"已核对：系统没有收下这次{what}，可以重新决定",
                })
            outcome = {"error": "checkpoint_reply_not_accepted"}
        self._cut_off_specs.pop(token, None)
        return outcome

    async def settle_cut_off_answers(
        self, *, look_back: LookBackFn | None = None, spec_for: SpecForFn | None = None,
    ) -> list[dict[str, Any]]:
        """The duty watch's pass: settle every answer still cut off, whether or not its gate still waits.

        A click settles an earlier cut-off answer only on a gate that still
        waits.  Once the runtime has taken an answer the gate is no longer
        offered -- the console answers ``gate_not_found`` and a restarted
        group listener ``unknown_gate`` -- so an answer cut off while nobody
        could tell (a shutdown, a store that could not be read, a group click
        with no way to look) would stay unsettled for good, and a taken
        打回's sheet would never be written.  This pass finds each claim that
        still has an unsettled record and does what a click would:

        * a read-only look (``look_back``); nothing is ever sent;
        * taken: the sheet is written (beside a newer one, never over it) and
          the decision made final, with the spec this process kept or one
          ``spec_for`` rebuilds from the checkpoint; a 打回 whose workspace
          cannot be found now waits for a later pass;
        * not taken: the gate is given back and can be decided again;
        * cannot tell: everything stays for the next pass.

        A claim with no record (in flight right now, or left by a crash
        before anything could be recorded) is not touched.
        """
        look = look_back or self.look_back
        claims = getattr(self.business, "gate_claims", None)
        if look is None or not callable(claims):
            return []
        results: list[dict[str, Any]] = []
        for row in list(claims() or []):
            checkpoint_id = str(row.get("checkpoint_id", "") or "")
            token = str(row.get("decided_at", "") or "")
            if order_close_target(checkpoint_id) is not None or self.extension_card_kind(checkpoint_id) is not None:
                continue  # an order's close card or an extension's card: no checkpoint to look at (close_order; its hooks)
            try:
                earlier = self._cut_off_answer(checkpoint_id, token) if checkpoint_id else None
                if earlier is None:
                    continue
                spec = self._cut_off_specs.get(token)
                verdict = await self._look(spec or self._spec_from_record(row, earlier), look, earlier)
                if verdict is None:
                    continue
                if verdict is True and spec is None:
                    spec = await self._rebuilt_spec(checkpoint_id, earlier, spec_for)
                    if spec is None and earlier.get("decision") == "reject":
                        logger.warning("gate: checkpoint {} took a cut-off 打回 but its workspace cannot be found yet; the sheet waits", checkpoint_id[:8])
                        continue
                outcome = await self._settle_by(
                    verdict, spec or self._spec_from_record(row, earlier), token, earlier, update_card=spec is not None,
                )
            except Exception:
                logger.opt(exception=True).warning("gate: could not settle the cut-off answer on checkpoint {}", checkpoint_id[:8])
                continue
            logger.info("gate: cut-off answer on checkpoint {} settled as {}", checkpoint_id[:8], outcome.get("decided") or outcome.get("error") or outcome.get("ignored"))
            results.append({"checkpoint_id": checkpoint_id, **outcome})
        return results

    def _spec_from_record(self, row: dict[str, Any], earlier: dict[str, Any]) -> GateSpec:
        """Enough of a gate to look at its checkpoint and give it back -- not to write a sheet (no workspace)."""
        return GateSpec(
            checkpoint_id=str(row.get("checkpoint_id", "") or ""),
            checkpoint_type=str(earlier.get("checkpoint_type", "") or ""),
            task_id=str(row.get("task_id", "") or ""), session_id="", waiting_task_id="",
            order_id=str(row.get("order_id", "") or ""), order_title="", node_id="", node_name="", node_type="",
            assignee="", version=int(earlier.get("version") or row.get("version") or 1), standard=[], deliverables=[],
            revision_path=str(earlier.get("revision_path", "") or ""), workspace_root="",
            card_kind=str(earlier.get("card_kind", "") or "review"),
        )

    async def _rebuilt_spec(self, checkpoint_id: str, earlier: dict[str, Any], spec_for: SpecForFn | None) -> GateSpec | None:
        """The gate as it was answered, rebuilt from its checkpoint: the sheet goes where the record says."""
        if spec_for is None:
            return None
        try:
            spec = await spec_for(checkpoint_id, str(earlier.get("checkpoint_type", "") or ""))
        except Exception:
            logger.opt(exception=True).warning("gate: could not rebuild checkpoint {} to settle a cut-off answer", checkpoint_id[:8])
            return None
        if spec is None or str(getattr(spec, "checkpoint_id", "") or "") != checkpoint_id:
            return None
        return replace(
            spec,
            revision_path=str(earlier.get("revision_path", "") or "") or spec.revision_path,
            version=int(earlier.get("version") or spec.version or 1),
        )

    async def _settle_earlier_answer(self, spec: GateSpec, look: LookBackFn | None) -> dict[str, Any] | None:
        """Why this gate cannot take a decision now -- or None when it can.

        An earlier answer cut off on its way to the runtime is settled first
        by a read-only look: had the runtime taken it, it is finished now
        (its sheet written) and this click is too late; had it not, the gate
        is given back and this click goes ahead.  While nobody can tell,
        nothing is sent -- not even once the claim is old.
        """
        row = self.business.gate_card(spec.checkpoint_id) or {}
        if not row.get("decision"):
            return None
        token = str(row.get("decided_at", "") or "")
        if not token.startswith(self._claim_prefix()):
            return {"ignored": "already_decided", "by": row.get("decided_by")}
        earlier = self._cut_off_answer(spec.checkpoint_id, token)
        if earlier is None:
            return None  # in flight, or left by a crash: the claim itself decides (claim_gate_decision)
        outcome = await self._settle_cut_off(spec, token, earlier, look, update_card=True)
        if outcome.get("error") == "checkpoint_reply_not_accepted":
            return None  # given back: this click goes ahead
        if outcome.get("error") == "reply_outcome_unknown":
            return {"ignored": "reply_outcome_unknown", "by": row.get("decided_by")}
        if outcome.get("decided"):
            return {"ignored": "already_decided", "by": row.get("decided_by")}
        return outcome

    async def settle_answered_elsewhere(
        self, spec: GateSpec, kind: str, *, person: dict[str, Any], channel: str = "console",
    ) -> dict[str, Any] | None:
        """Close a permission card whose answer the runtime already holds from somewhere else (the console's chat card).

        Previously, a permission answered on the console's chat card never came through here, so its Feishu
        card kept its buttons and a later click met ``checkpoint_reply_not_accepted``. This is the ``decide`` path
        with nothing sent: claim the decision (the same one-writer rule), then ``_finish`` -- record it, stamp it
        final, update the card with the status line. Only a permission card: a review answered elsewhere carries a
        sheet this service did not see, and the console's review path already goes through ``decide``. None when
        there is no card to close; ``ignored`` with the reason when someone else already claimed or decided it.
        """
        if not spec.is_permission:
            return None
        row = self.business.gate_card(spec.checkpoint_id) or {}
        if not row.get("message_id"):
            return None
        if row.get("decision"):
            return self._why_not_free(spec.checkpoint_id)
        kind = "permit_once" if kind in YES_DECISIONS else "deny"
        actor = str(person.get("name") or person.get("login") or "")
        decided_by = str(person.get("employee_id") or actor)
        token = self.business.claim_gate_decision(
            spec.checkpoint_id, decision=kind, decided_by=decided_by, note="", task_id=spec.task_id,
            order_id=spec.order_id, version=spec.version, stale_after_seconds=DECISION_CLAIM_STALE_SECONDS,
        )
        if not token:
            return self._why_not_free(spec.checkpoint_id) or {"ignored": "decision_in_progress"}
        answered = {"decision": kind, "decision_note": "", "sheet": "", "actor": actor, "by": decided_by, "channel": channel}
        return await self._finish(spec, token, answered)

    def _why_not_free(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Why a claim on this gate was not won: decided for good, being decided right now, or not known.

        None when the gate is free after all: the claim it lost to has been
        given back since (the runtime refused that answer).
        """
        row = self.business.gate_card(checkpoint_id) or {}
        if not row.get("decision"):
            return None
        token = str(row.get("decided_at", "") or "")
        if not token.startswith(self._claim_prefix()):
            code = "already_decided"
        elif self._cut_off_answer(checkpoint_id, token) is not None:
            code = "reply_outcome_unknown"
        else:
            code = "decision_in_progress"
        return {"ignored": code, "by": row.get("decided_by")}

    # ── a card whose checkpoint closed with nobody deciding it on the card (C4-A) ──

    def _retired_marker(self) -> str:
        return str(getattr(self.business, "GATE_RETIRED", "") or "retired")

    def _retired_card(self, checkpoint_id: str) -> dict[str, Any] | None:
        """The card's row when the card was retired; None otherwise."""
        row = self.business.gate_card(checkpoint_id) or {}
        return row if str(row.get("decision", "") or "") == self._retired_marker() else None

    async def _answer_retired(self, action: GateAction, row: dict[str, Any]) -> dict[str, Any]:
        """A reply, a click or an upload on a retired card gets one plain line in its thread; a look or a read, nothing."""
        if action.kind in {"view", "read"}:
            return {"ignored": "retired"}
        target = str(action.message_id or row.get("message_id", "") or "")
        if self.client is not None and target:
            try:
                await self.client.reply_text(target, RETIRED_REPLY)
            except Exception:
                logger.opt(exception=True).warning(
                    "feishu gate: could not answer a {} on the retired card of checkpoint {}", action.kind, action.checkpoint_id[:8],
                )
        return {"ignored": "retired"}

    def outbound_open(self) -> bool:
        """Whether the group can be written to now: channels not muted and the Feishu connector switched on."""
        from ampuly.plugins.office_ui.notifier import require_outbound_enabled

        try:
            require_outbound_enabled("feishu", getattr(self.client, "outbound_allowed", None))
        except Exception:
            return False
        return True

    def order_end(self, checkpoint_id: str) -> str:
        """"cancelled" or "closed" when the order behind this card has ended; "" when it has not, or cannot be told."""
        from ampuly.plugins.office_ui.business_store import ENDED_ORDER_STATUSES

        row = self.business.gate_card(checkpoint_id) or {}
        order_id = str(row.get("order_id", "") or "").strip()
        task_id = str(row.get("task_id", "") or "").strip()
        try:
            order = self.business.get_order(order_id) if order_id else None
            by_task = getattr(self.business, "order_for_launched_task", None)
            if order is None and task_id and callable(by_task):
                order = by_task(task_id)
            status = str(getattr(order, "status", "") or "")
            if order is None or status not in ENDED_ORDER_STATUSES:
                return ""
            events = list(self.business.events(str(getattr(order, "order_id", "") or order_id)) or [])
        except Exception:
            logger.opt(exception=True).debug("feishu gate: the order behind checkpoint {} could not be read", checkpoint_id[:8])
            return ""
        ended = next((event for event in reversed(events) if event.get("kind") == status), None)
        note = ended.get("note") if isinstance(ended, dict) else None
        # A cancel records its outcome on the closing event (order_cancel.cancel_order); a close after delivery does not.
        return "cancelled" if isinstance(note, dict) and "cancel" in note else "closed"

    async def retire(
        self, checkpoint_id: str, reason: str, *, spec: GateSpec | None = None, replaced_by: str = "",
    ) -> dict[str, Any]:
        """Retire the card of a checkpoint that closed with nobody deciding it on the card: no buttons, one line why.

        ``reason`` is a key of ``RETIRE_STATUS_LINES`` (see ``retire_reason``).  The row is marked first, with one
        conditional update (``retire_gate_card``): a click that claimed the card a moment before keeps it, and after
        the mark no click can claim it.  Then the card is updated in the group.  With the channels muted or the
        Feishu connector switched off nothing is marked or sent: the card stays open for the next sweep.  An update
        Feishu refused gives the mark back for the next sweep, ``RETIRE_UPDATE_ATTEMPTS`` times in all; after that
        the card stays retired here (a reply on it is still answered) and the failure is on its record.
        """
        reason = reason if reason in RETIRE_STATUS_LINES else "void"
        line = RETIRE_STATUS_LINES[reason]
        row = self.business.gate_card(checkpoint_id) or {}
        message_id = str(row.get("message_id", "") or "")
        if not message_id:
            return {"ignored": "no_card"}
        if str(row.get("decision", "") or "") == self._retired_marker():
            return {"ignored": "retired"}
        if row.get("decision"):
            return self._why_not_free(checkpoint_id) or {"ignored": "already_decided"}
        if self.client is None:
            return {"ignored": "no_client"}
        if not self.outbound_open():
            return {"deferred": "outbound_muted"}
        stamp = self.business.retire_gate_card(checkpoint_id, reason=reason)
        if not stamp:
            # Claimed or decided in between: that answer keeps the card.
            return self._why_not_free(checkpoint_id) or {"ignored": "already_decided"}
        spec = spec or self._specs.get(checkpoint_id)
        if spec is not None:
            card = card_for(spec, [], status_line=line)
        else:
            order_id = str(row.get("order_id", "") or "").strip()
            card = build_retired_card(f"【{order_id}】" if order_id else "【卡片】", line)
        recorded = {"reason": reason, "note": line, **({"replaced_by": replaced_by} if replaced_by else {})}
        try:
            await self.client.update_card(message_id, card)
        except Exception as exc:
            if not self.outbound_open():
                self.business.unretire_gate_card(checkpoint_id, stamp)
                return {"deferred": "outbound_muted"}
            attempt = 1 + sum(1 for event in self.business.gate_events(checkpoint_id) if event.get("kind") == CARD_RETIRE_FAILED_EVENT)
            keep = attempt >= RETIRE_UPDATE_ATTEMPTS
            self.business.record_gate_event(checkpoint_id, CARD_RETIRE_FAILED_EVENT, actor="", note={
                "reason": reason, "attempt": attempt, "cause": type(exc).__name__,
                "note": "飞书上的卡没能改成作废，" + ("不再重试；在卡上回复会收到「这张卡已作废」" if keep else "下一轮再试"),
            })
            if not keep:
                self.business.unretire_gate_card(checkpoint_id, stamp)
                logger.warning("feishu gate: the card of checkpoint {} could not be retired ({}); tried again next sweep", checkpoint_id[:8], type(exc).__name__)
                return {"error": "card_update_failed", "retry": True}
            logger.warning("feishu gate: the card of checkpoint {} could not be retired after {} tries; kept retired here", checkpoint_id[:8], attempt)
            self._specs.pop(checkpoint_id, None)
            self.business.record_gate_event(checkpoint_id, CARD_RETIRED_EVENT, actor="", note={**recorded, "card_updated": False})
            return {"error": "card_update_failed", "retry": False}
        self._specs.pop(checkpoint_id, None)
        self.business.record_gate_event(checkpoint_id, CARD_RETIRED_EVENT, actor="", note=recorded)
        return {"retired": reason}

    # ── an order's 确认收货并结案 card (FC-1) ──

    def _order_close_spec_from_order(self, checkpoint_id: str) -> GateSpec | None:
        """The close card of this key rebuilt from its order (after a restart); None when the order is gone."""
        target = order_close_target(checkpoint_id)
        if target is None:
            return None
        order_id, spell = target
        try:
            order = self.business.get_order(order_id)
            record = self.business.acceptance(order_id) if order is not None else None
        except Exception:
            logger.opt(exception=True).warning("feishu gate: the order behind a close card could not be read")
            return None
        return order_close_spec(order, record, spell=spell) if order is not None else None

    def order_close_retire_reason(self, checkpoint_id: str) -> tuple[str, str]:
        """(why an order's close card is retired now, the newer close card of the same order); ("", "") while it stands.

        The card stands while its order is in 客户验收中 on the same arrival it was sent for. Else, as a key of
        ``RETIRE_STATUS_LINES``: the order closed or cancelled (``order_end``); back in 客户验收中 after a rework, with
        its new card sent (superseded, 请看新卡); out of 客户验收中, or gone (void). ("", "") too when the order cannot
        be read: the card is looked at again next round.
        """
        target = order_close_target(checkpoint_id)
        if target is None:
            return "", ""
        order_id, spell = target
        try:
            order = self.business.get_order(order_id)
            if order is None:
                return "void", ""
            ended = self.order_end(checkpoint_id)
            if ended in {"cancelled", "closed"}:
                return ("order_cancelled" if ended == "cancelled" else "order_closed"), ""
            if str(getattr(order, "status", "") or "") != "delivering":
                return "void", ""
            now = delivery_spell(self.business.events(order_id))
        except Exception:
            logger.opt(exception=True).debug("feishu gate: the order behind close card {} could not be read", checkpoint_id[:40])
            return "", ""
        if now == spell:
            return "", ""
        newer = order_close_key(order_id, now)
        return ("superseded", newer) if now > spell and self.business.gate_sent_or_decided(newer) else ("void", "")

    def release_left_order_close_claim(self, checkpoint_id: str) -> bool:
        """Give back the claim a stopped console left on an order's close card; True when it was given back.

        Only a claim older than ``DECISION_CLAIM_STALE_SECONDS`` is taken (``claim_gate_decision``'s own rule): a press
        being handled right now keeps its card. It is taken over and given back at once, and the card's record says
        so. Called only for a card whose order has moved on, to retire it; the claim on a card whose order still stands
        is taken over by its next press (``close_order``).
        """
        if order_close_target(checkpoint_id) is None:
            return False
        row = self.business.gate_card(checkpoint_id) or {}
        if not row.get("message_id") or not str(row.get("decided_at", "") or "").startswith(self._claim_prefix()):
            return False
        token = self.business.claim_gate_decision(
            checkpoint_id, decision=ORDER_CLOSE_DECISION, decided_by="", note="",
            task_id=str(row.get("task_id", "") or ""), order_id=str(row.get("order_id", "") or ""),
            version=int(row.get("version") or 1), stale_after_seconds=DECISION_CLAIM_STALE_SECONDS,
        )
        if not token or not self.business.release_gate_decision(checkpoint_id, token):
            return False
        self.business.record_gate_event(checkpoint_id, ORDER_CLOSE_CLAIM_RELEASED_EVENT, actor="", note={
            "note": ORDER_CLOSE_CLAIM_RELEASED, "held_by": str(row.get("decided_by", "") or ""),
        })
        logger.warning("feishu gate: the close card of order {} was left claimed by a stopped press; the claim is given back", row.get("order_id", ""))
        return True

    async def retire_left_order_close_claims(self) -> list[dict[str, Any]]:
        """The duty round's pass over the close cards a stopped console left claimed (FC-1); returns what it did.

        A claimed card is not open, so the retire sweep never looks at it (``open_gate_cards``), and the pass over
        cut-off answers skips close cards: a press cut off by a stop would keep its card's button for good, even after
        the order was closed. Here each close-card claim older than ``DECISION_CLAIM_STALE_SECONDS`` whose order has
        moved on (``order_close_retire_reason``) is given back and the card retired as the sweep retires it -- a close
        that went through before the stop reads 「这张卡已作废：订单已结案」. A claim whose order still stands is left:
        the card still shows its button and the next press takes the claim over. Nothing is given back without a
        Feishu client or with the channels muted or the connector off: the next round does it.
        """
        claims = getattr(self.business, "gate_claims", None)
        if self.client is None or not callable(claims) or not self.outbound_open():
            return []
        now = datetime.now().timestamp()
        results: list[dict[str, Any]] = []
        for row in list(claims() or []):
            checkpoint_id = str(row.get("checkpoint_id", "") or "")
            if order_close_target(checkpoint_id) is None or not row.get("message_id"):
                continue
            claimed_at = self._claimed_at(str(row.get("decided_at", "") or ""))
            if claimed_at is not None and now - claimed_at < DECISION_CLAIM_STALE_SECONDS:
                continue  # a press being handled now, or one stopped only just now: looked at again next round
            try:
                reason, newer = self.order_close_retire_reason(checkpoint_id)
                if not reason or not self.release_left_order_close_claim(checkpoint_id):
                    continue
                outcome = await self.retire(checkpoint_id, reason, spec=await self.load_spec(checkpoint_id), replaced_by=newer)
            except Exception:
                logger.opt(exception=True).warning("feishu gate: a close card left claimed could not be looked at; tried again next round")
                continue
            results.append({"checkpoint_id": checkpoint_id, **outcome})
        return results

    async def _say(self, message_id: str, text: str) -> None:
        """One plain line in a card's thread; a line Feishu refuses is only logged."""
        if self.client is None or not message_id:
            return
        try:
            await self.client.reply_text(message_id, text)
        except Exception:
            logger.opt(exception=True).warning("feishu gate: a line in a card's thread could not be sent")

    async def _on_order_close_event(self, spec: GateSpec, action: GateAction) -> dict[str, Any]:
        """What happens on an order's close card: its button closes the order; a reply or an upload is told so, politely,
        and taken nowhere; a read receipt or a look records nothing."""
        target = str(action.message_id or (self.business.gate_card(spec.checkpoint_id) or {}).get("message_id", "") or "")
        if action.kind in {"read", "view"}:
            return {"ignored": "not_a_review"}
        if action.kind != ORDER_CLOSE_DECISION:
            await self._say(target, ORDER_CLOSE_TYPED_REPLY)
            return {"ignored": "not_a_close"}
        person = self._employee_by_open_id(action.actor_open_id) or {}
        if str(person.get("role", "") or "").strip().lower() != "owner":
            self.business.record_gate_event(spec.checkpoint_id, "unauthorized_attempt", actor=action.actor_open_id, note={"kind": action.kind})
            logger.warning("feishu gate: 确认收货并结案 on order {} by {} refused: not the owner", spec.order_id, action.actor_open_id[:10])
            await self._say(target, ORDER_CLOSE_OWNER_ONLY)
            return {"ignored": "unauthorized"}
        return await self.close_order(spec, person=person, reply_to=target)

    async def close_order(self, spec: GateSpec, *, person: dict[str, Any], reply_to: str = "") -> dict[str, Any]:
        """确认收货并结案 pressed on an order's close card by the owner: the order is closed, once, and the card says so.

        1. An order that has moved on (closed or cancelled elsewhere, out of 客户验收中, or back after a rework) is not
           closed here: the card is retired as the duty round would retire it, and the press is told why. A claim a
           stopped console left on the card is given back first once it is old (``release_left_order_close_claim``).
        2. The card is claimed with the gates' one conditional update (``claim_gate_decision``): a second click while
           the first is being handled is told to wait; one after it is told the order is closed.
        3. The close (``close_order``: the console's all-or-nothing close, by the owner's Feishu identity). Refused, it
           records nothing: the claim is given back, the reason goes into the card's thread in the owner's words, and
           the card keeps its button for the next click. An error does the same -- a close that did go through before
           it is finished by the next click (a click on the closed order is a success) or its card retired by the duty
           round. Done, the claim is made final and the card reads 「已结案 · <who> · HH:MM」.
        """
        target = reply_to or str((self.business.gate_card(spec.checkpoint_id) or {}).get("message_id", "") or "")
        reason, newer = self.order_close_retire_reason(spec.checkpoint_id)
        if reason:
            self.release_left_order_close_claim(spec.checkpoint_id)
            outcome = await self.retire(spec.checkpoint_id, reason, spec=spec, replaced_by=newer)
            await self._say(target, ORDER_CLOSE_MOVED_ON.get(reason, ORDER_CLOSE_MOVED_ON["void"]))
            return {"ignored": "moved_on", "reason": reason, **({"retired": outcome["retired"]} if outcome.get("retired") else {})}
        if self._close_order is None:
            await self._say(target, ORDER_CLOSE_UNAVAILABLE)
            return {"ignored": "no_close"}
        actor = str(person.get("name") or person.get("login") or "")
        decided_by = str(person.get("employee_id") or actor)

        def claim() -> str:
            return self.business.claim_gate_decision(
                spec.checkpoint_id, decision=ORDER_CLOSE_DECISION, decided_by=decided_by, note="",
                task_id=spec.task_id, order_id=spec.order_id, version=spec.version,
                stale_after_seconds=DECISION_CLAIM_STALE_SECONDS,
            )

        token = claim()
        if not token:
            busy = self._why_not_free(spec.checkpoint_id)
            token = "" if busy is not None else claim()
            if not token:
                busy = busy or self._why_not_free(spec.checkpoint_id) or {"ignored": "decision_in_progress"}
                await self._say(target, ORDER_CLOSE_MOVED_ON["order_closed"] if busy.get("ignored") == "already_decided" else ORDER_CLOSE_BUSY)
                return busy
        try:
            result = dict(await self._close_order(spec, dict(person)) or {})
        except BaseException as exc:
            # Nothing is known to be recorded: the card is given back. The close is all or nothing and a click on the
            # closed order answers as a success, so the next click finishes whatever this one did.
            self.business.release_gate_decision(spec.checkpoint_id, token)
            if not isinstance(exc, Exception):
                raise
            logger.opt(exception=exc).warning("feishu gate: 确认收货并结案 on order {} stopped on an error; the card is open again", spec.order_id)
            self.business.record_gate_event(spec.checkpoint_id, "close_failed", actor=decided_by, note={"note": ORDER_CLOSE_FAILED, "cause": type(exc).__name__})
            await self._say(target, ORDER_CLOSE_FAILED)
            return {"error": "close_failed"}
        if not result.get("closed"):
            refused = str(result.get("refused", "") or "").strip() or ORDER_CLOSE_FAILED
            self.business.release_gate_decision(spec.checkpoint_id, token)
            self.business.record_gate_event(spec.checkpoint_id, "close_refused", actor=decided_by, note={"note": refused, "channel": "feishu"})
            await self._say(target, refused)
            return {"refused": refused}
        if not self.business.finish_gate_decision(spec.checkpoint_id, token):
            logger.warning("feishu gate: order {} was closed, but its card's claim was taken over meanwhile", spec.order_id)
        self.business.record_gate_event(spec.checkpoint_id, ORDER_CLOSE_DECISION, actor=decided_by, note={"note": ORDER_CLOSE_LABEL, "channel": "feishu"})
        status = f"已结案 · {actor} · {datetime.now().strftime('%H:%M')}"
        row = self.business.gate_card(spec.checkpoint_id) or {}
        if self.client is not None and row.get("message_id"):
            try:
                await self.client.update_card(str(row["message_id"]), card_for(spec, [], status_line=status))
            except Exception:
                logger.opt(exception=True).warning("feishu gate: the close card of order {} could not be updated", spec.order_id)
        return {"decided": ORDER_CLOSE_DECISION, "by": actor}

    # ── a console extension's own card kind (``extensions.FeishuCardKind``) ──
    #
    # What the gate service offers every extension card kind: which kind a card key is, whether that card still
    # stands (its kind's rule), and the few service calls its press handler makes -- a line in the card's thread, who a
    # Feishu person is, why a card cannot be claimed now. ``retire``, ``order_end``, ``business`` and ``client`` are
    # the service's own, used by the console as well.

    def extension_card_kind(self, checkpoint_id: str) -> Any:
        """The registered extension card kind whose key this is (``extensions.FeishuCardKind``); None for any other."""
        from ampuly.plugins.office_ui.extensions import feishu_card_kind_for

        return feishu_card_kind_for(checkpoint_id)

    def card_retire_reason(self, checkpoint_id: str) -> tuple[str, str]:
        """(why an extension's card is retired now, its newer card); ("", "") while it stands, and for any other card."""
        kind = self.extension_card_kind(checkpoint_id)
        if kind is None:
            return "", ""
        return kind.retire_reason(self, checkpoint_id)

    async def say(self, message_id: str, text: str) -> None:
        """One plain line in a card's thread (``_say``), for an extension card's press handler."""
        await self._say(message_id, text)

    def employee_by_open_id(self, open_id: str) -> dict[str, Any] | None:
        """Who a Feishu person is, by the one directory (role, name, login, employee id); None for nobody."""
        return self._employee_by_open_id(open_id)

    def why_not_free(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Why a claim on this card was not won (``_why_not_free``); None when the card is free after all."""
        return self._why_not_free(checkpoint_id)

    async def store_upload(self, spec: GateSpec, action: GateAction) -> str:
        """Write an uploaded file into the node's delivery folder; returns the relative path."""
        upload = dict(action.upload or {})
        file_key = str(upload.get("file_key", "") or "")
        if self.client is None or not file_key or not spec.workspace_root:
            return ""
        name = str(upload.get("file_name", "") or "").strip() or f"{file_key[:12]}.bin"
        name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip() or "upload.bin"
        target_dir = ""
        for declared in spec.deliverables:
            if declared.endswith("/"):
                target_dir = declared
                break
            parent = str(PurePosixPath(declared).parent)
            if parent not in {"", "."}:
                target_dir = parent + "/"
                break
        relative_target = str(PurePosixPath(target_dir) / name)
        path = _workspace_path(spec.workspace_root, relative_target)
        if path is None:
            logger.warning("feishu gate: refused an upload target outside the workspace")
            return ""
        try:
            blob = await self.client.download_message_file(str(upload.get("message_id", "") or action.message_id), file_key, kind=str(upload.get("type", "file")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        except Exception:
            logger.opt(exception=True).warning("feishu gate: could not store the upload for {}", spec.checkpoint_id[:8])
            return ""
        return str(path.relative_to(Path(spec.workspace_root))).replace(os.sep, "/")

    def _write_revision(self, spec: GateSpec, text: str, *, not_newer_than: float | None = None) -> str:
        """Write a 打回's sheet at the node's revision path (the one there is archived); returns the path written.

        ``not_newer_than`` (a time) is for a 打回 settled late: a sheet on
        disk written after it belongs to a later 打回, which the rework
        follows, so this one is kept beside it instead of over it.
        """
        if not spec.workspace_root or not spec.revision_path:
            return ""
        path = _workspace_path(spec.workspace_root, spec.revision_path)
        if path is None:
            logger.warning("feishu gate: refused a revision path outside the workspace")
            return ""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not_newer_than is not None and path.is_file() and path.stat().st_mtime > not_newer_than:
                stamp = datetime.fromtimestamp(not_newer_than).strftime("%Y%m%d-%H%M%S")
                beside = path.with_name(f"{path.stem}-{stamp}-{spec.checkpoint_id[:8]}{path.suffix}")
                beside.write_text(text, encoding="utf-8")
                return str(PurePosixPath(spec.revision_path).with_name(beside.name))
            if path.is_file():
                # A second rejection must not erase the first: the upstream
                # role reworks against the latest sheet, the audit keeps all.
                stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
                archived = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
                if archived.exists():
                    archived = path.with_name(f"{path.stem}-{stamp}-{spec.checkpoint_id[:8]}{path.suffix}")
                path.replace(archived)
            path.write_text(text, encoding="utf-8")
        except OSError:
            return ""
        return spec.revision_path
