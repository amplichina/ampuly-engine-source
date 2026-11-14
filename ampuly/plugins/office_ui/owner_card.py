"""One wording for everything that waits on a person: the owner's card (S1-A).

Until now each surface worded a pending pause for itself: the approval centre said "tool authorization" for a parked
run, the order page counted every card as 等你处理, the Feishu card had its own words and the frontend kept a word table
of its own. The owner's decision (design, 「把系统词换成人话」 and 「第二轮定稿」; architecture-review
candidate 4): one OwnerCard, projected on the server, and the frontend word table is only its test sample.

``project`` turns one ``blockers_list`` item into that card: what it is called (``title``), one line under it
(``line``), the one action the owner takes from the list (``primary``), the rest (``more``), what the primary action
leads to (``consequence``), the order it belongs to, the row's status word (the S0-A constants of ``order_attention``:
``kind_word``) and whether the decision is taken on the card or at the end of the materials (``decides_here``).
The words are the design's:

- 审核 / 关口: 等你审核 · <step>; 放行 / 打回修改 live at the end of the materials, never on the card. A step assigned
  to an employee is 轮到 <name> · <step>, with 我来审 as the owner's way to take it.
- 工具 / 外部动作: the server's permission sentence (``feishu_gate.describe_permission``, which knows the order
  folder), then 允许这一次 / 不允许 on the checkpoint's own option ids.
- 钱不够了: the allowance stopped the turn; 继续这一项 / 判这一项失败 (a report or review card has no 判失败).
- 没收到回执: a paid call whose receipt never came -- a turn parked with its outcome unknown, or a step parked at a
  console restart: 再试一次 / 跳过这一步.
- 已停: a parked run; 续跑.
- 等你验收: the final delivery; 接受 / 打回重做 live at the end of the deliverables.
- 留点经验: never a primary action on any list; 学一轮（会花钱） / 不用了.
- Staffing, recruitment, reorg, a question, a peer wait: a title in the owner's words and 打开这张单.

An action id is the checkpoint's own option id wherever the card answers the checkpoint (allow / deny, continue /
fail, redo / skip); where the page acts itself (open the materials, take a step, open the order, resume) it is one
of the verbs named below. No checkpoint id, task id, session id, role id, "Report #" or runtime turn title reaches
a text field: the step is named through ``blockers.owner_turn_name`` and the ids stay in the item's own fields.

Pure: reads the item, the checkpoint's payload and the waiting task handed in, does no I/O. Plugin-layer module:
no engine-kernel changes.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from loguru import logger

from ampuly.plugins.office_ui.blockers import blocker_kind, owner_turn_name
from ampuly.plugins.office_ui.order_attention import (
    KIND_ACCEPTANCE,
    KIND_ALLOWANCE,
    KIND_ANSWER,
    KIND_EMPLOYEE,
    KIND_NO_RECEIPT,
    KIND_REVIEW,
    KIND_STOPPED,
    KIND_TOOL,
    REASON_FAILED,
    WAITS_OWNER,
)

# The actions a card offers that are not one of the checkpoint's options: the page acts on them itself.
OPEN_MATERIALS = "open_materials"      # 看材料并处理 -- the materials, where 放行 / 打回修改 are
OWNER_REVIEW = "owner_review"          # 我来审 -- the owner takes a step assigned to an employee
OPEN_ACCEPTANCE = "open_acceptance"    # 看交付物并处理 -- the deliverables, where 接受 / 打回重做 are
OPEN_ORDER = "open"                    # 打开这张单
RESUME = "resume"                      # 续跑
# The lesson card's two answers: the checkpoint's own accepted answers.  The console answers a card through
# interaction_reply with the id as ``option_id``, and layer0's ``validate_decision`` takes only ``approve`` /
# ``feedback`` / ``ignore`` for ``company_delivery_feedback``; the engine then routes ``ignore`` to "not this time"
# and ``approve`` to the paid learning pass.  The Feishu card's ``learn`` / ``no_lesson`` are its own decision kinds
# (feishu_gate maps them onto these before answering); on the console they would be refused as invalid_delivery_action.
LESSON_LEARN = "approve"
LESSON_SKIP = "ignore"

# The budget stop that is not the allowance saying no but a paid call whose answer never came (LK-1 / OUTCOME_UNKNOWN).
_NO_RECEIPT_STOP_KINDS = frozenset({"outcome_unknown"})
# The final delivery's two answers (company_mode._save_final_delivery_review_checkpoint).
_ACCEPTANCE_OPTION_IDS = frozenset({"accept", "rework"})

# The words, in the owner's language.  ``{kind}`` slots take the S0-A constants so the row and the card say the same.
_WORDS: dict[str, dict[str, str]] = {
    "zh": {
        "employee_turn": "轮到 {name}",
        "someone": "负责人",
        "review_line": "材料已固定为第 {n} 版。看完材料再决定。",
        "review_line_no_version": "材料已固定。看完材料再决定。",
        "employee_line": "材料已固定为第 {n} 版，等{name}处理。",
        "employee_line_no_version": "材料已固定，等{name}处理。",
        "open_materials": "看材料并处理",
        "owner_review": "我来审",
        "review_consequence": "放行后，后面的步骤马上开始做，不再有 AI 复审",
        "permission_line": "要用「{tool}」，允许吗",
        "some_tool": "一个工具",
        "allow_once": "允许这一次",
        "deny": "不允许",
        "permission_consequence": "只允许这一次，下次还会再问",
        "continue": "继续这一项",
        "fail": "判这一项失败",
        "allowance_consequence": "继续会重跑这一回合，回合里已经做过的动作可能重复",
        "allowance_no_fail": "这一项没有「判失败」，要停就取消这张单",
        "retry": "再试一次",
        "skip": "跳过这一步",
        "no_receipt_line": "上次停下前这一步发出的调用没有回执，不知道做到哪了。",
        "no_receipt_consequence": "再试会重发这一次调用，可能重复计费",
        "resume": "续跑",
        "stopped_line": "这张单的运行已经停下，没有在做。",
        "stopped_consequence": "续跑从停下的那一步接着做",
        "open_acceptance": "看交付物并处理",
        "acceptance_line": "最终交付第 {n} 版已交上来。看完交付物再决定。",
        "acceptance_line_no_version": "最终交付已交上来。看完交付物再决定。",
        "acceptance_consequence": "接受后进入客户验收，确认收货才结案",
        "lesson_title": "给这次交付留一句经验",
        "lesson_title_step": "给这一步的交付留一句经验",
        "lesson_line": "想让 AI 员工从这次交付里学一轮吗？",
        "lesson_line_step": "想让 AI 员工从这一步的交付里学一轮吗？",
        "lesson_consequence": "学一轮最长约 40 分钟，会产生模型费用；不用了不花钱",
        "open_order": "打开这张单",
        "staffing_title": "等你确认人员安排",
        "staffing_line": "这张单的人员安排要你确认。",
        "recruitment_title": "等你确认招人",
        "recruitment_line": "要招人，等你确认。",
        "reorg_title": "等你确认组织调整",
        "reorg_line": "组织调整方案要你确认。",
        "failure_line": "这次运行出了错，看一眼出了什么问题。",
        "peer_wait_title": "在等另一步做完",
        "peer_wait_line": "这一步在等另一步的结果，不用你处理。",
        "question_line": "这一步有话要问你。",
    },
    "en": {
        "employee_turn": "{name}'s turn",
        "someone": "the assignee",
        "review_line": "The materials are fixed as version {n}. Read them, then decide.",
        "review_line_no_version": "The materials are fixed. Read them, then decide.",
        "employee_line": "The materials are fixed as version {n}; waiting on {name}.",
        "employee_line_no_version": "The materials are fixed; waiting on {name}.",
        "open_materials": "Read the materials and decide",
        "owner_review": "I'll review it",
        "review_consequence": "Once passed, the next steps start at once; no AI re-review",
        "permission_line": "It wants to “{tool}”. Allow?",
        "some_tool": "a tool",
        "allow_once": "Allow this once",
        "deny": "Don't allow",
        "permission_consequence": "Allowed once only; it will ask again next time",
        "continue": "Continue this item",
        "fail": "Fail this item",
        "allowance_consequence": "Continuing runs this turn again; what the turn already did may be repeated",
        "allowance_no_fail": "This item cannot be failed here; to stop, cancel the order",
        "retry": "Try again",
        "skip": "Skip this step",
        "no_receipt_line": "The call this step sent before the stop has no receipt; nobody knows how far it got.",
        "no_receipt_consequence": "Trying again resends the call and may be billed twice",
        "resume": "Resume",
        "stopped_line": "This order's run is stopped; nothing is being done.",
        "stopped_consequence": "Resuming continues from the step it stopped at",
        "open_acceptance": "Open the deliverables and decide",
        "acceptance_line": "Final delivery version {n} is in. Read it, then decide.",
        "acceptance_line_no_version": "The final delivery is in. Read it, then decide.",
        "acceptance_consequence": "Accepting hands it to the customer; the order closes once receipt is confirmed",
        "lesson_title": "Leave a lesson from this delivery",
        "lesson_title_step": "Leave a lesson from this step's delivery",
        "lesson_line": "Should the AI employee spend a round learning from this delivery?",
        "lesson_line_step": "Should the AI employee spend a round learning from this step's delivery?",
        "lesson_consequence": "A round takes up to about 40 minutes and costs model fees; skipping costs nothing",
        "open_order": "Open this order",
        "staffing_title": "Confirm the staffing",
        "staffing_line": "This order's staffing needs your confirmation.",
        "recruitment_title": "Confirm the hiring",
        "recruitment_line": "Hiring is proposed; it needs your confirmation.",
        "reorg_title": "Confirm the reorganisation",
        "reorg_line": "A reorganisation proposal needs your confirmation.",
        "failure_line": "This run failed; have a look at what went wrong.",
        "peer_wait_title": "Waiting for another step",
        "peer_wait_line": "This step waits for another step's result; nothing for you to do.",
        "question_line": "This step has a question for you.",
    },
}

# The row words for the kinds the S0-A constants do not name in English.
_KIND_WORDS_EN = {
    KIND_REVIEW: "Your review", KIND_TOOL: "Wants a tool", KIND_ALLOWANCE: "Out of allowance",
    KIND_NO_RECEIPT: "No receipt", KIND_STOPPED: "Stopped", KIND_ACCEPTANCE: "Your acceptance",
    KIND_ANSWER: "Your answer", KIND_EMPLOYEE: "Someone's turn", REASON_FAILED: "Failed",
}

# What an option the design does not name is called, by its id; a label already in the owner's words stays.
_OPTION_WORDS_ZH = {
    "approve": "同意", "approve_once": "允许这一次", "permit_once": "允许这一次", "deny": "不同意", "reject": "打回",
    "accept": "接受", "rework": "打回重做", "continue": "继续这一项", "fail_item": "判这一项失败",
    "redo_step": "再试一次", "skip_step": "跳过这一步", "confirm": "确认", "cancel": "取消", "ignore": "忽略",
}

# An id the owner must never read: a uuid, a fingerprint, a hex tail (the same rule as the console's own wording).
_INTERNAL_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"|(?<![0-9a-zA-Z])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{12,}(?![0-9a-zA-Z])"
)
# A role or employee label that is an identifier (vi_lead, emp-1), not a name a person would recognise.
_IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:[_.:-][A-Za-z0-9]+)*")
# A runtime turn title ("Report #1: …", "Review #2: …"): internal, never shown to the owner as it is.
_RUNTIME_TURN_TITLE = re.compile(r"\b(?:Report|Review)\s*#\s*\d+", re.IGNORECASE)
# The turn a headline quotes: 「X」, 「X」的汇报, 「X」的评审 -- the owner's name for it, already worded by the runtime.
_QUOTED_TURN = re.compile(r"「[^「」]{1,80}」(?:的汇报|的评审)?")
_REFUSAL_MARK = "拒绝原文："


def _words(zh: bool) -> dict[str, str]:
    return _WORDS["zh" if zh else "en"]


def _kind_word(kind: str, zh: bool) -> str:
    return kind if zh else _KIND_WORDS_EN.get(kind, kind)


def _plain(text: Any) -> str:
    """``text`` when it is a sentence the owner can read as it is (mostly Chinese, no internal id); '' otherwise."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    chinese = sum(1 for ch in clean if "一" <= ch <= "鿿")
    if not chinese:
        return ""
    latin = sum(1 for ch in clean if ch.isascii() and ch.isalpha())
    # A runtime turn title ("Report #1: X", "Review #2: X") is never the owner's sentence, however much Chinese it
    # carries after the colon (review of S1-A: it slipped through when the waiting task could not be read).
    if latin > 2 * chinese or _INTERNAL_ID.search(clean) or _RUNTIME_TURN_TITLE.search(clean):
        return ""
    return clean


def _first_sentence(text: Any, *, limit: int = 120) -> str:
    """The first sentence of a paragraph, for a line under the title."""
    paragraph = str(text or "").strip().split("\n", 1)[0].strip()
    if not paragraph:
        return ""
    head = re.split(r"(?<=[。！？!?])", paragraph, maxsplit=1)[0].strip()
    head = head or paragraph
    return head if len(head) <= limit else head[: limit - 1] + "…"


def _without_outer_quotes(text: str) -> str:
    for left, right in (("「", "」"), ("“", "”")):
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1]
    return text


def _joined(word: str, name: str) -> str:
    return f"{word} · {name}" if name else word


def _option(option_id: str, label: str) -> dict[str, str]:
    return {"id": option_id, "label": label}


# ── what the item and the checkpoint say ──────────────────────────────────


def _options(item: Mapping[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for option in list(item.get("options", []) or []):
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id", "") or "").strip()
        if option_id:
            found.append({"id": option_id, "label": str(option.get("label", "") or "").strip()})
    return found


def _option_label(option: Mapping[str, str], zh: bool) -> str:
    """An option the design does not name, in the owner's words: by id first, else its own label."""
    option_id = str(option.get("id", "") or "")
    label = str(option.get("label", "") or "")
    if zh:
        return _OPTION_WORDS_ZH.get(option_id.lower()) or _plain(label) or label or option_id
    return label or option_id


def _facts(item: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """The facts the card reads beyond the item: the pause request's, as ``order_attention.pause_from_checkpoint``
    reads them, from the item's ``attention_facts`` when it carries them, else from the payload handed in."""
    carried = item.get("attention_facts") if isinstance(item.get("attention_facts"), dict) else {}
    request = dict(payload.get("pause_request", {}) or {})
    note = str(request.get("context_note", "") or "")
    _head, marker, refusal = note.partition(_REFUSAL_MARK)
    final_review = payload.get("final_delivery_review")
    return {
        "assignee": str(carried.get("assignee") or request.get("assignee_employee_id", "") or "").strip(),
        "human_node": bool(carried.get("human_node") or request.get("human_node")),
        "budget_stop": str(request.get("budget_stop", "") or "").strip() or ("stop" if carried.get("budget_stop") else ""),
        "no_receipt": bool(carried.get("no_receipt")) or str(request.get("request_id", "") or "").startswith("restart-unknown:"),
        "final_delivery_review": bool(carried.get("final_delivery_review")) or isinstance(final_review, dict),
        "delivery_revision": dict(final_review or {}).get("delivery_revision", "") if isinstance(final_review, dict) else "",
        "feedback_scope": str(payload.get("feedback_scope", "") or "").strip(),
        "refusal": refusal.strip() if marker else "",
        "reason": str(request.get("reason", "") or ""),
        "tool_call": dict(payload.get("tool_call", {}) or {}),
        "payload": dict(payload),
    }


def _order_words(order: Any, item: Mapping[str, Any]) -> tuple[str, str]:
    """``(order_id, order_label)``: the label is the customer or brand the owner knows the order by, else its number."""
    if order is None:
        return "", ""
    if isinstance(order, Mapping):
        get = order.get
    else:
        def get(key: str, default: Any = "") -> Any:
            return getattr(order, key, default)
    order_id = str(get("order_id", "") or "").strip()
    label = str(get("customer_name", "") or get("brand_name", "") or get("title", "") or "").strip()
    return order_id, (label or order_id)


def _turn_name(item: Mapping[str, Any], waiting_task: Any, *, limit: int = 40) -> str:
    """The turn as the owner knows it: 首轮 6 稿, 「首轮 6 稿」的汇报, 最终交付 -- never "Report #1: ..."."""
    if waiting_task is not None:
        name = owner_turn_name(getattr(waiting_task, "title", ""), getattr(waiting_task, "metadata", None), in_brackets=True, limit=limit)
        if name:
            return name
    subject = " ".join(str(item.get("subject", "") or "").split())
    found = _QUOTED_TURN.search(subject)
    if found:
        return _without_outer_quotes(found.group(0))
    if subject.startswith("最终交付"):
        return "最终交付"
    return ""


def _employee_of(item: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    """The employee a step is assigned to, as ``order_attention.classify_pause`` reads it: the pause request's person,
    or the signer a ``task_user_input`` item was resolved to.  '' for the owner ('' / 'owner') -- and for a work-item
    gate, whose ``assignee`` names a reviewer role, not a person."""
    assignee = str(facts.get("assignee", "") or "").strip()
    gate = item.get("gate") if isinstance(item.get("gate"), dict) else {}
    if not assignee and str(item.get("checkpoint_type", "") or "") == "task_user_input":
        assignee = str(gate.get("assignee", "") or "").strip()
    if not assignee or assignee.lower() == WAITS_OWNER:
        return ""
    return assignee


def assignee_of(blocker_item: Mapping[str, Any], *, checkpoint: Any = None) -> str:
    """The employee a card would name (「轮到 <name>」), or '' when the card is the owner's or nobody's.

    For the caller that resolves names: only a review card names a person; every other card is the owner's.
    """
    item = dict(blocker_item or {})
    payload = dict(getattr(checkpoint, "payload", {}) or {}) if checkpoint is not None else {}
    facts = _facts(item, payload)
    checkpoint_type = str(item.get("checkpoint_type", "") or "")
    kind = str(item.get("kind", "") or "") or blocker_kind(checkpoint_type)
    if kind in {"delivery", "wait", "suspended", "failure", "permission"} or isinstance(item.get("permission"), dict):
        return ""
    if facts["budget_stop"] or facts["no_receipt"] or facts["final_delivery_review"]:
        return ""
    return _employee_of(item, facts)


# ── the cards ─────────────────────────────────────────────────────────────


def _review_card(item, facts, gate, words, zh, *, turn, employee_names) -> dict[str, Any]:
    node = str(gate.get("node_name", "") or "").strip() or turn or _plain(item.get("subject")) or ""
    try:
        version = int(gate.get("version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    employee_id = _employee_of(item, facts)
    if employee_id:
        name = str((employee_names or {}).get(employee_id, "") or "").strip()
        if not name:
            name = employee_id if not _IDENTIFIER.fullmatch(employee_id) else words["someone"]
        line = words["employee_line"].format(n=version, name=name) if version > 0 else words["employee_line_no_version"].format(name=name)
        return {
            "title": _joined(words["employee_turn"].format(name=name), node),
            "line": line,
            "primary": None,
            "more": [_option(OWNER_REVIEW, words["owner_review"])],
            "consequence": "",
            "kind_word": _kind_word(KIND_EMPLOYEE, zh),
            "decides_here": False,
        }
    return {
        "title": _joined(_kind_word(KIND_REVIEW, zh), node),
        "line": words["review_line"].format(n=version) if version > 0 else words["review_line_no_version"],
        "primary": _option(OPEN_MATERIALS, words["open_materials"]),
        "more": [],
        "consequence": words["review_consequence"],
        "kind_word": _kind_word(KIND_REVIEW, zh),
        "decides_here": False,
    }


def _tool_word(tool_name: str) -> str:
    """What the tool does, in words: 抓取网页, 查看文件, 运行命令 -- the bare tool name only for one nobody has named."""
    from ampuly.layer2_organization.restart_unknown_wait import tool_action_label
    from ampuly.plugins.office_ui.feishu_gate import describe_permission

    name = str(tool_name or "").strip()
    if not name:
        return ""
    labelled = tool_action_label(name)
    if labelled and labelled != name:
        return labelled
    sentence, _outside = describe_permission(name, {}, "")
    if sentence == "运行一条命令":
        return "运行命令"
    if sentence.startswith("使用工具 "):
        return sentence[len("使用工具 "):].strip() or name
    return sentence or name


def _permission_card(item, facts, permission, words, zh, options) -> dict[str, Any]:
    from ampuly.plugins.office_ui.feishu_gate import NO_APPROVE_OPTION_NOTE, describe_permission

    tool_call = facts["tool_call"]
    tool = str(tool_call.get("name") or item.get("subject") or "").strip()
    summary = str(permission.get("summary", "") or "").strip()
    if not summary:
        arguments = tool_call.get("arguments")
        if isinstance(arguments, dict):
            summary, _outside = describe_permission(tool, arguments, "")
        else:
            detail = str(item.get("detail", "") or "").strip()
            guessed = {"url": detail} if detail.lower().startswith(("http://", "https://")) else ({"path": detail, "command": detail} if detail else {})
            summary, _outside = describe_permission(tool, guessed, "")
    option_ids = [option["id"] for option in options]
    approve = next((oid for oid in option_ids if oid == "approve_once"), "") or next(
        (oid for oid in option_ids if "approve" in oid.lower() and "deny" not in oid.lower()), "")
    deny = next((oid for oid in option_ids if oid == "deny"), "") or next((oid for oid in option_ids if "deny" in oid.lower()), "")
    more = [_option(deny, words["deny"])] if deny else []
    more.extend(_option(option["id"], _option_label(option, zh)) for option in options if option["id"] not in {approve, deny})
    return {
        "title": summary,
        "line": words["permission_line"].format(tool=_tool_word(tool) or tool or words["some_tool"]),
        "primary": _option(approve, words["allow_once"]) if approve else None,
        "more": more,
        "consequence": words["permission_consequence"] if approve else (NO_APPROVE_OPTION_NOTE if zh else "This request offers no “allow”; only refusing is possible here."),
        "kind_word": _kind_word(KIND_TOOL, zh),
        "decides_here": True,
    }


def _allowance_line(item, facts, zh) -> str:
    """The stop reason in the owner's words (P-c ``budget_error_zh``), else what the headline says was stopped."""
    if facts["refusal"] and zh:
        from ampuly.plugins.office_ui.snapshot_builder import BUDGET_UNKNOWN_ZH, budget_error_zh

        said = budget_error_zh(facts["refusal"])
        if said and said != BUDGET_UNKNOWN_ZH:
            return said
    subject = " ".join(str(item.get("subject", "") or "").split())
    _head, colon, what = subject.partition("：")
    if colon and what.strip():
        return what.strip()
    return _plain(subject) or _first_sentence(facts["reason"]) or subject


def _allowance_card(item, facts, words, zh, options, *, turn) -> dict[str, Any]:
    from ampuly.layer2_organization.budget_stop_wait import CONTINUE, FAIL

    option_ids = [option["id"] for option in options]
    no_receipt = facts["budget_stop"] in _NO_RECEIPT_STOP_KINDS
    word = KIND_NO_RECEIPT if no_receipt else KIND_ALLOWANCE
    primary = _option(CONTINUE, words["retry"] if no_receipt else words["continue"]) if CONTINUE in option_ids else None
    more = [_option(FAIL, words["fail"])] if FAIL in option_ids else []
    more.extend(_option(option["id"], _option_label(option, zh)) for option in options if option["id"] not in {CONTINUE, FAIL})
    if no_receipt:
        consequence = words["no_receipt_consequence"]
    else:
        consequence = words["allowance_consequence"]
        if FAIL not in option_ids:
            consequence = f"{consequence}；{words['allowance_no_fail']}" if zh else f"{consequence}; {words['allowance_no_fail']}"
    return {
        "title": _joined(_kind_word(word, zh), turn),
        "line": _allowance_line(item, facts, zh),
        "primary": primary,
        "more": more,
        "consequence": consequence,
        "kind_word": _kind_word(word, zh),
        "decides_here": True,
    }


def _restart_card(item, facts, words, zh, options, *, turn) -> dict[str, Any]:
    from ampuly.layer2_organization.restart_unknown_wait import REDO, SKIP

    option_ids = [option["id"] for option in options]
    more = [_option(SKIP, words["skip"])] if SKIP in option_ids else []
    more.extend(_option(option["id"], _option_label(option, zh)) for option in options if option["id"] not in {REDO, SKIP})
    line = (_first_sentence(facts["reason"]) if zh else "") or _plain(item.get("subject")) or words["no_receipt_line"]
    return {
        "title": _joined(_kind_word(KIND_NO_RECEIPT, zh), turn),
        "line": line,
        "primary": _option(REDO, words["retry"]) if REDO in option_ids else None,
        "more": more,
        "consequence": words["no_receipt_consequence"],
        "kind_word": _kind_word(KIND_NO_RECEIPT, zh),
        "decides_here": True,
    }


def _stopped_card(item, words, zh, options, *, order_label, turn) -> dict[str, Any]:
    return {
        "title": _joined(_kind_word(KIND_STOPPED, zh), order_label or turn),
        "line": _plain(item.get("subject")) or words["stopped_line"],
        "primary": _option(RESUME, words["resume"]),
        "more": [_option(option["id"], _option_label(option, zh)) for option in options],
        "consequence": words["stopped_consequence"],
        "kind_word": _kind_word(KIND_STOPPED, zh),
        "decides_here": True,
    }


def _acceptance_card(facts, words, zh, *, order_label, turn) -> dict[str, Any]:
    try:
        revision = int(facts["delivery_revision"] or 0)
    except (TypeError, ValueError):
        revision = 0
    return {
        "title": _joined(_kind_word(KIND_ACCEPTANCE, zh), order_label or turn),
        "line": words["acceptance_line"].format(n=revision) if revision > 0 else words["acceptance_line_no_version"],
        "primary": _option(OPEN_ACCEPTANCE, words["open_acceptance"]),
        "more": [],
        "consequence": words["acceptance_consequence"],
        "kind_word": _kind_word(KIND_ACCEPTANCE, zh),
        "decides_here": False,
    }


def _lesson_card(item, facts, words, zh) -> dict[str, Any]:
    from ampuly.plugins.office_ui.feishu_gate import LESSON_LEARN_LABEL, LESSON_SKIP_LABEL

    scope = facts["feedback_scope"]
    step = (scope and scope != "final") or (not scope and "这一步的交付" in str(item.get("subject", "") or ""))
    return {
        "title": words["lesson_title_step"] if step else words["lesson_title"],
        "line": words["lesson_line_step"] if step else words["lesson_line"],
        "primary": None,
        "more": [
            _option(LESSON_LEARN, LESSON_LEARN_LABEL if zh else "Learn a round (costs money)"),
            _option(LESSON_SKIP, LESSON_SKIP_LABEL if zh else "No thanks"),
        ],
        "consequence": words["lesson_consequence"],
        "kind_word": "",
        "decides_here": True,
    }


def _plain_card(item, words, zh, options, *, title, line, kind_word) -> dict[str, Any]:
    """Staffing, recruitment, reorg, a run failure, a question, a peer wait: the title, and the order to open."""
    return {
        "title": title,
        "line": _plain(item.get("subject")) or line,
        "primary": _option(OPEN_ORDER, words["open_order"]),
        "more": [_option(option["id"], _option_label(option, zh)) for option in options],
        "consequence": "",
        "kind_word": kind_word,
        "decides_here": False,
    }


def _bare_card(item: Mapping[str, Any], zh: bool) -> dict[str, Any]:
    """The card when the projection itself fails: the kind's word and nothing decided, never an unlisted pause."""
    kind_word = _kind_word(KIND_ANSWER, zh)
    return {
        "title": _plain(item.get("subject")) or kind_word,
        "line": "",
        "primary": None,
        "more": [],
        "consequence": "",
        "order_id": "",
        "order_label": "",
        "kind_word": kind_word,
        "decides_here": False,
    }


# ── the projection ────────────────────────────────────────────────────────


def project(
    blocker_item: Mapping[str, Any],
    *,
    order: Any = None,
    zh: bool = True,
    checkpoint: Any = None,
    waiting_task: Any = None,
    employee_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One ``blockers_list`` item as the owner's card.

    ``order`` is the order the item belongs to (a ``WorkOrder`` or its payload), found by the caller the way the pages
    match it (the run's task, or the order's own id); ``checkpoint`` and ``waiting_task`` are what the item was built
    from -- the pause request's facts (a budget stop and its refusal, a restart marker, the final delivery review,
    the person a human step is assigned to) and the turn's runtime title are read there, never from the item's text.
    Without them the card is read from the item alone: its kind, ``gate`` / ``permission`` (as the list resolves
    them) and the option ids the checkpoint carries.  ``employee_names`` maps an employee id to the name the owner
    knows; an id with no name is said as 负责人, never as the id.
    """
    item = dict(blocker_item or {})
    try:
        return _project(item, order=order, zh=zh, checkpoint=checkpoint, waiting_task=waiting_task, employee_names=employee_names)
    except Exception:
        logger.opt(exception=True).warning("owner card: a pause could not be projected; its card says only what it is")
        return _bare_card(item, zh)


def _project(item, *, order, zh, checkpoint, waiting_task, employee_names) -> dict[str, Any]:
    from ampuly.layer2_organization.budget_stop_wait import CONTINUE, FAIL
    from ampuly.layer2_organization.restart_unknown_wait import REDO, SKIP

    words = _words(zh)
    checkpoint_type = str(item.get("checkpoint_type", "") or "")
    kind = str(item.get("kind", "") or "") or blocker_kind(checkpoint_type)
    payload = dict(getattr(checkpoint, "payload", {}) or {}) if checkpoint is not None else {}
    facts = _facts(item, payload)
    options = _options(item)
    option_ids = {option["id"] for option in options}
    gate = item.get("gate") if isinstance(item.get("gate"), dict) else {}
    permission = item.get("permission") if isinstance(item.get("permission"), dict) else {}
    order_id, order_label = _order_words(order, item)
    turn = _turn_name(item, waiting_task)

    if kind == "delivery":
        card = _lesson_card(item, facts, words, zh)
    elif kind == "permission" or permission:
        card = _permission_card(item, facts, permission, words, zh, options)
    elif kind == "suspended":
        card = _stopped_card(item, words, zh, options, order_label=order_label, turn=turn)
    elif facts["budget_stop"] or (CONTINUE in option_ids and option_ids <= {CONTINUE, FAIL}):
        card = _allowance_card(item, facts, words, zh, options, turn=turn)
    elif facts["no_receipt"] or (bool(option_ids) and option_ids <= {REDO, SKIP}):
        card = _restart_card(item, facts, words, zh, options, turn=turn)
    elif facts["final_delivery_review"] or (checkpoint_type == "task_user_input" and option_ids == _ACCEPTANCE_OPTION_IDS):
        card = _acceptance_card(facts, words, zh, order_label=order_label, turn=turn)
    elif kind == "gate" or gate or facts["human_node"]:
        card = _review_card(item, facts, gate, words, zh, turn=turn, employee_names=employee_names)
    elif str(facts["tool_call"].get("name", "") or "").strip():
        # A checkpoint of a kind the list does not know, carrying a tool call: it asks for a tool.
        card = _permission_card(item, facts, permission, words, zh, options)
    elif kind == "failure":
        card = _plain_card(item, words, zh, options, title=_joined(_kind_word(REASON_FAILED, zh), order_label or turn),
                           line=words["failure_line"], kind_word=_kind_word(KIND_ANSWER, zh))
    elif kind == "staffing":
        recruiting = checkpoint_type == "company_recruitment_confirmation"
        card = _plain_card(item, words, zh, options, title=words["recruitment_title" if recruiting else "staffing_title"],
                           line=words["recruitment_line" if recruiting else "staffing_line"], kind_word=_kind_word(KIND_ANSWER, zh))
    elif kind == "reorg":
        card = _plain_card(item, words, zh, options, title=words["reorg_title"], line=words["reorg_line"], kind_word=_kind_word(KIND_ANSWER, zh))
    elif kind == "wait":
        card = _plain_card(item, words, zh, options, title=words["peer_wait_title"], line=words["peer_wait_line"], kind_word="")
    else:
        card = _plain_card(item, words, zh, options, title=_joined(_kind_word(KIND_ANSWER, zh), turn),
                           line=words["question_line"], kind_word=_kind_word(KIND_ANSWER, zh))
    card["order_id"] = order_id
    card["order_label"] = order_label
    return card
