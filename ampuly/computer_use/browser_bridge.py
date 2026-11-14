"""Where the product's browser tools meet the Gateway (4.3 X02, first slice).

`browser.py` keeps doing what it does; this puts every tool call into the
Gateway's sequence for the binding's session: sync the control lease from
`BrowserSessionRegistry` (the lifecycle stays the registry's), take a minimal
observation of the page, submit a classified action, and record a receipt
after the runtime returns. Nothing typed into a page and nothing read from it
enters an event: targets carry selectors and lengths, observations carry a
hash (C27).

What this slice is not: the observation has no structured snapshot or stable
element ref yet (the hash is over url and title), coordinates and geometry are
not used by the tools, clicks are classified as reversible writes because the
tool face cannot yet tell "select" from "pay" (that split is X06), and the
Gateway's events live in memory. Each of those is named in the plan.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from ampuly.computer_use.contracts import ACTION_KINDS, ActionClass, ActionReceipt, Geometry, Observation, Outcome, verified_outcome
from ampuly.computer_use.gateway import AGENT_MAY_ACT, Admission, ComputerUseGateway, ControlLease, GatewayJournal, GatewayRefused, LeaseState
from ampuly.layer4_tools.browser_runtime_policy import BrowserRuntimePolicyRefused, require_browser_runtime_policy

# The registry's lifecycle words, in §5.5 terms. created/ready are "nobody has
# driven this page yet"; the registry lets an agent start from there, so the
# bridge marks the agent operating (through the registry) before it asks.
STATE_TO_LEASE: dict[str, LeaseState] = {
    "created": LeaseState.CREATED,
    "ready": LeaseState.CREATED,
    "agent_operating": LeaseState.AGENT_OPERATING,
    "awaiting_human": LeaseState.AWAITING_HUMAN,
    "human_returned": LeaseState.HUMAN_RETURNED,
    "identity_verified": LeaseState.IDENTITY_VERIFIED,
    "revoked": LeaseState.REVOKED,
    "closed": LeaseState.CLOSED,
    "lease_expired": LeaseState.EXPIRED,
}
UNAVAILABLE_TO_LEASE = {"closing": LeaseState.CLOSE_PENDING, "close_failed": LeaseState.CLOSE_FAILED,
                        "other_host": LeaseState.CLOSE_FAILED}  # not this host's to drive: nothing is admitted
HUMAN_STATES = frozenset({LeaseState.HANDOFF_REQUESTED, LeaseState.AWAITING_HUMAN, LeaseState.HUMAN_OPERATING,
                          LeaseState.HUMAN_RETURNED, LeaseState.IDENTITY_CHECKING})

# What each tool is, in the contract's vocabulary. A click is a reversible
# write here only because the tool face cannot yet see what it clicks.
TOOL_KINDS: dict[str, str] = {
    "browser_navigate": "observe",
    "browser_snapshot": "observe",
    "browser_observe": "observe",
    "browser_take_screenshot": "observe",
    "browser_wait_for": "observe",
    "browser_navigate_back": "observe",
    "browser_switch_tab": "switch_tab",
    "browser_scroll": "scroll",
    "browser_click": "select_candidate",
    "browser_type": "type_draft",
    "browser_submit_enter": "submit",
    "browser_select_option": "select_candidate",
}

# X06 first slice: a click is only "select a candidate" when the last snapshot
# says the target is not a submit. A form submit button, or a control whose
# text names an irreversible act, is an external side effect: the Gateway
# then requires an approved Effect claim, and today none can be issued at the
# tool face, so the click is refused rather than sent (C08, C09). The list is
# deliberately short; a miss means a submit that is treated as a draft, so it
# errs towards the words that name money, sending and publishing.
# Words that name an act strongly enough to count even on a plain navigation link (a GET "Delete permanently").
STRONG_EFFECT_WORDS: tuple[str, ...] = (
    "pay", "checkout", "place order", "confirm order", "delete", "authorize", "authorise", "buy", "purchase", "refund", "transfer",
    "withdraw", "top up", "recharge",
    "付款", "支付", "下单", "确认订单", "删除", "转账", "授权", "购买", "退款", "确认收货", "签署", "签约",
    # money out and money in ( review: 提现 / 充值 were not acts, so a script button could move money unasked)
    "提现", "充值", "打款",
    "取现", "提款", "续费", "开通会员", "打赏", "top-up", "topup", "cash out", "add funds", "deposit", "donate",
)
EXTERNAL_EFFECT_WORDS: tuple[str, ...] = STRONG_EFFECT_WORDS + (
    "submit", "send", "publish", "remove", "sign", "save",
    "提交", "发送", "发布", "结算", "保存", "发货", "发表",
    # putting content out ( review of bulk publishing: 「立即投稿」 was a plain click, so it skipped approval)
    "post", "share", "upload", "go live",
    "投稿", "上传", "上线", "推送",
    "分享", "转发", "开始直播", "上架", "发帖", "发评论", "tweet", "reply", "subscribe",
)
# Latin words that count only as whole words (a "Share" button, not "Shared with me" or "Shareholders"; "Post", not
# "Posts", "Postcode" or "post_date"): the review of the first list found them naming navigation.
_WHOLE_WORDS = frozenset({"post", "share", "upload", "go live", "top-up", "topup", "cash out", "add funds", "deposit", "donate",
                          "tweet", "reply", "subscribe"})
# Latin words match at the start of a word, after a verb prefix at most ("pay", "payment", "repay", "resend", "unpublish";
# not "display", "assign", "redesign"); CJK words match inside a label.
_VERB_PREFIX = "(?:re|un|pre|auto|co|counter|bulk)?"


def _word_pattern(word: str) -> re.Pattern[str]:
    if not word.isascii():
        return re.compile(re.escape(word))
    end = "(?![a-z0-9_])" if word in _WHOLE_WORDS else ""
    return re.compile(rf"(?<![a-z0-9]){_VERB_PREFIX}{re.escape(word)}{end}")


_EFFECT_WORD_PATTERNS = tuple(_word_pattern(word) for word in EXTERNAL_EFFECT_WORDS)
# On a plain link a Latin word must also end where the word ends: "Delete permanently", not "Payments" or "Saved".
# On a plain link every effect word counts, but a Latin word must also end where the word ends: "Publish", not
# "Publications"; "Delete permanently", not "Payments" or "Saved items".
_LINK_WORD_PATTERNS = tuple(
    re.compile(rf"(?<![a-z0-9]){_VERB_PREFIX}{re.escape(word)}(?![a-z0-9{'_' if word in _WHOLE_WORDS else ''}])")
    if word.isascii() else re.compile(re.escape(word))
    for word in EXTERNAL_EFFECT_WORDS
)
_CJK = "\u3400-\u9fff\uf900-\ufaff"
# antd writes a two-character Chinese label with a space between (删 除); a gap between CJK characters is not a word break.
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])[\s\u00a0\u3000\u200b-\u200d\ufeff]+(?=[{_CJK}])")
# Navigation that only reads like an act. A whole label that is just a sign-in entry on something that runs no script
# of a button (Sign in with Google, Sign in / Register), or just an order-status tab (待付款(3), 待发货 99+, 已发布文章);
# a product or report name anywhere. A toggle's state word is never excluded: it is the act.
_SIGN_ENTRY = re.compile(r"sign[\s-]*(?:in|up|out|on)"
                         r"(?:\s*(?:/|\||or|或)\s*(?:sign[\s-]*(?:in|up)|register|log[\s-]*in|join|注册|登录))?"
                         r"(?P<tail>\s+(?:with|to|for|using|via)\b.{0,40})?")
_STATUS_LABEL = re.compile(r"[待已未](?:付款|支付|发货|发送|发布|提交|结算|签署|签约|授权|转账|退款|删除|保存|购买"
                           r"|投稿|上传|上线|推送|提现|充值|打款|上架|分享|转发)"
                           r"(?:订单|文章|商品|宝贝|列表|记录|内容|作品|视频|素材|稿件|消息|图片|文件|动态|笔记)?")
# Navigation named after an act: a record, a setting, a centre or a time of it (我的投稿, 投稿管理, 上传记录, 充值中心,
# 消息推送, 按上线时间, Upload history, Post date) -- a whole label; the act itself (立即投稿, 上传视频) is not one.
_NAV_ACTS = "投稿|上传|上线|推送|提现|充值|打款|分享|转发|上架|发帖"
# Only nouns that cannot be the object of the act. 公告/列表 can be ("推送公告" pushes an announcement, "分享列表" shares
# a list) and 明细 is ambiguous, so they are navigation only with a 我的/历史/按 prefix (or as 上线公告); Latin
# records/log/list likewise only after "my" ("Upload records" uploads them). The review of the first list found
# script buttons named like that sent without asking.
_NAV_NOUNS = "记录|管理|设置|时间|历史|中心|规则|说明"
_NAV_LABEL = re.compile(
    rf"(?:我的|消息|历史|按)?(?:{_NAV_ACTS})(?:{_NAV_NOUNS})"
    rf"|(?:我的|历史|按)(?:{_NAV_ACTS})(?:公告|列表|明细)|上线公告"
    rf"|我的(?:{_NAV_ACTS})|消息推送"
    r"|(?:my\s+)?(?:post|share|upload|recharge|top[\s-]?up|withdraw(?:al)?|deposit)s?\s+"
    r"(?:history|center|centre|settings|management|date|time)"
    r"|my\s+(?:post|share|upload|recharge|top[\s-]?up|withdraw(?:al)?|deposit)s?\s+(?:records?|logs?|list)"
)
# A trailing badge, counter or arrow on a navigation label, stripped one at a time (no nested repeats to backtrack on).
_BADGE = re.compile(r"\s*(?:[(（]\s*\d+\+?\s*[)）]|\d+\+?|[→›»>]+)\s*$")
_PRODUCT_NAMES = re.compile(r"支付宝|付款人数|人付款|结算中心|授权管理")
_TOGGLE_ROLES = frozenset({"switch", "checkbox", "radio"})
_COUNTER = re.compile(r"\s*[（(]\s*\d+\s*[sS秒]?\s*[)）]\s*$")
_TRAILING_PUNCT = re.compile(r"[,，.。!！?？:：、\s]+$")
# What confirms whatever a dialog is about (a delete behind Popconfirm): an act only inside a dialog that offers a way
# out or says what it confirms -- never an information popup's lone OK.
_CONFIRM_WORDS = frozenset({"确定", "确认", "是", "是的", "同意", "好的", "确认删除", "ok", "yes", "confirm", "accept", "agree"})
# Words that confirm only as the whole label: "Proceed" or "继续" in a dialog, never "Continue shopping".
_WHOLE_CONFIRM_WORDS = frozenset({"continue", "proceed", "继续", "go ahead", "i understand", "do it"})


def _normal(value: Any) -> str:
    return _CJK_GAP.sub("", str(value or "").lower()).strip()


def _without_badges(label: str) -> str:
    for _ in range(4):
        stripped = _BADGE.sub("", label)
        if stripped == label:
            break
        label = stripped
    return label


def _runs_a_script(element: dict[str, Any]) -> bool:
    tag = str(element.get("tag", "") or "").lower()
    return (tag == "button" or str(element.get("role", "") or "").lower() == "button"
            or (tag == "input" and str(element.get("type", "") or "").lower() in {"button", "submit", "image", "reset"}))


def _excluded_label(label: str, element: dict[str, Any]) -> bool:
    """A whole label that only reads like an act: an order-status tab, or a sign-in entry that no button script runs."""
    label = _without_badges(label)
    if _STATUS_LABEL.fullmatch(label) or _NAV_LABEL.fullmatch(label):
        return True
    entry = _SIGN_ENTRY.fullmatch(label)
    if entry is None or _runs_a_script(element):
        return False
    return not any(p.search(entry.group("tail") or "") for p in _EFFECT_WORD_PATTERNS)  # "Sign in to pay" still names paying


def _effect_text(element: dict[str, Any], keys: tuple[str, ...] = ("text", "label")) -> str:
    toggle = str(element.get("role", "") or "").lower() in _TOGGLE_ROLES
    values = [(raw, _normal(raw)) for raw in (str(element.get(key, "") or "").lower().strip() for key in keys) if raw]
    if not toggle:
        entries = [_without_badges(joined) for _, joined in values if _excluded_label(joined, element)]

        def says_no_more(joined: str) -> bool:
            # An aria-label or title that repeats or extends an excluded label ("Sign in to your account") names nothing new.
            label = _without_badges(joined)
            return any(label.startswith(entry) and not any(p.search(label[len(entry):]) for p in _EFFECT_WORD_PATTERNS) for entry in entries)

        values = [(raw, joined) for raw, joined in values if not (_excluded_label(joined, element) or says_no_more(joined))]
    text = " ".join(f"{raw} {joined}" if joined != raw else raw for raw, joined in values)
    return text if toggle else _PRODUCT_NAMES.sub(" ", text)


def _confirms_in_dialog(element: dict[str, Any]) -> bool:
    # A dialog form's confirm (<form method="dialog">) closes the dialog, and what listens for that close does the act.
    if element.get("in_dialog") is not True or element.get("dialog_info") is True:
        return False
    if not (element.get("dialog_choice") is True or any(p.search(_effect_text(element, ("dialog_text",))) for p in _EFFECT_WORD_PATTERNS)):
        return False
    for key in ("text", "label"):
        short = _TRAILING_PUNCT.sub("", _COUNTER.sub("", _normal(element.get(key, ""))))
        if not short:
            continue
        first = re.split(r"[\s,，.。!！]+", short, maxsplit=1)[0]
        if short in _CONFIRM_WORDS or short in _WHOLE_CONFIRM_WORDS or first in _CONFIRM_WORDS:
            return True
    return False


def _control_name(value: Any) -> str:
    """A button's name as it is compared: the same normalisation a live control's own name already gets."""
    return _without_badges(_normal(value))


def control_refusal(element: dict[str, Any] | None, declared: Any) -> str:
    """``""`` when this click is one the step declared it may make, else why not (P2 护栏一).

    ``declared is None`` means the step declared nothing -- which is every step until one does -- and the answer
    is always ``""``: nothing about today's behaviour changes for a flow that names no buttons. A step that
    *does* declare is held to it, and an empty declaration means it may click nothing at all, which is the only
    honest reading of 「只能点模板里声明过的按钮」.

    Two things are not writes and are not held to the list:

    * putting the cursor in a text field -- what gets typed is classified on its own, and draft text is not an act;
    * a plain link that names no act -- it navigates, and *where* it may land is the other half of this same
      guardrail (``web_scope``). The rule here is about 「其他写入一律拦下」.

    Matched by whole name, never by containment: declaring 「发布」 must not also authorise 「取消发布」 or
    「发布并推广」. A control carrying several names matches on any one of them, because they all name it.
    """
    if declared is None:
        return ""
    if not element:
        return "target_not_observed"
    if element.get("editable") is True and element.get("submits") is not True:
        return ""
    if element.get("plain_link") is True and classify_click(element) != "submit":
        return ""
    names = {name for item in list(declared or []) if (name := _control_name(item))}
    if not names:
        return "undeclared_control"
    for key in ("text", "label", "names", "hit_text", "hit_label", "hit_names"):
        for part in str(element.get(key, "") or "").splitlines():
            if (name := _control_name(part)) and name in names:
                return ""
    return "undeclared_control"


# P2 护栏一: the buttons a connector step declared. Not a browser_* key, so neither task metadata nor a
# delegation can set it -- only the workflow step itself, exactly as with the step's sites.
CONNECTOR_CONTROLS_FLAG = "browser_connector_controls"


def connector_controls_of(metadata: dict[str, Any]) -> list[str] | None:
    """The buttons the task's step declares, or None for a step that declares none (today: every step).

    ``None`` and ``[]`` mean different things on purpose. ``None`` is 「没声明」 and changes nothing; ``[]`` is
    「声明了，一个都不准点」, which is what a template that lists no buttons has actually said.
    """
    node = dict(metadata or {}).get("workflow_node")
    connector = node.get("connector") if isinstance(node, dict) else None
    if not isinstance(connector, dict):
        return None
    raw = connector.get("controls")
    if not isinstance(raw, list):
        return None
    return [name for item in raw if (name := str(item or "").strip())]


# X03: vision functions that put the mouse down on a point -- their target is whatever sits under it.
_VISION_CLICK_FUNCTIONS = frozenset({"click", "left_click", "left_single", "left_double", "double_click", "right_single", "right_click", "drag"})


def classify_click(element: dict[str, Any] | None) -> str:
    """The contract kind for a click on this element (the live element where one was read).

    A submit control (by its type, or a button that submits its form -- a button with no type attribute does)
    is a submit unless its form is an explicit GET or search form, which navigates; a label naming an
    irreversible act is a submit wherever it is. A target nothing describes is refused, never a draft (C08).
    """
    if not element:
        raise GatewayRefused("target_not_observed", "nothing describes this target; take a browser_snapshot and act on an element it lists")
    method = str(element.get("form_method", "") or "").lower()
    # The method the browser will use decides: GET navigates, a search form without a method navigates, a dialog form
    # stays on the page; a role=search never excuses an explicit POST.
    navigates = method == "get" or (method == "" and str(element.get("form_role", "") or "").lower() == "search") or method == "dialog"
    if (str(element.get("type", "") or "").lower() in {"submit", "image"} or element.get("submits") is True) and not navigates:
        return "submit"
    if element.get("editable") is True and element.get("submits") is not True:
        return "select_candidate"  # focusing a text field commits nothing; typing is classified on its own
    if _confirms_in_dialog(element):
        return "submit"
    short = _normal(" ".join(str(element.get(k, "") or "") for k in ("text", "label")))
    hit_words = _normal(" ".join(str(element.get(k, "") or "") for k in ("hit_text", "hit_label", "hit_names")))
    if hit_words and hit_words not in {short, _normal(element.get("text", ""))}:
        # An action labelled on its own inside a link card (发货 in an order card). Inside a plain link it is judged the
        # way the link is: a short label naming an act as a whole word ("Refund", not the nav span "Payments").
        hit = _effect_text(element, ("hit_text", "hit_label", "hit_names"))
        if element.get("plain_link") is True:
            if len(hit_words) <= 80 and any(p.search(hit) for p in _LINK_WORD_PATTERNS):
                return "submit"
        elif any(p.search(hit) for p in _EFFECT_WORD_PATTERNS):
            return "submit"
    # Every name the control carries counts for effect words (a title "Delete" behind an aria-labelledby row name).
    text = _effect_text(element, ("text", "label", "names"))
    if element.get("plain_link") is True:
        # A plain link navigates: a short label naming an act, as a whole word, is still one (a GET "Delete permanently").
        return "submit" if len(short) <= 80 and any(p.search(text) for p in _LINK_WORD_PATTERNS) else "select_candidate"
    if any(pattern.search(text) for pattern in _EFFECT_WORD_PATTERNS):
        return "submit"
    return "select_candidate"


_gateway: ComputerUseGateway | None = None
_journal_path: Callable[[], Path] | None = None
_sequences: dict[str, int] = {}
_process_nonce = secrets.token_hex(4)
# The engine's "publish a side effect into the owner's approval inbox". Bound
# at engine start; absent (tests, probes) a submit stays refused at the door.
_effect_proposer: Callable[..., Any] | None = None
_replay_runs: dict[str, str] = {}


def bind_effect_proposer(proposer: Callable[..., Any] | None) -> None:
    global _effect_proposer
    _effect_proposer = proposer


def note_replay_run(binding: Any, run_id: str) -> bool:
    """Claim one binding for one replay in this process; no nested/parallel replay on the same page."""

    key = binding.key()
    if key in _replay_runs:
        return False
    _replay_runs[key] = str(run_id or "")
    return True


def clear_replay_run(binding: Any, run_id: str) -> None:
    key = binding.key()
    if _replay_runs.get(key) == str(run_id or ""):
        _replay_runs.pop(key, None)


# The model a masked screenshot may go to (X03). Bound at engine start to the
# product's own LLM provider; absent (tests, probes) acting by sight is refused.
_vision_provider: Any = None
# How long a coordinate decided on one screenshot stays valid.
COORDINATE_TTL_SEC = 30.0


# X07: the owner's demo recorder, one recording per binding, fed by settle().
_recorder: Any = None


def recorder() -> Any:
    global _recorder
    if _recorder is None:
        from ampuly.computer_use.demo_recorder import DemoRecorder

        _recorder = DemoRecorder()
    return _recorder


def bind_vision_provider(provider: Any) -> None:
    global _vision_provider
    _vision_provider = provider


# Which engine a browser submit / a look by sight goes through, per project (runtime contract). Every
# engine used to overwrite the two process-wide bindings above at start and never take them back: the last engine
# started owned every project's submits, and a custom-org child that had shut down still did. Each engine now adds one
# layer at start (after it is fully initialized) and takes its own layer back when shutdown begins. A task reads the
# newest layer whose engine is in the task's project, matched by the engine's project AS IT IS NOW ("default" when
# unset, as tasks carry it). Once any engine has bound, a project with no layer gets nothing -- fail closed, never
# another project's engine. The process-wide bindings stay only for what binds them directly (tests and probes).
_engine_layers: list[tuple[Any, str, Callable[..., Any] | None, Any]] = []


def _project_key(value: Any) -> str:
    return str(value or "").strip() or "default"


def _layer_project(layer: tuple[Any, str, Callable[..., Any] | None, Any]) -> str:
    owner, bound_project = layer[0], layer[1]
    return _project_key(getattr(owner, "project_id", None) if hasattr(owner, "project_id") else bound_project)


def bind_engine_bindings(owner: Any, *, project_id: str, proposer: Callable[..., Any] | None, provider: Any) -> None:
    """One engine's submit path and vision model, for the project it runs (read again at every lookup)."""
    _engine_layers[:] = [layer for layer in _engine_layers if layer[0] is not owner]
    _engine_layers.append((owner, _project_key(project_id), proposer, provider))


def unbind_engine_bindings(owner: Any) -> None:
    """An engine shutting down takes its layer back: nothing of its project goes through it any more."""
    _engine_layers[:] = [layer for layer in _engine_layers if layer[0] is not owner]


def _task_layer(task: Any) -> tuple[Any, str, Callable[..., Any] | None, Any] | None:
    project_id = _project_key(getattr(task, "project_id", None) if task is not None else None)
    for layer in reversed(_engine_layers):
        if _layer_project(layer) == project_id:
            return layer
    return None


# Q05 B3: the browser decider. The plugin binds it (PULY over JEV, browser_decider.py); this layer never imports it.
_decision_provider: Any = None
_cancel_generations: dict[str, int] = {}
_decider_held: dict[str, str] = {}
_decisions: dict[str, int] = {}
_decider_seeded: set[str] = set()
# Q05 B5: the connector's done check -- what held before the step worked (its baseline) and each verdict, journaled.
_done_baselines: dict[str, dict[str, Any]] = {}
_done_verdicts: dict[str, dict[str, Any]] = {}
# Bulk P4: the per-item ledger. One terminal row per item, frozen the moment that item closed; nothing that happens
# afterwards can change it. Keyed binding -> item -> row, and mirrored into the journal as `item_result` events.
_item_results: dict[str, dict[str, dict[str, Any]]] = {}
# This binding's journal as read once per process (at seeding), and where the in-memory session stood then: its events
# are this history plus what the session recorded since -- the journal file is never read again on a tool call.
_history: dict[str, list[dict[str, Any]]] = {}
_history_mark: dict[str, tuple[int, int]] = {}


def bind_decision_provider(provider: Any) -> None:
    global _decision_provider
    _decision_provider = provider


# Bulk P1 (lookup mode): the value of an order's own input, read by the plugin from the business record -- never from
# the prompt or the page. The kernel only asks; ``resolver(task, name) -> str | None``.
_order_input_resolver: Any = None


def bind_order_input_resolver(resolver: Any) -> None:
    global _order_input_resolver
    _order_input_resolver = resolver


def order_input(task: Any, name: str) -> str | None:
    """An order input's value for this task's order, or None when it cannot be resolved (fail closed)."""
    resolver = _order_input_resolver
    if resolver is None or not str(name or "").strip():
        return None
    try:
        value = resolver(task, str(name).strip())
    except Exception:  # noqa: BLE001 - an input that cannot be read is no input
        return None
    return None if value is None else str(value)


# Bulk P4 (batch mode): the list of rows a batch step works through. Read by the plugin from the order's own
# business record, exactly as a lookup's key is -- never from the prompt, a page, task metadata or a tool argument.
# The caps live here, in the kernel, so no plugin resolver can widen them.
ORDER_ITEMS_LIMIT = 200
ITEM_FIELD_MAX = 64
ITEM_VALUE_MAX = 200
_order_items_resolver: Any = None


def bind_order_items_resolver(resolver: Any) -> None:
    global _order_items_resolver
    _order_items_resolver = resolver


def order_items(task: Any, name: str) -> list[dict[str, str]] | None:
    """The rows this batch step works through, or None when they cannot be read (fail closed).

    Every row is flattened to a plain ``str -> str`` map and bounded here: a page, a model or a mis-shaped order
    record can never make the list longer, the keys wider or the values bigger than the kernel allows. A list past
    :data:`ORDER_ITEMS_LIMIT` is refused outright rather than truncated -- silently dropping rows from a
    distribution list is the one failure nobody would notice.
    """
    resolver = _order_items_resolver
    if resolver is None or not str(name or "").strip():
        return None
    try:
        rows = resolver(task, str(name).strip())
    except Exception:  # noqa: BLE001 - a list that cannot be read is no list
        return None
    if not isinstance(rows, list) or not rows or len(rows) > ORDER_ITEMS_LIMIT:
        return None
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        out.append({str(k)[:ITEM_FIELD_MAX]: str(v)[:ITEM_VALUE_MAX] for k, v in row.items()
                    if not isinstance(v, (dict, list, bool)) and v is not None})
    return out


def batch_items(task: Any) -> list[dict[str, str]] | None:
    """The rows of the batch step this task runs, in order, or None for a step that is not one."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    connector = node.get("connector") if isinstance(node, dict) else None
    verify = connector.get("verify") if isinstance(connector, dict) else None
    if not isinstance(verify, dict) or str(verify.get("mode", "") or "") != "batch":
        return None
    ref = str(dict(verify.get("items", {}) or {}).get("from", "") or "")
    name = ref.rsplit(".", 1)[-1] if ref.startswith("order.inputs.") else ""
    return order_items(task, name)


def batch_item_key(task: Any) -> str:
    """Which field of a row is that row's identity. '' for a step that is not a batch step."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    connector = node.get("connector") if isinstance(node, dict) else None
    verify = connector.get("verify") if isinstance(connector, dict) else None
    if not isinstance(verify, dict) or str(verify.get("mode", "") or "") != "batch":
        return ""
    return str(verify.get("item_key", "") or "")


def batch_step(task: Any) -> bool:
    """Whether this task runs a connector step in batch mode (bulk P4): it works a list, item by item."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    connector = node.get("connector") if isinstance(node, dict) else None
    verify = connector.get("verify") if isinstance(connector, dict) else None
    return isinstance(verify, dict) and str(verify.get("mode", "") or "") == "batch"


def lookup_step(task: Any) -> bool:
    """Whether this task runs a connector step in lookup mode (bulk P1): it reads a site, it never writes one."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    connector = node.get("connector") if isinstance(node, dict) else None
    verify = connector.get("verify") if isinstance(connector, dict) else None
    return isinstance(verify, dict) and str(verify.get("mode", "") or "") == "lookup"


def decision_provider() -> Any:
    return _decision_provider


def cancel_generation(binding: Any) -> int:
    """How many times this binding's turn was cancelled: a decider loop that started before a cancel stops at once."""
    return _cancel_generations.get(binding.key(), 0)


def hold_decider(binding: Any, reason: str) -> None:
    """No more questions to the decider for this work item: its last ending was unknown or its allowance stopped it."""
    _decider_held[binding.key()] = str(reason or "held")
    gateway_annotate(binding, {"type": "decider_held", "reason": str(reason or "held")[:60]})


def _seed_from_journal(binding: Any) -> None:
    """After a restart: the latch and the count come back from the journal (their events are journaled as they happen)."""
    key = binding.key()
    if key in _decider_seeded:
        return
    _decider_seeded.add(key)
    journal = getattr(gateway(), "_journal", None)
    try:
        events = list(journal.events(key)) if journal is not None else []
    except Exception:  # noqa: BLE001 - an unreadable journal leaves the in-process record
        events = []
    _history[key] = [dict(event) for event in events]
    session = gateway()._sessions.get(key)
    # The journal already holds what this process's session recorded so far (written through) -- unless there is no
    # journal, or it could not be read: then every in-memory event counts from its start.
    offset = len(session.events) if (session is not None and journal is not None and events) else 0
    _history_mark[key] = (id(session) if session is not None else 0, offset)
    decisions = sum(1 for event in events if event.get("type") == "decision")
    if decisions > _decisions.get(key, 0):
        _decisions[key] = decisions
    held = [event for event in events if event.get("type") == "decider_held"]
    if held and key not in _decider_held:
        _decider_held[key] = str(held[-1].get("reason", "") or "held")
    # Bulk P4: folded per item. An event with no item folds into the slot it always did.
    merged_by_item: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") == "done_baseline":
            slot = _slot(key, str(event.get("item", "") or ""))
            merged_by_item[slot] = _extend_baseline(merged_by_item.get(slot), event)
    for slot, merged in merged_by_item.items():
        if slot not in _done_baselines:
            _done_baselines[slot] = merged
    for event in events:
        if event.get("type") == "done_check":
            slot = _slot(key, str(event.get("item", "") or ""))
            # The LAST verdict per slot wins; a later event simply overwrites what an earlier one seeded.
            _done_verdicts.setdefault(slot, {}).update(_clean_verdict(event))
    for event in events:
        if event.get("type") == "item_result":
            item = str(event.get("item", "") or "")
            _item_results.setdefault(key, {})[item] = {k: event.get(k) for k in
                                                       ("item", "status", "reason", "conditions", "at", "evidence")}


def decider_held(binding: Any) -> bool:
    _seed_from_journal(binding)
    return binding.key() in _decider_held


def note_decision(binding: Any, event: dict[str, Any]) -> int:
    """One question asked of the decider, counted against the step's max_steps and journaled (keys the audit view shows)."""
    count = _decisions.get(binding.key(), 0) + 1
    _decisions[binding.key()] = count
    gateway_annotate(binding, {"type": "decision", **{k: v for k, v in dict(event).items() if k in {"function", "model", "outcome", "decided_on"}},
                               "sequence": count})
    return count


def decisions_made(binding: Any) -> int:
    _seed_from_journal(binding)
    return _decisions.get(binding.key(), 0)


# The baseline: what already held on the step's own sites before its first write. Conditions are kept by a digest of
# (kind, value) -- never by their place in the list, which an edited flow may change -- and downloads by the product's
# own file names. Past the limit the file list is not kept at all and no download counts as new (fail closed).
DONE_BASELINE_FILES_LIMIT = 2000
_WRITE_RISKS = frozenset({ActionClass.REVERSIBLE_WRITE.value, ActionClass.EXTERNAL_EFFECT.value, ActionClass.HOST.value})
_UNKNOWN_OUTCOMES = frozenset({Outcome.OUTCOME_UNKNOWN.value, Outcome.CANCELLED_UNKNOWN.value, Outcome.AWAITING_HUMAN.value})


def done_condition_key(kind: str, value: str) -> str:
    """A postcondition by what it says, not where it stands in the list."""
    return hashlib.sha256(f"{kind}\x00{value}".encode("utf-8")).hexdigest()[:16]


def _extend_baseline(current: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
    held = set((current or {}).get("held", [])) | {str(k)[:32] for k in list(event.get("held", []) or [])}
    overflow = bool((current or {}).get("overflow")) or bool(event.get("overflow"))
    files = set((current or {}).get("downloads", [])) | {str(name)[:80] for name in list(event.get("downloads", []) or [])}
    if len(files) > DONE_BASELINE_FILES_LIMIT:
        overflow = True
    return {"held": sorted(held), "downloads": [] if overflow else sorted(files), "overflow": overflow}


def _clean_verdict(event: dict[str, Any]) -> dict[str, Any]:
    return {"status": str(event.get("status", "") or ""), "reason": str(event.get("reason", "") or "")[:80], "at": event.get("at"),
            "conditions": str(event.get("conditions", "") or "")[:32]}


def done_conditions_digest(posts: list[dict[str, Any]]) -> str:
    """The set of postconditions a verdict was about: a verdict on another set (the flow was edited) does not stand."""
    keys = sorted(done_condition_key(str(p.get("kind", "") or ""), str(p.get("value", "") or "").strip()) for p in list(posts or []))
    return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:16]


# Bulk P4: the item dimension. A step used to be one unit of work -- one baseline, one verdict, and four
# binding-wide booleans that let any later action void an earlier answer. A batch step does the same work several
# times over, and 「后一条的操作不让前一条作废」 needs each pass judged on its own.
#
# The whole dimension is this: the binding's events are already one ordered stream, so an `item_start` marker
# PARTITIONS it. Nothing else is tagged. That keeps the Gateway's event shape untouched, and -- because the stream
# is written through to the journal line by line -- the partition comes back after a restart for free.
#
# ``item=""`` everywhere below means 「没有条目」 and is deliberately byte-identical to what this module did before
# P4 existed: the same dict keys, the same events, the same answers. That is the phase's rollback promise
# 「没有台账时仍走今天的卡片」, and the tests hold it literally.


def _slot(key: str, item: str) -> str:
    """Where this binding's per-item state lives. Without an item it is exactly today's key, not a new one."""
    return key if not item else f"{key}\x1f{item}"


def _item_of(events: list[dict[str, Any]]) -> list[str]:
    """Which item each event belongs to: the one whose ``item_start`` most recently precedes it, '' before any."""
    out: list[str] = []
    current = ""
    for event in events:
        if event.get("type") == "item_start":
            current = str(event.get("item", "") or "")
        out.append(current)
    return out


def _owned_by(events: list[dict[str, Any]], item: str) -> list[bool]:
    """A mask over ``events``. Without an item every event counts, which is what every rule saw before P4."""
    if not item:
        return [True] * len(events)
    return [owner == item for owner in _item_of(events)]


def note_item_start(binding: Any, item_key: str, *, index: int = 0, total: int = 0) -> str:
    """Bulk P4: this item's work begins here. Everything after it, until the next marker, belongs to it."""
    item = str(item_key or "").strip()[:200]
    if not item:
        return ""
    _seed_from_journal(binding)
    gateway_annotate(binding, {"type": "item_start", "item": item, "index": int(index), "total": int(total),
                               "at": time.time()})
    return item


def current_item(binding: Any) -> str:
    """The item the step is working on now, or '' for a step that names none (every step until one does)."""
    for event in reversed(_binding_events(binding)):
        if event.get("type") == "item_start":
            return str(event.get("item", "") or "")
    return ""


def note_item_result(binding: Any, item_key: str, status: str, reason: str = "", *,
                     conditions: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bulk P4: freeze this item's answer. Terminal -- a later item cannot reach back through a written row.

    Recorded in the journal, which is the record of truth, and held in memory as a fallback for the case the
    journal refuses (no live session: ``gateway_annotate`` swallows that, and ``done_verdict`` has the same
    fallback for the same reason). A re-run appends; the reader takes the last row per item.
    """
    _seed_from_journal(binding)
    item = str(item_key or "").strip()[:200]
    row = {
        "item": item,
        "status": str(status or "")[:32],
        "reason": str(reason or "")[:80],
        "conditions": str(conditions or "")[:32],
        "at": time.time(),
        "evidence": {str(k)[:32]: str(v)[:200] for k, v in dict(evidence or {}).items()},
    }
    _item_results.setdefault(binding.key(), {})[item] = row
    gateway_annotate(binding, {"type": "item_result", **row})
    return row


def item_results(binding: Any) -> list[dict[str, Any]]:
    """Every item's last row, oldest item first. Empty for a step that has no ledger -- which is every step today."""
    _seed_from_journal(binding)
    rows: dict[str, dict[str, Any]] = {}
    for event in _binding_events(binding):
        if event.get("type") == "item_result":
            rows[str(event.get("item", "") or "")] = {k: event.get(k) for k in
                                                      ("item", "status", "reason", "conditions", "at", "evidence")}
    for item, row in dict(_item_results.get(binding.key(), {})).items():
        rows.setdefault(item, row)
    return list(rows.values())


def _binding_events(binding: Any) -> list[dict[str, Any]]:
    """This binding's Gateway events in the order they happened: the journal as read once (at seeding) followed by what
    the in-memory session recorded since. A journal that cannot be read counts as empty and every in-memory event is
    used, so a check can only see less history, never skip a write this process made."""
    _seed_from_journal(binding)
    key = binding.key()
    history = _history.get(key, [])
    try:
        session = gateway()._sessions.get(key)
    except Exception:  # noqa: BLE001
        session = None
    if session is None:
        return list(history)
    owner, offset = _history_mark.get(key, (0, 0))
    start = offset if owner == id(session) else 0
    return [*history, *list(session.events[start:])]


def _is_write(event: dict[str, Any]) -> bool:
    return event.get("type") == "admitted" and str(event.get("risk", "") or "") in _WRITE_RISKS


def writes_admitted(binding: Any, *, item: str = "") -> bool:
    """Whether a write (a click, typing, a choice, a submit) was ever admitted on this binding.

    With an item, only writes inside that item's partition count (Bulk P4): item 1's post is already on the page
    when item 2 begins, and it is not item 2's doing.
    """
    events = _binding_events(binding)
    return any(_is_write(event) for event, mine in zip(events, _owned_by(events, item)) if mine)


def effect_outcome_unknown(binding: Any, *, item: str = "") -> bool:
    """An external effect (a submit the owner approved, a send, a payment) whose latest receipt is not known, or that has
    none yet. Such an action never locks later writes (it carries an Effect claim, gateway.py), so it is asked here. A
    late receipt -- the executor's own report after the Gateway had settled the attempt as unknown -- resolves nothing."""
    effects: list[str] = []
    latest: dict[str, str] = {}
    events = _binding_events(binding)
    for event, mine in zip(events, _owned_by(events, item)):
        kind = event.get("type")
        if kind == "admitted" and str(event.get("risk", "") or "") == ActionClass.EXTERNAL_EFFECT.value and mine:
            effects.append(str(event.get("action_id", "") or ""))
        elif kind == "receipt" and event.get("action_id"):
            # Receipts are read from the WHOLE stream whatever the item: a receipt belongs to its action, and one
            # that arrives after the next item started still settles the action it names.
            latest[str(event["action_id"])] = str(event.get("outcome", "") or "")
    return any(action and (latest.get(action) is None or latest.get(action) in _UNKNOWN_OUTCOMES) for action in effects)


def verified_external_effect(binding: Any, *, item: str = "") -> bool:
    """A successful, independently verified submit in this item's own history.

    Navigation, draft edits, late receipts, failed inputs and unresolved
    siblings cannot close the pre-work baseline.
    """
    actions: set[str] = set()
    latest: dict[str, str] = {}
    events = _binding_events(binding)
    for event, mine in zip(events, _owned_by(events, item)):
        if (mine and event.get("type") == "admitted"
                and event.get("risk") == ActionClass.EXTERNAL_EFFECT.value):
            actions.add(str(event.get("action_id") or ""))
        elif event.get("type") == "receipt" and event.get("action_id"):
            latest[str(event["action_id"])] = str(event.get("outcome") or "")
    if any(not latest.get(action) or latest[action] in _UNKNOWN_OUTCOMES for action in actions):
        return False
    return any(action and latest.get(action) == Outcome.SUCCEEDED.value for action in actions)


def note_done_baseline(binding: Any, *, held: list[str], downloads: list[str], item: str = "") -> None:
    """Q05 B5: extend the baseline -- page conditions that hold on the step's own sites, and the downloads the work item
    already has -- until its first write (the caller stops calling then). Only what is new is journaled; a restart
    brings the whole baseline back, never re-taking it on a later page."""
    _seed_from_journal(binding)
    slot = _slot(binding.key(), item)
    current = _done_baselines.get(slot)
    extended = _extend_baseline(current, {"held": held, "downloads": downloads})
    if current is not None and extended == current:
        return
    _done_baselines[slot] = extended
    before = current or {"held": [], "downloads": [], "overflow": False}
    gateway_annotate(binding, {"type": "done_baseline", "held": sorted(set(extended["held"]) - set(before["held"])),
                               "downloads": sorted(set(extended["downloads"]) - set(before["downloads"])),
                               "overflow": extended["overflow"],
                               # Only when there is one: an event without this key is exactly today's event.
                               **({"item": item} if item else {})})


def done_baseline(binding: Any, *, item: str = "") -> dict[str, Any] | None:
    _seed_from_journal(binding)
    found = _done_baselines.get(_slot(binding.key(), item))
    return dict(found) if found is not None else None


def note_done_verdict(binding: Any, status: str, reason: str = "", *, conditions: str = "", item: str = "") -> None:
    """Q05 B5: what the done check found (done_verified / done_unverified) and the set of conditions it checked,
    journaled: the step's completion reads it, in the order of the Gateway's events."""
    _seed_from_journal(binding)
    at = time.time()
    verdict = _clean_verdict({"status": status, "reason": reason, "at": at, "conditions": conditions})
    _done_verdicts[_slot(binding.key(), item)] = verdict
    gateway_annotate(binding, {"type": "done_check", "status": verdict["status"], "reason": verdict["reason"],
                               "conditions": verdict["conditions"], "at": at,
                               **({"item": item} if item else {})})


def _last_verdict(events: list[dict[str, Any]], item: str = "") -> tuple[int, dict[str, Any]] | None:
    """The last done check OF THIS ITEM. Matched on the event's own item, which an event written before P4 does
    not carry -- so ``item=""`` finds exactly the events it always found."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.get("type") == "done_check" and str(event.get("item", "") or "") == item:
            return index, _clean_verdict(event)
    return None


def done_verdict(binding: Any, *, item: str = "") -> dict[str, Any] | None:
    """The last done check of this work item's browser, or None when none ran."""
    found = _last_verdict(_binding_events(binding), item)
    if found is not None:
        return found[1]
    fallback = _done_verdicts.get(_slot(binding.key(), item))
    return dict(fallback) if fallback is not None else None


def done_verified(binding: Any, since: float | None = None, conditions: str | None = None, *, item: str = "") -> bool:
    """Whether the last done check held and still stands: it checked these conditions (when given), it ran in this turn
    (at or after `since`, when given), nothing was written after it -- by the order of the Gateway's events, never by
    the clock -- no write is unresolved and no external effect's outcome is unknown."""
    events = _binding_events(binding)
    found = _last_verdict(events, item)
    if found is None:
        return False
    index, verdict = found
    if verdict.get("status") != "done_verified":
        return False
    if conditions is not None and verdict.get("conditions") != conditions:
        return False
    if since is not None:
        try:
            if float(verdict.get("at") or 0.0) < float(since):
                return False
        except (TypeError, ValueError):
            return False
    owned = _owned_by(events, item)
    if any(_is_write(event) for event, mine in zip(events[index + 1:], owned[index + 1:]) if mine):
        return False
    return not (outcome_unresolved(binding, item=item) or effect_outcome_unknown(binding, item=item))


def recent_actions(binding: Any, limit: int = 5) -> list[dict[str, str]]:
    """The last actions this work item's browser completed, by the Gateway's own ids (evidence; never page text)."""
    events = _binding_events(binding)
    done = [{"action_id": str(e.get("action_id", "")), "outcome": str(e.get("outcome", ""))}
            for e in events if e.get("type") == "receipt" and e.get("action_id")]
    return done[-max(1, int(limit)):]


# Q05 B3: acts the decider never takes on its own, beyond the submit words -- a business decision on a record
# (cancel, close, approve, reject, withdraw, save, send, confirm). The step's own model proposes them for approval.
DECISION_WORDS: tuple[str, ...] = (
    "cancel", "close order", "approve", "reject", "decline", "unsubscribe", "confirm", "save", "send", "submit", "publish",
    "archive", "remove", "withdraw", "revoke", "unlink", "deactivate", "settle",
    "取消", "关闭订单", "审核", "通过", "驳回", "拒绝", "下架", "上架", "发布", "退订", "确认", "提交", "保存", "发送",
    "同意", "作废", "撤销", "撤回", "注销", "解绑", "停用", "结算", "移除",
)
_DECISION_WORD_PATTERNS: tuple[Any, ...] = ()


def names_an_effect(text: str) -> bool:
    """Whether these words name an irreversible act (the submit words, EXTERNAL_EFFECT_WORDS). A whole label that only
    names navigation after an act (已上传, 按上线时间, Post date) does not."""
    normal = _normal(text)
    if _excluded_label(normal, {}):
        return False
    return any(pattern.search(normal) for pattern in _EFFECT_WORD_PATTERNS)


def names_a_decision(element: dict[str, Any] | None) -> bool:
    """Whether a control's words name a business decision the decider leaves to the step (DECISION_WORDS)."""
    global _DECISION_WORD_PATTERNS
    if not _DECISION_WORD_PATTERNS:
        _DECISION_WORD_PATTERNS = tuple(_word_pattern(word) for word in DECISION_WORDS)
    text = _effect_text(dict(element or {}), ("text", "label", "names"))
    return any(pattern.search(text) for pattern in _DECISION_WORD_PATTERNS)


def outcome_unresolved(binding: Any, *, item: str = "") -> bool:
    """Whether a write of this binding ended unknown, or a cancel is waiting for a fresh observation (read only).

    With an item, only that item's own writes are asked about (Bulk P4). A cancel stays whole-session on purpose:
    a cancel is the turn's, not one item's.
    """
    try:
        session = gateway()._sessions.get(binding.key())
    except GatewayRefused:
        return False
    if session is None:
        return False
    if not item:
        return bool(session.unresolved or session.cancelled_until_observed)
    events = _binding_events(binding)
    mine = {str(event.get("action_id", "") or "")
            for event, owned in zip(events, _owned_by(events, item))
            if owned and event.get("type") == "admitted" and event.get("action_id")}
    return bool((set(dict(session.unresolved or {})) & mine) or session.cancelled_until_observed)


def agent_may_act(registry: Any, binding: Any) -> bool:
    """Whether the agent holds this page now (the lease as the registry sees it)."""
    try:
        view = registry.lease_view(binding)
    except Exception:  # noqa: BLE001 - no session: nothing to act on
        return False
    held = UNAVAILABLE_TO_LEASE.get(view.get("unavailable", "")) or STATE_TO_LEASE.get(view.get("state", ""), LeaseState.CLOSED)
    return held in AGENT_MAY_ACT


def vision_provider(task: Any = None) -> Any:
    """The vision model of the engine running this task's project. None when engines are bound but none runs that
    project (never another project's); the process-wide one only when no engine has bound (tests, probes)."""
    if not _engine_layers:
        return _vision_provider
    layer = _task_layer(task)
    return layer[3] if layer is not None else None


def effect_proposer(task: Any = None) -> Callable[..., Any] | None:
    """Where this task's browser submit is proposed: the engine running its project. None when engines are bound but
    none runs that project (the submit is refused, never filed in another project); the process-wide one only when no
    engine has bound (tests, probes)."""
    if not _engine_layers:
        return _effect_proposer
    layer = _task_layer(task)
    return layer[2] if layer is not None else None


def _default_journal_path() -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "browser" / "gateway-events.jsonl"


def configure(journal_path: Callable[[], Path] | Path | None) -> None:
    """Where the Gateway's journal lives. None keeps it in memory (tests, probes)."""
    global _gateway, _journal_path, _process_nonce
    _journal_path = (lambda p=journal_path: Path(p)) if isinstance(journal_path, (str, Path)) else journal_path
    _gateway = None
    _sequences.clear()
    _process_nonce = secrets.token_hex(4)


def gateway() -> ComputerUseGateway:
    global _gateway
    if _gateway is None:
        journal = GatewayJournal(_journal_path()) if _journal_path is not None else None
        _gateway = ComputerUseGateway(journal=journal)
    return _gateway


def reset_for_tests() -> None:
    configure(None)
    _engine_layers.clear()
    bind_effect_proposer(None)
    bind_vision_provider(None)
    bind_decision_provider(None)
    bind_order_input_resolver(None)
    bind_order_items_resolver(None)   # bulk P4: a resolver left bound is a test reading another test's list
    _cancel_generations.clear()
    _decider_held.clear()
    _decisions.clear()
    _decider_seeded.clear()
    _done_baselines.clear()
    _done_verdicts.clear()
    _item_results.clear()
    _history.clear()
    _history_mark.clear()
    _task_flags.clear()
    _delegations.clear()
    _replay_runs.clear()
    global _recorder
    _recorder = None


# The product journals; anything that imports this module for tests calls
# reset_for_tests() / configure() to point elsewhere.
configure(_default_journal_path)


def scope_digest_for(binding: Any) -> str:
    return hashlib.sha256(f"browser:{binding.key()}".encode()).hexdigest()


def workflow_scope_for(binding: Any) -> str:
    from ampuly.computer_use.workflow_contracts import workflow_scope_digest

    return workflow_scope_digest(project_id=str(getattr(binding, "project_id", "") or ""),
                                 customer_id=str(getattr(binding, "customer_id", "") or ""),
                                 environment_id=str(getattr(binding, "environment_id", "") or ""))


def sync_lease(registry: Any, binding: Any) -> ControlLease:
    """The Gateway's lease says what the registry says, every time, before anything else."""
    # The term the task set for the agent's control, if any: from the task's
    # metadata, never from a tool argument (X01 / §5.5).
    minutes = _task_flag(binding, "browser_lease_minutes")
    binder = getattr(registry, "bind_lease_term", None)
    if minutes and callable(binder):
        try:
            binder(binding, float(minutes) * 60)
        except (TypeError, ValueError):
            pass
    view = registry.lease_view(binding)
    session_id = binding.key()
    state = UNAVAILABLE_TO_LEASE.get(view["unavailable"]) or STATE_TO_LEASE.get(view["state"], LeaseState.CREATED)
    hosts = tuple(view["allowed_domains"])
    left = view.get("lease_seconds_left")
    expires_at = gateway()._clock() + float(left) if left is not None and float(left) > 0 else 0.0
    try:
        lease = gateway().lease(session_id)
    except Exception:  # noqa: BLE001 - first sight of this binding
        lease = ControlLease(lease_id=f"lease:{session_id}", session_id=session_id, scope_digest=scope_digest_for(binding),
                             state=state, allowed_hosts=hosts, expires_at=expires_at)
        gateway().open_session(lease)
        return lease
    if lease.state != state:
        gateway().set_lease_state(session_id, state, note="synced from browser session registry")
    lease.allowed_hosts = hosts
    lease.expires_at = expires_at
    return lease


async def _control_page_facts(runtime: Any, *, include_cookie_count: bool = False) -> dict[str, Any]:
    """Consume the runtime's public, bounded read contract, never its private page."""
    if runtime is None:
        return {}  # a restored lifecycle record may legitimately have no browser
    reader = getattr(runtime, "page_facts_for_control", None)
    if not callable(reader):
        raise GatewayRefused("browser_control_facts_unavailable", "the browser runtime has no control observation contract")
    try:
        facts = reader(include_cookie_count=include_cookie_count)
        facts = await facts if inspect.isawaitable(facts) else facts
    except Exception:
        raise GatewayRefused("browser_control_facts_unavailable", "the browser control observation could not be read") from None
    if (not isinstance(facts, dict) or set(facts) - {"url", "title", "cookie_count"}
            or any(key in facts and (not isinstance(facts[key], str) or len(facts[key]) > limit)
                   for key, limit in (("url", 8192), ("title", 512)))
            or ("cookie_count" in facts and (type(facts["cookie_count"]) is not int
                                             or not 0 <= facts["cookie_count"] <= 1_000_000))):
        raise GatewayRefused("browser_control_facts_invalid", "the browser returned an invalid control observation")
    return dict(facts)


async def _page_facts(runtime: Any) -> tuple[str, str]:
    facts = await _control_page_facts(runtime)
    return facts.get("url", ""), facts.get("title", "")


async def page_facts_for_handoff(runtime: Any) -> dict[str, Any]:
    """C07: what a verification may compare -- the address (host and path) and how many cookies the
    context holds. No cookie value, no header, no page text leaves the runtime."""
    observed = await _control_page_facts(runtime, include_cookie_count=True)
    facts: dict[str, Any] = {}
    if "url" in observed:
        parts = urlsplit(observed["url"])
        facts.update({"host": (parts.hostname or "").lower(), "path": parts.path or ""})
    if "title" in observed:
        facts["title"] = observed["title"][:80]
    if "cookie_count" in observed:
        facts["cookies"] = observed["cookie_count"]
    return facts


def handoff_checks(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The page's own account of a hand-back: did the address leave the login page, stay on the
    site, and did the session gain cookies. Facts for the owner's decision, not the decision."""
    checks: dict[str, Any] = {}
    # Missing observations are not evidence that navigation happened. Empty
    # strings, however, are explicit facts (for example about:blank's host).
    known_hosts = all(isinstance(facts.get("host"), str) for facts in (before, after))
    if known_hosts:
        checks["same_host"] = before["host"] == after["host"]
        if all(isinstance(facts.get("path"), str) for facts in (before, after)):
            checks["url_changed"] = (before["host"], before["path"]) != (after["host"], after["path"])
    if "cookies" in before and "cookies" in after:
        checks["cookies_grew"] = int(after["cookies"]) > int(before["cookies"])
    return checks


def handoff_adjudication(before: dict[str, Any], after: dict[str, Any], allowed_domains: Any) -> tuple[bool, dict[str, Any]]:
    """C07: the page's own account of a hand-back, as a RECOMMENDATION for the owner -- never a grant.

    The 2026-09-14 adversarial review showed every fact here is page-controlled: an untrusted page
    sets its own path (history.replaceState), its own cookie count (document.cookie) and its own
    structure, so it can make all three look good with no login at all. So this never verifies on its
    own; it only tells the owner whether the page looks consistent with a completed login (all of:
    facts recorded, the address left the hand-off page, the host is on a delegated site, cookies grew).
    The owner's explicit verify_identity is the only thing that restores agent control. Returns
    (recommend, checks); no cookie value, header or page text is read.
    """
    checks = handoff_checks(before, after)
    host = str(after.get("host", "") or "").lower()
    domains = [str(d or "").strip().lower().lstrip(".") for d in list(allowed_domains or []) if str(d or "").strip()]
    checks["on_allowed_site"] = bool(host) and any(host == d or host.endswith("." + d) for d in domains)
    recommend = (bool(before) and checks.get("url_changed") is True and checks["on_allowed_site"] is True
                 and checks.get("cookies_grew") is True)
    checks["recommend"] = recommend
    return recommend, checks


_task_flags: dict[str, dict[str, Any]] = {}


# X08: a delegation the console wrote while a turn was already running. The
# turn's Task object still carries the metadata it was loaded with; the newer
# delegation wins until the task is reloaded with it (then they agree).
_delegations: dict[str, dict[str, Any]] = {}


# Q05 B2: a flag only the workflow step can set -- never a browser_* key of the task or of a delegation.
CONNECTOR_DOMAINS_FLAG = "browser_connector_domains"
# Data phase 1, C6: the order and step the task works in (ampuly.core.order_layout.OrderArea, or None), computed from the
# task's durable roots on every call. Not a browser_* key, so neither task metadata nor a delegation can set it.
ORDER_AREA_FLAG = "_order_area"


def note_delegation(binding: Any, metadata: dict[str, Any]) -> None:
    """The console just delegated a browser scope to this binding's order: hold it for the running turn."""
    _delegations[binding.key()] = {k: v for k, v in dict(metadata or {}).items()
                                   if k.startswith("browser_") and k not in {CONNECTOR_DOMAINS_FLAG, CONNECTOR_CONTROLS_FLAG}}


def connector_domains_of(metadata: dict[str, Any]) -> list[str] | None:
    """Where the task's step may go, or None for a step without a connector.

    The sites it names, narrowed by the addresses it named inside them (P2 护栏一). A step that named no
    addresses gets its sites whole, exactly as before that field existed.
    """
    from ampuly.computer_use.web_scope import effective_scope

    node = dict(metadata or {}).get("workflow_node")
    connector = node.get("connector") if isinstance(node, dict) else None
    if not isinstance(connector, dict):
        return None
    return list(effective_scope(connector.get("allowed_domains", []) or [],
                                connector.get("allowed_paths", []) or []))


def note_task_flags(binding: Any, metadata: dict[str, Any]) -> None:
    """What the launching task allows for this binding. Set by the tool layer from the task on every call;
    a delegation written since the task was loaded overrides the task's own copy (X08)."""
    flags = {k: v for k, v in dict(metadata or {}).items()
             if k.startswith("browser_") and k not in {CONNECTOR_DOMAINS_FLAG, CONNECTOR_CONTROLS_FLAG}}
    held = _delegations.get(binding.key())
    if held and str(held.get("browser_delegated_at", "")) >= str(flags.get("browser_delegated_at", "")):
        flags = {**flags, **held}
    # Q05 B2: a connector step's own sites narrow what its browser may reach (below the order's delegation, never above).
    connector_domains = connector_domains_of(metadata)
    if connector_domains is not None:
        flags[CONNECTOR_DOMAINS_FLAG] = connector_domains
    # P2 护栏一: and the buttons it declared, when it declared any.
    connector_controls = connector_controls_of(metadata)
    if connector_controls is not None:
        flags[CONNECTOR_CONTROLS_FLAG] = connector_controls
    flags[ORDER_AREA_FLAG] = _order_area_of(binding, metadata)
    _task_flags[binding.key()] = flags


def _order_area_of(binding: Any, metadata: dict[str, Any] | None) -> Any:
    """The business order and step this task works in, or None (a plain session, a trial, an order id that does not
    name its folder)."""
    from ampuly.core.order_layout import order_area_of

    try:
        return order_area_of(str(getattr(binding, "project_id", "") or ""), dict(metadata or {}))
    except Exception:  # noqa: BLE001 - no area: the task-id folder, as before
        return None


def narrowed_hosts(delegated: Any, connector: Any) -> tuple[str, ...]:
    """What is in both lists: the narrower entry of each pair that overlaps.

    An entry may be a whole site or an address on one (P2 护栏一), so the narrower side has to survive: given
    ``example.com`` delegated and ``example.com/creator`` named by the step, the step gets ``example.com/creator``
    and not the whole site back. Entries that overlap nothing drop out, and two lists that share nothing give
    nothing -- which is how a connector step reaches no site at all rather than every site.
    """
    from ampuly.computer_use.web_scope import entry_covers

    left = [str(d).strip().lower().lstrip(".") for d in list(delegated or []) if str(d).strip()]
    right = [str(d).strip().lower().lstrip(".") for d in list(connector or []) if str(d).strip()]
    out = ({c for c in right if any(entry_covers(d, c) for d in left)}
           | {d for d in left if any(entry_covers(c, d) for c in right)})
    return tuple(sorted(out))


def _task_flag(binding: Any, name: str) -> Any:
    return _task_flags.get(binding.key(), {}).get(name)


def task_flag(binding: Any, name: str) -> Any:
    return _task_flag(binding, name)


def _artifact_dir_for(binding: Any) -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "browser" / "artifacts" / binding.key()


def _download_dir_for(binding: Any, metadata: dict[str, Any] | None = None) -> Path:
    """The order-owned destination for newly admitted downloads.

    The project workplace is already the deliverable source of truth.  Putting
    inspected downloads under the bound order makes them ordinary scoped
    artifacts without a second artifact table or a later copy step. A task in
    a business order saves into its step's own folder,
    ``orders/<order>/steps/<step>/downloads`` (data phase 1), which the step
    reads as ``../steps/<step>/downloads``; any other task into its task-id
    folder, as before. ``metadata`` (the task's) decides when given, otherwise
    the flags of the binding's last call.
    """
    area = _order_area_of(binding, metadata) if metadata is not None else _task_flag(binding, ORDER_AREA_FLAG)
    if area is not None:
        return area.downloads
    return _legacy_task_download_dir_for(binding)


def _legacy_task_download_dir_for(binding: Any) -> Path:
    """Where a task in no business order saves its downloads (and where every task did before data phase 1)."""
    from ampuly.core.config import get_project_workplace
    from ampuly.project_id import is_valid_project_id

    project_id = str(getattr(binding, "project_id", "") or "").strip()
    task_id = str(getattr(binding, "task_id", "") or "").strip()
    if not project_id or not is_valid_project_id(project_id):
        raise ValueError("download requires a valid project binding")
    if (
        not task_id or task_id in {".", ".."} or task_id.rstrip(" .") != task_id
        or any(mark in task_id for mark in ("/", "\\", ":", "\x00"))
    ):
        raise ValueError("download requires a valid task binding")
    return get_project_workplace(project_id) / "orders" / task_id / "browser-downloads"


def order_downloads(binding: Any) -> list[Path]:
    """Q05 B5: the files the browser saved for this work item, oldest first: its step's folder and the task-id folder
    it used before data phase 1. Their names are the product's own (download-<sha256 prefix>.<type>, C27): a page's
    suggested filename never reaches here. Empty when none or unbound."""
    directories: list[Path] = []
    for pick in (lambda: _download_dir_for(binding), lambda: _legacy_task_download_dir_for(binding)):
        try:
            directory = pick()
        except (ValueError, OSError):
            continue
        if directory not in directories:
            directories.append(directory)
    found: list[tuple[float, str, Path]] = []
    for directory in directories:
        try:
            if not directory.is_dir() or directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                continue
            items = list(directory.iterdir())
        except OSError:
            continue
        for item in items:
            try:
                if item.name.startswith("download-") and item.is_file() and not item.is_symlink():
                    found.append((item.stat().st_mtime, item.name, item))
            except OSError:
                continue
    return [path for _, _, path in sorted(found)]


DOWNLOAD_SAVED_EVENT = "download_saved"


def _note_download_saved(session_key: str, name: str) -> None:
    """Journal that this binding's browser published a download under its product name (download-<digest>.<type>).

    An order step's download folder is shared by every task of that step, so the file alone does not say whose it is;
    this event does, durably (data phase 1 review). Never raises into the download handler."""
    try:
        gateway().annotate(session_key, {"type": DOWNLOAD_SAVED_EVENT, "name": str(name or "")[:120]})
    except Exception:  # noqa: BLE001
        pass


def _downloads_saved_by(binding: Any) -> set[str]:
    try:
        return {str(event.get("name", "") or "") for event in events_for(binding)
                if event.get("type") == DOWNLOAD_SAVED_EVENT and event.get("name")}
    except Exception:  # noqa: BLE001 - nothing known: nothing from a shared folder is attributed
        return set()


def _legacy_download_dir_for(binding: Any) -> Path:
    """Read-only compatibility for downloads produced before v4.5 P1-03."""
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "browser" / "downloads" / binding.key()


_ARTIFACT_LIST_CAP = 50


def session_artifacts(binding: Any, task_metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The product-owned artifacts a session produced, for the audit view: screenshots by name (the
    name is product-generated, and the console already shows the latest one) and downloads by time
    only -- a download's suggested filename is page-controlled, so it never reaches the view (C27).
    Bounded to the most recent, scoped to this binding's own directories (keyed by binding.key(), so
    one session cannot list another's). ``task_metadata`` names the order step's folder after a
    restart, when the in-memory flags are gone. That folder is shared by every task of the step, so from it only
    the files this binding's browser published are listed (its journaled ``download_saved`` events)."""
    out: list[dict[str, Any]] = []
    art_dir = _artifact_dir_for(binding)
    if art_dir.is_dir():
        for item in sorted(art_dir.glob("*.png")):
            try:
                out.append({"kind": "screenshot", "name": item.name, "at": item.stat().st_mtime})
            except OSError:
                continue
    download_dirs = [_legacy_download_dir_for(binding)]
    own_dirs: set[Path] = set(download_dirs)
    try:
        own_dirs.add(_legacy_task_download_dir_for(binding))
    except ValueError:
        pass
    for pick in (lambda: _legacy_task_download_dir_for(binding),
                 lambda: _download_dir_for(binding, task_metadata) if task_metadata is not None else _download_dir_for(binding)):
        try:
            directory = pick()
        except ValueError:
            # An old/unbound session may still have audit history, but it can
            # never receive a new download into a business deliverable space.
            continue
        if directory not in download_dirs:
            download_dirs.append(directory)
    published: set[str] | None = None
    for dl_dir in download_dirs:
        shared = dl_dir not in own_dirs
        if shared and published is None:
            published = _downloads_saved_by(binding)
        if dl_dir.is_dir():
            for item in sorted(dl_dir.iterdir()):
                try:
                    if shared and item.name not in (published or set()):
                        continue
                    if item.is_file():
                        out.append({"kind": "download", "at": item.stat().st_mtime})
                except OSError:
                    continue
    out.sort(key=lambda a: a.get("at") or 0.0)
    return out[-_ARTIFACT_LIST_CAP:]


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    if not hosts:
        return False
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


@dataclass(frozen=True)
class BrowserAdmission(Admission):
    """The original registry control incarnation, never refreshed at dispatch."""

    control_fence: str | None = None


def _check_control_fence(registry: Any, binding: Any, captured: str | None) -> None:
    view = registry.lease_view(binding)
    state = UNAVAILABLE_TO_LEASE.get(view.get("unavailable", "")) or STATE_TO_LEASE.get(view.get("state", ""), LeaseState.CLOSED)
    if view.get("control_fence") != captured or state not in AGENT_MAY_ACT:
        raise GatewayRefused("browser_control_changed", "browser control changed while preparing the action; observe again")


def _bind_control_fence(admission: Admission, captured: str | None) -> BrowserAdmission:
    return BrowserAdmission(admission.action, admission.observation_hash, admission.effect_claim,
                            admission.admitted_at, captured)


async def _refresh_account_before_observation(registry: Any, binding: Any, runtime: Any) -> None:
    refresh = getattr(registry, "refresh_account_login", None)
    try:
        if callable(refresh):
            await refresh(binding, runtime)
    except Exception as exc:
        raise GatewayRefused(str(getattr(exc, "reason", "") or "account_unavailable")) from None


def _check_account_write(registry: Any, binding: Any, risk: ActionClass) -> None:
    check_write = getattr(registry, "check_account_write", None)
    try:
        if risk is not ActionClass.READ_ONLY and callable(check_write):
            check_write(binding)
    except Exception as exc:
        raise GatewayRefused(str(getattr(exc, "reason", "") or "account_unavailable")) from None


async def admit(registry: Any, binding: Any, runtime: Any, *, tool: str, target: dict[str, Any],
                egress_hosts: tuple[str, ...] = (), kind: str = "", effect_claim: str = "",
                resolve_kind: Callable[[], Any] | None = None, verification_sources: tuple[str, ...] = ()) -> Admission:
    """Observe the page as it is, then submit this tool call as a classified action.

    `kind` overrides the tool's default classification when the caller knows
    more about the target (a click on a submit button is a submit). `resolve_kind`
    reads the live target for that, after the page is observed and found to be the
    one the model decided on; a refusal or a target it cannot read is journaled.
    """
    lease = sync_lease(registry, binding)
    captured_fence = registry.lease_view(binding).get("control_fence")
    session_id = binding.key()
    _apply_runtime_policies(binding, runtime, lease)
    await _refresh_account_before_observation(registry, binding, runtime)
    _check_control_fence(registry, binding, captured_fence)
    url, title = await _page_facts(runtime)
    _check_control_fence(registry, binding, captured_fence)
    # C05: an action on an element was decided on the last structured snapshot.
    # The page is read again now; the observation carries what is there, and if
    # that is not what the model saw, nothing is sent.
    on_element = "ref" in target or "selector" in target
    current = await _current_structure(runtime) if on_element else ""
    _check_control_fence(registry, binding, captured_fence)
    observation_hash, sequence = _observe(binding, runtime, lease, url, title, structure=current)
    kind = kind or TOOL_KINDS[tool]
    decided_on = str(getattr(runtime, "last_snapshot_digest", "") or "")
    if on_element and decided_on and current and current != decided_on:
        gateway().annotate(session_id, {"type": "refused", "reason": "page_changed_since_snapshot", "kind": kind, "sequence": sequence,
                                        "decided_on": decided_on[:16], "observed": current[:16]})
        raise GatewayRefused("page_changed_since_snapshot",
                             "the page is not the one the last browser_snapshot showed; take a new snapshot before acting")
    if resolve_kind is not None:
        try:
            kind = str(await resolve_kind() or kind)
            _check_control_fence(registry, binding, captured_fence)
        except GatewayRefused as exc:
            gateway().annotate(session_id, {"type": "refused", "reason": exc.reason, "kind": kind, "sequence": sequence})
            raise
        except Exception as exc:
            # The target is gone, its frame is gone, the selector is invalid: nothing was sent.
            gateway().annotate(session_id, {"type": "refused", "reason": "target_not_resolved", "kind": kind, "sequence": sequence,
                                            "error": type(exc).__name__})
            raise
    risk = ACTION_KINDS[kind]
    _check_account_write(registry, binding, risk)
    payload = {
        "action_id": f"{session_id}:{_process_nonce}:{sequence}",
        "attempt_id": f"{session_id}:{_process_nonce}:{sequence}:1", "kind": kind,
        "scope_digest": lease.scope_digest, "control_lease_id": lease.lease_id, "observation_hash": observation_hash,
        "target": dict(target), "expected_effect": f"{tool} on the current page", "risk": risk.value,
        "timeout_sec": 30, "idempotent": risk is ActionClass.READ_ONLY, "retryable": False,
        "egress_hosts": tuple(h.lower() for h in egress_hosts if h),
    }
    if risk is ActionClass.EXTERNAL_EFFECT:
        # The plan names the sources the approved expectation will read (§5.4); with nothing machine-checkable
        # stated, a person decides whether it worked. The Effect claim is what no tool-face caller can supply.
        payload["verification"] = {"sources": list(dict.fromkeys(s for s in verification_sources if s)) or ["human"]}
    _check_control_fence(registry, binding, captured_fence)
    return _bind_control_fence(gateway().submit(session_id, payload, effect_claim=effect_claim), captured_fence)


def _apply_runtime_policies(binding: Any, runtime: Any, lease: Any) -> None:
    """What the runtime must enforce before this action, read from the task each time, never from an argument."""
    # Preflight the complete policy contract before invoking any setter or
    # observing an account/page. A leaf selector must not silently discard the
    # protections BrowserRuntime implements. Write-fence timing stays with the
    # lookup/decider callers; its methods must nevertheless exist at admission.
    try:
        hooks = require_browser_runtime_policy(runtime)
    except BrowserRuntimePolicyRefused as exc:
        raise GatewayRefused(exc.reason, exc.method) from None

    def apply(name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return hooks.apply(name, *args, **kwargs)
        except BrowserRuntimePolicyRefused as exc:
            raise GatewayRefused(exc.reason, exc.method) from None

    # C26: the same allow-list the registry checks at navigate time is enforced
    # by the runtime on every request the page makes (redirects, iframes,
    # subresources, WebSockets), not only on the address bar.
    # P2 护栏一: control is back with the agent, so the page rules bind again. Cleared on every action rather
    # than when the hand-off ends, so no path can leave the person's exemption switched on behind the agent.
    apply("set_person_driving", False)
    connector_domains = _task_flag(binding, CONNECTOR_DOMAINS_FLAG)
    # Q05 B2: a connector step reaches only the sites it names that the order also delegated (none: nothing).
    apply("set_egress_hosts", narrowed_hosts(lease.allowed_hosts, connector_domains)
          if connector_domains is not None else lease.allowed_hosts)
    # C18 / C29: downloads only when the task says so, only into the product's
    # own directory for this binding. Read from the task each time, never from
    # a tool argument.
    # C06: a session the owner may hand to a person is launched on a screen (browser_visible from the task).
    visibility = getattr(runtime, "set_visible", None)
    if callable(visibility):
        visibility(bool(_task_flag(binding, "browser_visible")))
    output_root = getattr(runtime, "set_output_root", None)
    if callable(output_root):
        output_root(_artifact_dir_for(binding))
    listener = getattr(runtime, "set_download_listener", None)
    if callable(listener):
        key = binding.key()
        listener(lambda name, _key=key: _note_download_saved(_key, name))
    allowed = bool(_task_flag(binding, "browser_allow_downloads"))
    if allowed:
        from ampuly.core.config import get_project_workplace

        download_dir = _download_dir_for(binding)
        download_root = get_project_workplace(str(getattr(binding, "project_id", "") or ""))
    else:
        download_dir = None
        download_root = None
    apply("set_download_policy", allowed=allowed, directory=download_dir, root=download_root,
          max_bytes=_task_flag(binding, "browser_download_max_bytes"),
          types=_task_flag(binding, "browser_download_types"))


async def _current_structure(runtime: Any) -> str:
    """The page's structure digest right now, or "" when the runtime cannot say (no page yet)."""
    reader = getattr(runtime, "structure_digest", None)
    if not callable(reader):
        return ""
    try:
        return str(await reader() or "")
    except Exception:  # noqa: BLE001 - a page that cannot be read is refused by the action itself
        return ""


def _observe(binding: Any, runtime: Any, lease: Any, url: str, title: str, *, geometry: Geometry | None = None,
             structure: str = "") -> tuple[str, int]:
    """The page as it is, hashed: what the next action binds to. Returns (observation_hash, sequence)."""
    session_id = binding.key()
    sequence = _sequences.get(session_id, 0) + 1
    _sequences[session_id] = sequence
    controller = "agent" if lease.state in AGENT_MAY_ACT else ("human" if lease.state in HUMAN_STATES else "none")
    observation = Observation(
        session_id=session_id, task_id=str(getattr(binding, "task_id", "") or ""), scope_digest=lease.scope_digest,
        sequence=sequence, url=url, title=title, domain_allowed=_host_allowed(url, lease.allowed_hosts),
        snapshot_hash=structure or str(getattr(runtime, "last_snapshot_digest", "") or "")
        or hashlib.sha256(f"{url}\n{title}".encode()).hexdigest(),
        controller=controller,
        lease_id=lease.lease_id, verification_state="none", produced_at=gateway()._clock(),
        redacted=bool((getattr(runtime, "last_screenshot", None) or {}).get("redacted", True)),
        screenshot_ref=str((getattr(runtime, "last_screenshot", None) or {}).get("path", "") or ""),
        geometry=geometry,
    )
    return gateway().observe(observation), sequence


async def admit_vision(registry: Any, binding: Any, runtime: Any, task: Any, *, instruction: str) -> tuple[Admission | None, Any]:
    """A masked screenshot to the vision model; the action it names admitted at the Gateway (X03 fourth slice).

    The observation carries the viewport geometry the coordinates are measured
    against, so a page whose size changed before the act is refused (C30).
    Returns (admission, grounding). `admission` is None when the model asked
    for verification (`finished`) or for the user (`call_user`): nothing to act.
    """
    from ampuly.computer_use.vision_grounding import GroundingRefused, ground

    provider = vision_provider(task)
    if provider is None:
        raise GatewayRefused("no_vision_provider", "no vision model is bound for this product")
    lease = sync_lease(registry, binding)
    captured_fence = registry.lease_view(binding).get("control_fence")
    session_id = binding.key()
    _apply_runtime_policies(binding, runtime, lease)
    await _refresh_account_before_observation(registry, binding, runtime)
    _check_control_fence(registry, binding, captured_fence)
    screenshot = dict(getattr(runtime, "last_screenshot", None) or {})
    if not screenshot.get("redacted") or not screenshot.get("path"):
        raise GatewayRefused("no_observation", "take a masked screenshot before acting by sight")
    decided_on = dict(screenshot.get("page_fingerprint") or {})
    reader = getattr(runtime, "observation_fingerprint", None)
    if not decided_on.get("structure") or not callable(reader):
        raise GatewayRefused("no_observation_fingerprint", "the screenshot carries no page fingerprint to act against; take a new screenshot")
    geometry_of = getattr(runtime, "page_geometry", None)
    facts = geometry_of() if callable(geometry_of) else None
    if not facts:
        raise GatewayRefused("geometry_unknown", "the page's viewport size is not known; a coordinate cannot be bound")
    geometry = Geometry(window_id=session_id, display_id="page", dpi_scale=float(facts.get("dpi_scale", 1.0) or 1.0),
                        width=int(facts["width"]), height=int(facts["height"]), expires_at=gateway()._clock() + COORDINATE_TTL_SEC)
    url, title = await _page_facts(runtime)
    _check_control_fence(registry, binding, captured_fence)
    # X03: the observation binds to the page as the picture showed it, not as it may be by now.
    observation_hash, sequence = _observe(binding, runtime, lease, url, title, geometry=geometry, structure=str(decided_on["structure"]))
    try:
        grounding = await ground(provider, screenshot=screenshot, instruction=instruction, screen=(geometry.width, geometry.height),
                                 geometry=geometry, task=task, action_id=f"{session_id}:{_process_nonce}:{sequence}",
                                 attempt_id=f"{session_id}:{_process_nonce}:{sequence}:1",
                                 scope_digest=lease.scope_digest, control_lease_id=lease.lease_id, observation_hash=observation_hash)
    except GroundingRefused as exc:
        gateway().annotate(session_id, {"type": "grounding_refused", "reason": exc.reason, "sequence": sequence})
        raise GatewayRefused(exc.reason, str(exc)[:160]) from None
    _check_control_fence(registry, binding, captured_fence)
    if grounding.action is None:
        gateway().annotate(session_id, {"type": "grounding", "outcome": "finished" if grounding.finished else "hand_off",
                                        "model": grounding.model, "sequence": sequence})
        return None, grounding
    # X03 / C30: the model took seconds to look. The page is read again now; a coordinate is only good for
    # the page it was measured on, so an address, structure or scroll position that moved refuses the act.
    try:
        current = dict(await reader() or {})
    except Exception:  # noqa: BLE001 - a page that cannot be read now cannot be acted on by sight
        current = {}
    _check_control_fence(registry, binding, captured_fence)
    changed = sorted(key for key in ("url", "structure", "scroll") if decided_on.get(key) != current.get(key)) if current else ["unreadable"]
    if changed:
        gateway().annotate(session_id, {"type": "refused", "reason": "page_changed_since_screenshot", "changed": changed,
                                        "sequence": sequence, "model": grounding.model})
        raise GatewayRefused("page_changed_since_screenshot",
                             f"the page changed while the model looked ({', '.join(changed)}); take a new screenshot before acting by sight")
    # X03 / C30 / X06: a click is a geometric target, and geometry the fingerprint cannot see (a control slid
    # under the coordinate, an overlay drawn over it) is caught here -- what actually sits under the point is
    # read now and classified. A submit-like control by sight never runs unapproved: the model must reach it
    # by ref (browser_click), which routes a submit to the owner. Vision is a fallback for pages with no
    # structure to act on, so refusing an external-effect vision click costs almost nothing.
    vision = grounding.vision
    function = str(getattr(vision, "function", "") or "")
    if function in _VISION_CLICK_FUNCTIONS and getattr(vision, "start", None):
        hit_test = getattr(runtime, "element_at", None)
        # A drag lets go at its end point: a slide-to-confirm is judged where it ends as well as where it starts.
        points = [vision.start] + ([vision.end] if function == "drag" and getattr(vision, "end", None) else [])
        for point in points if callable(hit_test) else []:
            try:
                under = await hit_test(float(point[0]), float(point[1]))
            except Exception:  # noqa: BLE001 - a point that cannot be read is not one to click by sight
                under = None
            _check_control_fence(registry, binding, captured_fence)
            if under is None:
                gateway().annotate(session_id, {"type": "refused", "reason": "vision_click_hits_nothing", "sequence": sequence})
                raise GatewayRefused("vision_click_hits_nothing", "nothing is under that coordinate to click; take a fresh screenshot")
            if under.get("frame") or under.get("opaque") or str(under.get("tag", "") or "").lower() in {"iframe", "frame", "object", "embed"}:
                gateway().annotate(session_id, {"type": "refused", "reason": "vision_click_into_frame", "sequence": sequence})
                raise GatewayRefused("vision_click_into_frame",
                                     "the point is inside an embedded frame or widget whose control cannot be classified; take a "
                                     "browser_snapshot and act by ref, or hand the page to a person")
            actionable = (str(under.get("tag", "") or "").lower() in {"button", "a", "summary", "select"}
                          or str(under.get("role", "") or "").lower() in {"button", "link"}
                          or (under.get("clickable") is True and under.get("editable") is not True))
            # A canvas or a video is described as itself, never as clickable: C03 allows sight on pages without structure.
            # A wordless icon may have borrowed the surrounding row's words so
            # the carrier can be classified.  Those words do not name the
            # coordinate the model chose: sight must still refuse the original
            # hit unless that hit itself has an accessible name.
            name_keys = ("hit_text", "hit_label", "hit_names") if under.get("carried") is True else ("text", "label", "names")
            if actionable and not any(str(under.get(key, "") or "").strip() for key in name_keys):
                # An icon-only control nothing names cannot be shown not to delete or pay.
                gateway().annotate(session_id, {"type": "refused", "reason": "vision_click_unlabelled_control", "sequence": sequence})
                raise GatewayRefused("vision_click_unlabelled_control",
                                     "the control under that coordinate has no text or label to judge it by; act on it by ref "
                                     "(browser_click), or hand the page to a person")
            declared = _task_flags.get(session_id, {}).get(CONNECTOR_CONTROLS_FLAG)
            if declared is not None and control_refusal(under, declared):
                # P2 护栏一: sight refuses submits and unlabelled controls, but a LABELLED, non-submit control
                # (下一页, 编辑, 导出) passes both -- so a step that declared its buttons could still reach an
                # undeclared one by coordinate. The declaration binds every way of pressing a control.
                gateway().annotate(session_id, {"type": "refused", "reason": "undeclared_control", "sequence": sequence})
                raise GatewayRefused("undeclared_control",
                                     "this step may only click the buttons its template declares; act on a declared "
                                     "one by ref (browser_click), or hand the page to a person")
            if classify_click(under) == "submit":
                # The reason only: what sits under the point may be a field someone typed into (C27).
                gateway().annotate(session_id, {"type": "refused", "reason": "vision_click_on_external_effect", "sequence": sequence})
                raise GatewayRefused("vision_click_on_external_effect",
                                     "a submit/pay/delete control is under that coordinate; act on it by ref (browser_click), which asks the owner")
    if function in {"type", "hotkey"}:
        from ampuly.computer_use.vision_actions import keystroke_needs_text_focus

        inputs = dict(getattr(vision, "inputs", {}) or {})
        if str(dict(grounding.action or {}).get("kind", "") or "") == "submit":
            # C08: Enter, Space, a send chord or a typed line break commits; by sight nothing commits unapproved.
            gateway().annotate(session_id, {"type": "refused", "reason": "vision_key_on_external_effect", "sequence": sequence,
                                            "function": function})
            raise GatewayRefused("vision_key_on_external_effect",
                                 "Enter, Space, send shortcuts and line breaks commit; type the draft by sight, then submit by ref "
                                 "with browser_click, which asks the owner")
        if keystroke_needs_text_focus(function, inputs):
            reader = getattr(runtime, "focused_element", None)
            try:
                focused = await reader() if callable(reader) else None
            except Exception:  # noqa: BLE001 - a focus that cannot be read is not one to type into
                focused = None
            _check_control_fence(registry, binding, captured_fence)
            if not focused or not focused.get("editable") or focused.get("opaque"):
                gateway().annotate(session_id, {"type": "refused", "reason": "vision_keys_without_text_focus", "sequence": sequence,
                                                "function": function})
                raise GatewayRefused("vision_keys_without_text_focus",
                                     "the keyboard focus is not a text field this page can read (keys there can press buttons or fire "
                                     "shortcuts); click the field first, or act by ref")
    _check_account_write(registry, binding, ACTION_KINDS[grounding.action["kind"]])
    _check_control_fence(registry, binding, captured_fence)
    admission = _bind_control_fence(gateway().submit(session_id, grounding.action), captured_fence)
    gateway().annotate(session_id, {"type": "grounding", "outcome": "admitted", "function": grounding.vision.function,
                                    "model": grounding.model, "sequence": sequence})
    return admission, grounding


def destination_of(element: dict[str, Any]) -> str:
    """Where a click on this control sends, as an approval can bind it: scheme, host, port, path, and the names of the
    query's keys. The values are left out, so a nonce regenerated on every render does not ask the owner again, while a
    different order id in the path always does (C09)."""
    raw = str(element.get("form_action", "") or element.get("href", "") or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw[:512]  # not a URL the browser resolved (a javascript: or an odd scheme): bound as written
    keys = ",".join(sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)}))
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{port}{parts.path}"[:480] + (f"?{keys}"[:32] if keys else "")


def _host_path(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return f"{parts.hostname or ''}{parts.path or ''}"


# Keyed per process: the stored binding cannot be turned back into a password by guessing. After a restart it no longer
# matches, so an approval from before the restart is refused at apply (the page it bound to is gone anyway).
_FORM_KEY = os.urandom(32)


def form_state_digest(fields: Any) -> str:
    """One keyed digest of what a form would send, in document order, every value in full (sensitive ones included).
    A challenge token that refreshes itself (Turnstile, reCAPTCHA) is left out; any other hidden field counts."""
    from ampuly.layer4_tools.browser import form_state_of

    state = form_state_of(fields)
    pairs = [
        (index, str(f.get("name", "")), hashlib.sha256(str(f.get("value", "")).encode("utf-8")).hexdigest(), len(str(f.get("value", ""))))
        for index, f in enumerate(state["fields"]) if f.get("volatile") is not True
    ]
    if not pairs:
        return ""
    return hmac.new(_FORM_KEY, json.dumps(pairs, ensure_ascii=False).encode("utf-8"), hashlib.sha256).hexdigest()


async def observe_before_proposal(registry: Any, binding: Any, runtime: Any, target: dict[str, Any]) -> int:
    """The start of admit for a click that may become a proposal: lease synced, page observed, C05 held. Returns the sequence."""
    lease = sync_lease(registry, binding)
    session_id = binding.key()
    _apply_runtime_policies(binding, runtime, lease)
    url, title = await _page_facts(runtime)
    current = await _current_structure(runtime)
    observation_hash, sequence = _observe(binding, runtime, lease, url, title, structure=current)
    if not lease.agent_may_act(gateway()._clock()):
        gateway().annotate(session_id, {"type": "refused", "reason": "agent_not_in_control", "kind": "submit", "sequence": sequence})
        raise GatewayRefused("agent_not_in_control", "the agent does not hold this page now")
    decided_on = str(getattr(runtime, "last_snapshot_digest", "") or "")
    if decided_on and current and current != decided_on:
        gateway().annotate(session_id, {"type": "refused", "reason": "page_changed_since_snapshot", "kind": "select_candidate",
                                        "sequence": sequence, "decided_on": decided_on[:16], "observed": current[:16]})
        raise GatewayRefused("page_changed_since_snapshot",
                             "the page is not the one the last browser_snapshot showed; take a new snapshot before acting")
    return sequence


async def propose_submit(registry: Any, binding: Any, runtime: Any, task: Any, *, selector: str, element: dict[str, Any] | None,
                         expect: dict[str, Any] | None = None, operation: str = "click") -> dict[str, Any]:
    """Put a submit click or Enter key into the owner's approval inbox instead of sending it (C08, C09).

    The proposal binds to the last structured snapshot's digest; the provider
    re-observes the page at apply time and refuses to submit a page that has
    changed since. The tool result tells the model to wait for the decision.
    """
    if operation not in {"click", "press_enter"}:
        raise GatewayRefused("submit_operation_unsupported", f"unsupported browser submit operation: {operation}")
    if lookup_step(task):
        # Bulk P1 (L3): a lookup step reads a site; it never submits, not even with the owner's approval.
        raise GatewayRefused("lookup_step_does_not_submit",
                             "this step is a lookup: it only reads the site; a submit, send, publish or delete is not its to propose")
    proposer = effect_proposer(task)
    if proposer is None:
        raise GatewayRefused("external_effect_not_authorized", "no approval path is bound for browser submits")
    digest = str(getattr(runtime, "last_snapshot_digest", "") or "")
    if not digest:
        raise GatewayRefused("no_observation", "take a browser_snapshot before submitting")
    url, _title = await _page_facts(runtime)
    page = _host_path(url)
    live = dict(element or {})
    # What is hashed into the proposal stays fixed for this page state: the snapshot's row when there is one, else the
    # live tag and type. The live text (a countdown, a price) goes to the owner's prompt and preview only, so a retry on
    # the same snapshot is the same proposal, not a conflicting one.
    lookup = getattr(runtime, "element_for", None)
    row = lookup(selector) if callable(lookup) else None
    stable = row if row else {"tag": live.get("tag", ""), "type": live.get("type", "")}
    element = {k: str((stable or {}).get(k, "") or "")[:120] for k in ("tag", "type", "text", "ref", "selector")}
    frame_reader = getattr(runtime, "ref_frame_url", None)
    if not callable(frame_reader):
        raise GatewayRefused("browser_control_facts_unavailable", "the browser runtime cannot identify the target's frame")
    frame_url = frame_reader(selector)
    if not isinstance(frame_url, str) or len(frame_url) > 8192:
        raise GatewayRefused("browser_control_facts_invalid", "the target's frame is unknown; take a new browser_snapshot")
    shown = {**element, "text": str(live.get("text", "") or element.get("text", ""))[:120], "label": str(live.get("label", "") or "")[:120],
             "submits": bool(live.get("submits")), "frame": _host_path(frame_url) if frame_url else ""}
    # A ref names an element of one snapshot; the approval names a concrete
    # target, so the ref is resolved now and the selector is what gets approved.
    resolver = getattr(runtime, "resolve_ref", None)
    concrete = str(resolver(selector) if callable(resolver) else selector)[:256]
    metadata = dict(getattr(task, "metadata", {}) or {})
    ttl_minutes = metadata.get("browser_submit_approval_ttl_minutes")
    try:
        approval_ttl_seconds = min(60.0, max(1.0, float(ttl_minutes))) * 60.0 if ttl_minutes is not None else None
    except (TypeError, ValueError):
        approval_ttl_seconds = None
    key = binding.key()
    counter = getattr(runtime, "target_count", None)
    if callable(counter) and await counter(selector) != 1:
        gateway().annotate(key, {"type": "refused", "reason": "submit_target_ambiguous", "kind": "submit"})
        raise GatewayRefused("submit_target_ambiguous", "this target does not name exactly one element on the page; take a new "
                                                        "browser_snapshot and click the element by its ref")
    # C09: the approval covers what the form would send, not only the button: a value changed after the proposal is
    # not what the owner approved. The owner is shown the values (never a password, a hidden or a card field).
    from ampuly.layer4_tools.browser import form_state_of

    reader = getattr(runtime, "form_state", None)
    state = form_state_of(await reader(selector) if callable(reader) else [])
    if state["overflow"]:
        gateway().annotate(key, {"type": "refused", "reason": "submit_form_unbindable", "kind": "submit"})
        raise GatewayRefused("submit_form_unbindable", "this form is too large to bind an approval to its values; hand the step to a person")
    fields = state["fields"]
    form_hash = form_state_digest(state)
    # The owner sees the fields near the control first (the dialog or form it sits in), with values; never a sensitive one.
    worth = [f for f in fields if f.get("worthy", True) and f.get("volatile") is not True]
    display = sorted(range(len(worth)), key=lambda i: (worth[i].get("near") is not True, i))
    shown["fields"] = [{"name": str(worth[i].get("name", ""))[:80],
                        "value": "（已隐藏）" if worth[i].get("sensitive") else str(worth[i].get("value", ""))[:120]} for i in display[:20]]
    # Hidden fields carry the amount on many checkout forms: they are bound and counted, even though their values are
    # not shown, so "另有 N 项未列出" never reads as "nothing else is being sent".
    withheld = sum(1 for f in fields if f.get("volatile") is not True and not f.get("worthy", True) and str(f.get("value", "") or ""))
    shown["fields_omitted"] = max(0, len(worth) - 20) + withheld
    ref = str(element.get("ref", "") or "")
    # C09: and where it sends. A button whose words stay while its form action moves to another order is not what was approved.
    destination = destination_of(live)
    shown["destination"] = _host_path(destination) if destination else ""
    label = shown.get("text") or shown.get("label") or element.get("selector") or selector
    from ampuly.effects.browser_submit import clean_expectation, page_expectations_held, verification_sources

    expectation = clean_expectation(expect)
    business_event = expectation.get("business_event")
    if isinstance(business_event, dict) and str(business_event.get("source", "") or "") not in verification_sources():
        gateway().annotate(key, {"type": "refused", "reason": "business_event_source_not_registered", "kind": "submit"})
        raise GatewayRefused("business_event_source_not_registered", "the business-event verification source is not registered")
    observer = getattr(runtime, "observation_facts", None)
    before_facts = await observer() if callable(observer) else {"url": url, "text": "", "interactive_elements": []}
    held_before = await page_expectations_held(expectation, before_facts, runtime)
    payload_identity = json.dumps({"element": element, "operation": operation, "expect": expectation, "expect_held_before": held_before},
                                  ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    idempotency_key = hashlib.sha256(
        f"browser-submit|{key}|{digest}|{frame_url}|{concrete}|{ref}|{form_hash}|{destination}|{payload_identity}".encode()).hexdigest()
    # X06 (adversarial review): a submit may carry an API back-check the provider fetches with the session's
    # login after the click. The owner is approving that fetch too, so its host is named in the prompt --
    # not buried inside the expectation dict.
    api_host = ""
    if isinstance(expectation.get("api"), dict):
        api_url = str(expectation["api"].get("url", "") or "")
        api_host = (urlsplit(api_url).hostname or "").lower()
        # A proposal cannot turn an undelegated back-check into a perpetual
        # unknown outcome. The owner must delegate that host first.
        from ampuly.layer4_tools.browser_sessions import BrowserSessionRefused
        try:
            # The host only: this is a request the flow named, not a page the browser is moved to. An order API
            # legitimately lives at /v2/orders/9/payment, and judging it as a page refused the very back-check
            # that proves a submit landed.
            registry.check_host(binding, api_url)
        except BrowserSessionRefused:
            gateway().annotate(key, {"type": "refused", "reason": "api_expectation_host_not_delegated", "kind": "submit"})
            raise GatewayRefused("api_expectation_host_not_delegated", "the API verification host is not delegated for this task") from None
    # A revoke or hand-off can land while the target was read: nothing is published for a page the agent no longer holds.
    view = registry.lease_view(binding)
    held = UNAVAILABLE_TO_LEASE.get(view.get("unavailable", "")) or STATE_TO_LEASE.get(view.get("state", ""), LeaseState.CLOSED)
    if held not in AGENT_MAY_ACT:
        gateway().annotate(key, {"type": "refused", "reason": "agent_not_in_control", "kind": "submit"})
        raise GatewayRefused("agent_not_in_control", "the agent does not hold this page now; nothing was proposed")
    stable_label = element.get("text") or element.get("selector") or concrete
    from ampuly.effects.models import EffectConflictError

    # The engine of this project may have begun shutting down while the page was read: ask again right before filing.
    proposer = effect_proposer(task)
    if proposer is None:
        gateway().annotate(key, {"type": "refused", "reason": "external_effect_not_authorized", "kind": "submit"})
        raise GatewayRefused("external_effect_not_authorized", "no approval path is bound for this project's browser submits")
    publication = None
    for _generation in range(8):
        try:
            from ampuly.computer_use.local_browser_consent import source_binding
            publication = await proposer(
                provider="browser:submit", kind="browser.submit",
                payload={"binding_key": key, "selector": concrete, "observation_hash": digest, "element": element, "page": page,
                         **source_binding(url),
                         "operation": operation,
                         "frame_url": frame_url, "form_hash": form_hash, "destination": destination,
                         "expected_effect": f"{'press Enter on' if operation == 'press_enter' else 'click'} {stable_label} on {page}", "expect": expectation,
                         "expect_held_before": held_before},
                idempotency_key=idempotency_key,
                prompt=f"在 {page} 上{'对「' + str(label) + '」按 Enter' if operation == 'press_enter' else '点击「' + str(label) + '」'}——这是一次提交类外部动作，页面状态 {digest[:12]}"
                       + (f"（位于内嵌页面 {_host_path(frame_url)}）" if frame_url else "")
                       + (f"；控件说明：{shown['label']}" if shown["label"] and shown["label"] != shown["text"] else "")
                       + (f"；将提交到 {shown['destination']}" if shown["destination"] and shown["destination"] != page else "")
                       + ("；将提交的表单内容：" + "，".join(f"{f['name']}={f['value']}" for f in shown["fields"])
                          + (f"；另有 {shown['fields_omitted']} 项未列出" if shown["fields_omitted"] else "")
                          if shown["fields"] else "")
                       + (f"；点击后系统会用本会话登录态读取 {api_host} 核对结果" if api_host else "")
                       + (f"；预期结果：{expectation}" if expectation else "；未声明预期结果")
                       + (f"；其中 {', '.join(held_before)} 在点击前已成立，不能作为完成证据" if held_before else ""),
                preview={"page": page, "element": shown, "observation_hash": digest[:16], "expect": expectation,
                         "expect_held_before": held_before},
                run_id=str(metadata.get("run_id", "") or ""), customer_id=str(metadata.get("customer_id", "") or ""),
                environment_id=str(metadata.get("environment_id", "") or ""),
                task_id=str(getattr(task, "id", "") or "") or None,
                session_id=str(getattr(task, "session_id", "") or "") or None,
                causation_id=_replay_runs.get(key, ""),
                approval_ttl_seconds=approval_ttl_seconds,
            )
        except EffectConflictError:
            gateway().annotate(key, {"type": "refused", "reason": "submit_already_proposed", "kind": "submit"})
            raise GatewayRefused("submit_already_proposed", "this submit on this page state conflicts with an existing proposal") from None
        effect = getattr(publication, "effect", None)
        raw_state = getattr(effect, "state", "awaiting_approval")
        state = str(getattr(raw_state, "value", raw_state) or "awaiting_approval")
        last_error = str(getattr(effect, "last_error", "") or "")
        effect_id = str(getattr(effect, "effect_id", "") or "")
        if state == "failed" and last_error.startswith("local_consent_expectation_required:"):
            return {"status": "requires_new_expectation", "action": "submit", "effect_id": effect_id,
                    "note": "Nothing was clicked. State a checkable result that is not already true, then observe and propose again."}
        if state == "failed" and last_error.startswith("target no longer on the page: target_not_actionable:"):
            return {"status": "requires_page_recovery", "action": "submit", "effect_id": effect_id,
                    "note": "No input was sent. Observe the page, dismiss any covering dialog or wait until the control is ready, "
                            "then take a fresh snapshot before proposing a new action. This did not complete the transaction."}
        if state in {"proposed", "awaiting_approval"}:
            break
        if state == "rejected" and not last_error.startswith("approval_expired"):
            return {"status": "denied", "action": "submit", "effect_id": effect_id,
                    "note": "the owner declined this exact submit on this page state; do not perform it again"}
        if state in {"applied"}:
            return {"status": "already_applied", "action": "submit", "effect_id": effect_id,
                    "note": "this exact submit was already performed; do not perform it again"}
        if state in {"claimed", "outcome_unknown"}:
            return {"status": "outcome_unknown", "action": "submit", "effect_id": effect_id,
                    "note": "verify with a look-back; do not perform it again"}
        if state in {"rejected", "failed"}:
            idempotency_key = hashlib.sha256(f"{idempotency_key}|after:{effect_id}".encode()).hexdigest()
            continue
        raise GatewayRefused("submit_effect_state_unknown", f"cannot interpret submit effect state {state}")
    else:
        raise GatewayRefused("reproposal_limit", "too many terminal approvals exist for this exact submit")
    assert publication is not None
    effect_id = str(getattr(getattr(publication, "effect", None), "effect_id", "") or "")
    checkpoint_id = str(getattr(getattr(publication, "checkpoint", None), "checkpoint_id", "") or "")
    sync_lease(registry, binding)
    gateway().annotate(key, {"type": "proposed", "kind": "submit", "effect_id": effect_id, "checkpoint_id": checkpoint_id,
                             "target": {"ref": element.get("ref", "")} if element.get("ref") else {"selector": selector[:256]}})
    if _recorder is not None and _recorder.recording(binding):
        # The demo shows "then submit": the step is recorded as the proposal, bound to the page it was
        # decided on; what the page becomes is known only after approval, so the after hash is empty
        # and a replay pauses at this step for the owner (X07).
        from types import SimpleNamespace

        _recorder.note(binding, action=SimpleNamespace(kind="submit", observation_hash=digest,
                                                       target={"ref": element["ref"]} if element.get("ref") else {"selector": concrete}),
                       before_hash=digest, after_hash="")
    return {
        "status": "awaiting_approval", "action": "submit", "effect_id": effect_id, "checkpoint_id": checkpoint_id,
        # The model is told which fields were bound, never their values (C27).
        "page": page, "element": {**shown, "fields": [{"name": f["name"]} for f in shown["fields"]]}, "observation_hash": digest[:16],
        "note": "This submit is an external side effect. It was sent to the owner for approval and will be performed "
                "only after approval, and only if the page is still in this state. Do not perform it again.",
    }


def dispatch_guard(registry: Any, binding: Any, admission: Admission) -> Callable[[], bool]:
    """What the runtime asks right before it sends input for this admission (G04 / C06).

    The Gateway's word (still admitted, not cancelled, the lease still the agent's) and the registry's (a person
    may have taken the page through a path that has not synced the Gateway yet) must both say yes.
    """
    session_id = binding.key()
    attempt_id = admission.action.attempt_id
    # The task key/lease id deliberately stay stable. This separate incarnation
    # fence prevents a pre-handoff admission becoming usable after hand-back.
    captured_fence = getattr(admission, "control_fence", None)
    account_intent_recorded = False

    def allowed() -> bool:
        nonlocal account_intent_recorded
        try:
            if not gateway().dispatchable(session_id, attempt_id):
                return False
            view = registry.lease_view(binding)
        except Exception:  # noqa: BLE001 - a guard that cannot answer does not let input through
            return False
        state = UNAVAILABLE_TO_LEASE.get(view.get("unavailable", "")) or STATE_TO_LEASE.get(view.get("state", ""), LeaseState.CLOSED)
        if state not in AGENT_MAY_ACT or view.get("control_fence") != captured_fence:
            return False
        check_write = getattr(registry, "check_account_write", None)
        if admission.action.risk is not ActionClass.READ_ONLY and callable(check_write):
            try:
                check_write(binding)
            except Exception:
                return False
        # Persist the uncertain-write latch BEFORE input. A crash between input
        # and the receipt must not let a later task silently repeat the write.
        marker = getattr(registry, "mark_account_unknown", None)
        if not account_intent_recorded and admission.action.risk is not ActionClass.READ_ONLY and callable(marker):
            try:
                marker(binding, attempt_id)
            except Exception:
                return False
            account_intent_recorded = True
        return True

    return allowed


def settle(binding: Any, admission: Admission, *, ok: bool, evidence: dict[str, Any] | None = None,
           before_effect: bool = False, after_effect: bool = False, after_hash: str = "", before_hash: str = "",
           verification: dict[str, Any] | None = None, observed_after: bool = False,
           registry: Any = None) -> ActionReceipt:
    """The runtime returned (or raised): say what that establishes, no more (§5.3).

    Returning proves the input was accepted, not that it did what was meant. A write is `succeeded` only with a
    `verification` that checked something and held; a read that observed the page afterwards (`observed_after`)
    is complete; an approved submit nobody stated a checkable result for waits for a person; anything else is
    outcome_unknown, input accepted and unverified.

    `after_hash` is the structured digest of the page after the action, when the caller has it; an accepted
    action becomes a step of the demo being recorded for this binding (X07), with that hash as what the step
    should produce.
    """
    action = admission.action
    base = dict(evidence or {})
    verified = verified_outcome(verification) if ok else None
    if not ok and after_effect:
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.FAILED_AFTER_EFFECT,
                                reason="postcondition_not_met", evidence=base)
    elif verified is not None:
        receipt = ActionReceipt(action.action_id, action.attempt_id, verified[0], reason=verified[1],
                                evidence={**base, "verification": dict(verification or {})})
    elif ok and action.risk is ActionClass.READ_ONLY and observed_after:
        # A read that came back with the page it read: the observation itself is what was asked for.
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.SUCCEEDED,
                                evidence={**base, "verification": {"sources": ["element"], "checked": ["observation_after"], "holds": True}})
    elif ok and action.risk is ActionClass.EXTERNAL_EFFECT:
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.AWAITING_HUMAN, reason="no_postcondition_stated", evidence=base)
    elif ok:
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.OUTCOME_UNKNOWN, reason="input_accepted_unverified",
                                evidence={"input_accepted": True, **base})
    elif action.risk is ActionClass.READ_ONLY or before_effect:
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.FAILED_BEFORE_EFFECT, reason="runtime_raised",
                                evidence=dict(evidence or {}))
    else:
        # A raised click or keystroke proves nothing about what the page did with it.
        receipt = ActionReceipt(action.action_id, action.attempt_id, Outcome.OUTCOME_UNKNOWN, reason="runtime_raised",
                                evidence=base)
    recorded = gateway().record(binding.key(), receipt)
    if action.risk is not ActionClass.READ_ONLY and registry is not None:
        if recorded.outcome in {Outcome.SUCCEEDED, Outcome.FAILED_BEFORE_EFFECT}:
            resolver = getattr(registry, "resolve_account_action", None)
            if callable(resolver):
                resolver(binding, action.attempt_id, f"gateway:{action.attempt_id}:{recorded.outcome.value}")
        else:
            marker = getattr(registry, "mark_account_unknown", None)
            if callable(marker):
                marker(binding, action.attempt_id)
    if (ok and _recorder is not None and _recorder.recording(binding)
            and not str(getattr(admission, "effect_claim", "") or "") and recorded.outcome is not Outcome.CANCELLED_UNKNOWN):
        # A demo step is accepted input and the page afterwards -- never a result that arrived after a cancel. An
        # approved submit is already a step, recorded when it was proposed, so its click is not a second one. A step
        # binds the page's structure digests (stable across sessions), not the Gateway observation hash (this session's).
        _recorder.note(binding, action=action, before_hash=before_hash,
                       after_hash=after_hash or str(base.get("observation_after", "") or ""))
    return recorded


def facts_observed(binding: Any, admission: Admission) -> bool:
    """A successful, model-facing observation may clear an unresolved-write latch."""

    return gateway().facts_observed(binding.key(), admission.action.attempt_id)


def demo_start(binding: Any, *, recorded_by: str, label: str = "") -> dict[str, Any]:
    """The owner starts recording this binding's admitted actions into a demo (X07)."""
    import time

    demo_id = f"demo-{binding.key()[:24]}-{int(time.time())}"
    status = recorder().start(binding, demo_id=demo_id, recorded_by=recorded_by, scope_digest=workflow_scope_for(binding), label=label)
    gateway_annotate(binding, {"type": "demo", "state": "recording", "demo_id": demo_id})
    return status


def demo_stop(binding: Any, *, directory: Path | None = None) -> dict[str, Any]:
    """Seal what was recorded and write it to the product's demos directory; a recording without steps is refused."""
    from ampuly.computer_use.demo_recorder import save_demo

    demo = recorder().stop(binding)
    path = save_demo(demo, directory)
    gateway_annotate(binding, {"type": "demo", "state": "sealed", "demo_id": demo.demo_id, "steps": len(demo.steps), "seal": demo.seal[:16]})
    return {"demo_id": demo.demo_id, "steps": len(demo.steps), "seal": demo.seal, "saved_to": str(path),
            "kinds": [step.kind for step in demo.steps]}


def demo_status(binding: Any) -> dict[str, Any]:
    return recorder().status(binding)


def gateway_annotate(binding: Any, event: dict[str, Any]) -> None:
    try:
        gateway().annotate(binding.key(), event)
    except Exception:  # noqa: BLE001 - a session the Gateway has not seen yet has nothing to annotate
        pass


def note_page_opened(binding: Any) -> None:
    """Bulk P1: the browser was MOVED to a page, not merely looked at.

    An ``admitted`` event cannot say this on its own: ``TOOL_KINDS`` maps browser_navigate, browser_snapshot and
    browser_observe all to ``observe``/read-only, so 「打开页面」 and 「看一眼页面」 are indistinguishable there.
    A lookup's claim is 「这个网站此刻对这张单显示了这个答案」, and a read of a page left open since an earlier
    turn does not support it. Wall clock, to match the turn stamp it is compared against.
    """
    gateway_annotate(binding, {"type": "page_opened", "at": time.time()})


def journal_control(binding: Any, *, action: str, actor: str, at: str | float | None = None) -> bool:
    """Durably record one successful owner control action, even after the session was closed.

    Control notes are audit facts, not action admissions: failure to write them
    is reported to the caller but does not replay or undo the control action.
    """
    event = {"type": "console_control", "action": str(action or "")[:80],
             "actor": str(actor or "")[:120]}
    if at is not None:
        event["at"] = at
    current = gateway()
    try:
        return bool(current.annotate(binding.key(), event))
    except Exception:  # the registry may already have closed/removed the live Gateway session
        journal = getattr(current, "_journal", None)
        return bool(journal is not None and journal.append(binding.key(), {**event, "at": event.get("at", time.time())}))


def cancel_turn(binding: Any, *, reason: str = "turn_cancelled") -> list[dict[str, Any]]:
    """C12: the owner or the console cancelled the turn. In-flight browser actions become
    cancelled_unknown, later tool calls are refused until a new observation, the session
    and its browser stay as they are (a cancel is not a revoke)."""
    # Q05 B3: a decider loop between two steps has nothing in flight; the generation is what stops it.
    _cancel_generations[binding.key()] = _cancel_generations.get(binding.key(), 0) + 1
    try:
        receipts = gateway().cancel_in_flight(binding.key(), reason=reason)
    except GatewayRefused:
        return []
    return [{"action_id": r.action_id, "attempt_id": r.attempt_id, "outcome": r.outcome.value} for r in receipts]


def session_facts(binding: Any) -> dict[str, Any]:
    """For the owner surface: how many agent actions are in flight, what the last one was and how it ended,
    how many submits are waiting on approval. Counts and kinds only -- no targets, no page content."""
    try:
        session = gateway()._sessions[binding.key()]
    except (GatewayRefused, KeyError):
        return {"in_flight": 0, "last_action": None, "proposals": 0, "cancelled": 0}
    receipted = {r.attempt_id for rs in session.receipts.values() for r in rs}
    in_flight = [a for attempt_id, a in session.admitted.items() if attempt_id not in receipted]
    last: dict[str, Any] | None = None
    for event in reversed(session.events):
        if event.get("type") == "receipt":
            last = {"kind": next((a.action.kind for a in session.admitted.values() if a.action.attempt_id == event.get("attempt_id")), ""),
                    "outcome": event.get("outcome", ""), "reason": str(event.get("reason", "") or "")[:80], "at": event.get("at")}
            break
    return {
        "in_flight": len(in_flight),
        "last_action": last,
        "proposals": sum(1 for e in session.events if e.get("type") == "proposed"),
        "cancelled": sum(1 for e in session.events if e.get("type") == "receipt" and str(e.get("outcome", "")).startswith("cancelled")),
        "demo": recorder().status(binding),
    }


def events_for(binding: Any) -> list[dict[str, Any]]:
    current = gateway()
    durable: list[dict[str, Any]] = []
    journal = getattr(current, "_journal", None)
    if journal is not None:
        durable = journal.events(binding.key())
    try:
        memory = current.events(binding.key())
    except Exception:  # noqa: BLE001 - never observed through the gateway
        memory = []
    # A successful append exists in both projections.  Keep the durable copy;
    # retain memory-only events when the journal was temporarily unavailable.
    merged = list(durable)
    seen = {json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) for event in durable}
    for event in memory:
        identity = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        if identity not in seen:
            merged.append(event)
            seen.add(identity)
    return merged
