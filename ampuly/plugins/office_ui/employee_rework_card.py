"""Pure schema-1 Feishu confirmation card for AE00 section 8; no registration or IO.

Integration contract (Claude A owns the handler and the core transaction):
* Project ``record`` from the server's persisted human_review_rework_to and
  gate_harness_rework_request. Independently reload ``current_record``; do not
  copy the card's payload and call it a current record. Persist request_id with
  this exact card version, checkpoint and rework record. Never rotate it after
  an unknown outcome or generate it in this module.
* Register a FeishuCardKind using employee_rework_card_bridge_spec(), supply
  load_spec/answer/on_event/retire_reason hooks and the authenticated actor.
  Nothing here registers itself or implements a rework handler.
* Existing parse_event retains only action/gate/version, not extra callback
  value keys. Therefore hooks MUST resolve the exact frozen record and stable
  request_id through server-owned (checkpoint_id, card_version); do not expect
  extra callback fields to arrive on GateAction. Unknown mappings fail closed.
* Reconstruct human_review_rework_confirm's AE00 payload from that server
  record, including revision_note (never from arbitrary callback overrides).
  Recheck project membership, current in-service human eligibility from the
  frozen reviewer_slot or audited owner override, original version/attempt,
  upstream target, cumulative rework and money limits, transaction fencing and
  durable idempotency. Card eligibility is display projection, not permission.
* Recheck received card version and actor even on an idempotent/read lookup.
  A send/received acknowledgement is not a dispatched result. Unknown outcomes
  use a read-only effect/record lookup; never issue another write request.

This card contains no document body, knowledge, credentials, authority override,
external URL, output file or cost promise. All contents use plain_text.
"""

from collections.abc import Mapping, Sequence
import re
from typing import Any

CARD_KIND = "ai_employee_rework"
CONFIRM_DECISION = "ai_employee_rework_confirm"
INSPECT_DECISION = "ai_employee_rework_inspect"
AE00_ACTION = "human_review_rework_confirm"
READ_ACTION = "human_review_rework_inspect"  # Bridge name, NOT an existing AE00 action.

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_URL = re.compile(r"(?:https?://|www\.|file://|javascript:|data:)[^\s]*", re.IGNORECASE)
_REFERENCES = (
    "project_id", "order_id", "task_id", "review_node_id", "rework_record_id",
    "checkpoint_id", "expected_attempt", "reviewed_artifact_id", "reviewed_artifact_version",
    "reviewed_content_hash", "target_node_id", "revision_note_hash", "request_id", "card_version",
)
_RECORD_KEYS = frozenset((*_REFERENCES, "revision_note", "dispatched"))
_VIEW_KEYS = frozenset({"record", "current_record", "allowed_actions", "connected", "handler_ready",
    "actor_eligible", "record_matches_current", "read_handler_ready", "request_state", "runtime_state",
    "server_consequence", "project_label", "order_label", "target_label", "status_line"})
_REQUEST_WORDS = {
    "idle": "等待确认重派", "pending": "正在发送，等待回执", "received": "请求已收到，等待处理结果",
    "unknown": "结果未知；先核实，不重复重派", "partial": "仅完成一部分；先核实差异，不重复重派",
    "succeeded": "服务端已确认结果，请以当前派工记录为准", "failed": "上次操作失败，请核实当前状态",
}


def _plain(value: Any, *, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    value = "".join(char for char in value[:limit] if ord(char) >= 32 or char == "\n")
    return _URL.sub("[外部地址已省略]", value)


def _valid_record(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) - _RECORD_KEYS:
        return False
    for key in _REFERENCES:
        item = value.get(key)
        if key in {"expected_attempt", "reviewed_artifact_version", "card_version"}:
            if type(item) is not int or not 1 <= item <= 2**31 - 1:
                return False
        elif key in {"reviewed_content_hash", "revision_note_hash"}:
            if not isinstance(item, str) or not _HASH.fullmatch(item):
                return False
        elif not isinstance(item, str) or not _ID.fullmatch(item):
            return False
    note = value.get("revision_note")
    return isinstance(note, str) and bool(note.strip()) and len(note) <= 4000 and type(value.get("dispatched")) is bool


def _validate_view(view: Mapping[str, Any]) -> None:
    if not isinstance(view, Mapping) or set(view) - _VIEW_KEYS:
        raise ValueError("invalid_rework_card_projection")
    for name in ("record", "current_record"):
        record = view.get(name)
        if isinstance(record, Mapping) and set(record) - _RECORD_KEYS:
            raise ValueError("invalid_rework_record_projection")


def _actions(view: Mapping[str, Any]) -> frozenset[str]:
    actions = view.get("allowed_actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)) or len(actions) > 20:
        return frozenset()
    return frozenset(item for item in actions if isinstance(item, str))


def employee_rework_card_block_reason(view: Mapping[str, Any]) -> str:
    """Fail closed on projection gaps; it does not perform server authentication."""
    _validate_view(view)
    if view.get("connected") is not True:
        return "连接已断开；恢复后核实状态，不自动重发。"
    if view.get("handler_ready") is not True:
        return "确认重派尚未接入内核处理入口；这里只展示记录。"
    state = view.get("request_state")
    if state in {"pending", "received"}:
        return _REQUEST_WORDS[state]
    if state in {"unknown", "partial", "succeeded"}:
        return _REQUEST_WORDS[state]
    if state not in {"idle", "failed"}:
        return "操作结果状态待核实。"
    if view.get("runtime_state") != "rework_pending_confirm":
        runtime_words = {
            "paused": "运行已暂停；本卡不继续派工。", "stopped": "运行已停止；本卡不继续派工。",
            "completed": "订单已完成；本卡不能继续派工。", "cancelled": "订单已取消；本卡不能继续派工。",
            "reworking": "返修已在执行；不再重复派工。", "dispatch": "正在派工；不再重复提交。",
            "dispatched": "本次返修已派出；不再重复派工。",
        }
        return runtime_words.get(str(view.get("runtime_state", "")), "运行状态不允许确认重派。")
    record, current = view.get("record"), view.get("current_record")
    if not _valid_record(record) or not _valid_record(current):
        return "待返修记录、修改单或版本引用不完整；先核实。"
    if record["dispatched"] or current["dispatched"]:
        return "本次返修已派出；请查看派工记录，不再重复提交。"
    if view.get("record_matches_current") is not True or any(record[key] != current[key] for key in _REFERENCES) or record["revision_note"] != current["revision_note"]:
        return "这张卡对应旧版本或旧轮次；请核实当前返修记录。"
    if view.get("actor_eligible") is not True:
        return "当前人员未被服务端确认具有此轮审核资格。"
    if AE00_ACTION not in _actions(view):
        return "服务端未允许确认重派。"
    if not isinstance(view.get("server_consequence"), str) or not view["server_consequence"].strip():
        return "服务端尚未提供本次决定的后果说明。"
    return ""


def _callback(view: Mapping[str, Any], decision: str) -> dict[str, Any]:
    record = view["record"]
    return {"gate": record["checkpoint_id"], "version": record["card_version"], "action": decision,
        "rework_record_id": record["rework_record_id"], "request_id": record["request_id"]}


def employee_rework_confirm_value(view: Mapping[str, Any]) -> dict[str, Any] | None:
    """Minimal callback reference; never assembles an executable AE00 write body."""
    return None if employee_rework_card_block_reason(view) else _callback(view, CONFIRM_DECISION)


def employee_rework_inspect_value(view: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read bridge must be supplied by the host; no fake read button is offered."""
    _validate_view(view)
    if view.get("connected") is not True or view.get("read_handler_ready") is not True:
        return None
    if READ_ACTION not in _actions(view) or view.get("actor_eligible") is not True or not _valid_record(view.get("record")):
        return None
    return _callback(view, INSPECT_DECISION)


def _text(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def build_employee_rework_card(view: Mapping[str, Any], *, status_line: str = "") -> dict[str, Any]:
    """Build bounded schema-1 JSON. No loading, registration, send or side effects."""
    reason = employee_rework_card_block_reason(view)
    record = view.get("record")
    elements: list[dict[str, Any]] = []
    for label, key in (("项目", "project_label"), ("工单", "order_label"), ("返修到", "target_label")):
        shown = _plain(view.get(key), limit=160)
        if shown:
            elements.append(_text(f"{label}：{shown}"))
    if _valid_record(record):
        elements.append(_text(f"本次意见针对：{record['reviewed_artifact_id']} · 第 {record['reviewed_artifact_version']} 版 · attempt {record['expected_attempt']}"))
        elements.append(_text(f"修改单已保存在服务端；确认时使用记录 {record['rework_record_id']} 对应的固定版本。"))
    else:
        elements.append(_text("返修记录尚未完整加载；不提供确认操作。"))
    state = str(view.get("request_state", ""))
    elements.append(_text(_REQUEST_WORDS.get(state, "操作状态待核实")))
    if view.get("request_state") in {"pending", "received"}:
        elements.append(_text("收到请求不等于已经重派；以服务端最终派工记录为准。"))
    consequence = _plain(view.get("server_consequence"), limit=1000)
    if consequence:
        elements.append(_text(f"决定后会发生什么：{consequence}"))
    retired_line = _plain(status_line or view.get("status_line"), limit=600)
    if retired_line:
        reason = retired_line
    if reason:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": reason}]})
    buttons: list[dict[str, Any]] = []
    confirm = None if retired_line else employee_rework_confirm_value(view)
    if confirm:
        buttons.append({"tag": "button", "text": {"tag": "plain_text", "content": "确认重派"}, "type": "primary", "value": confirm})
    inspect = None if retired_line else employee_rework_inspect_value(view)
    if inspect:
        buttons.append({"tag": "button", "text": {"tag": "plain_text", "content": "核实当前记录"}, "type": "default", "value": inspect})
    elif view.get("request_state") in {"unknown", "partial"} and not retired_line:
        elements.append(_text("核实入口尚未接线；请由控制台读取当前返修记录，不要再次提交。"))
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    return {"config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue" if confirm else "grey", "title": {"tag": "plain_text", "content": "人工打回 · 确认重派"}},
        "elements": elements}


def build_employee_rework_gate_card(spec: Any, image_keys: Sequence[str], *, status_line: str = "") -> dict[str, Any]:
    """Adapter for FeishuCardKind.build_card(spec, image_keys, status_line=...)."""
    del image_keys
    delivery = getattr(spec, "delivery", None)
    view = delivery.get("employee_rework") if isinstance(delivery, Mapping) else None
    if not isinstance(view, Mapping):
        view = {}
    # Gate envelope must match the immutable projection; never rewrite the record to fit a different gate.
    copied = dict(view)
    record = copied.get("record")
    if (getattr(spec, "card_kind", None) != CARD_KIND or not _valid_record(record)
        or getattr(spec, "checkpoint_id", None) != record["checkpoint_id"]
        or getattr(spec, "project_id", None) != record["project_id"]
        or getattr(spec, "order_id", None) != record["order_id"]
        or getattr(spec, "task_id", None) != record["task_id"]
        or getattr(spec, "node_id", None) != record["review_node_id"]
        or type(getattr(spec, "version", None)) is not int or spec.version != record["card_version"]):
        copied["record_matches_current"] = False
        copied["read_handler_ready"] = False
        copied["handler_ready"] = False
    return build_employee_rework_card(copied, status_line=status_line)


def employee_rework_card_bridge_spec() -> dict[str, Any]:
    """Registration description only. Requires real A-line hooks; mutates no registry."""
    return {
        "name": CARD_KIND, "label": "人工打回 · 确认重派",
        "decisions": {CONFIRM_DECISION: "确认重派", INSPECT_DECISION: "核实当前记录"},
        "build_card": build_employee_rework_gate_card,
        "callback_keys": ("gate", "version", "action", "rework_record_id", "request_id"),
        "parse_event_retained_keys": ("gate", "version", "action"),
        "write_action": AE00_ACTION,
        "read_bridge_name": READ_ACTION,
        "read_bridge_is_existing_ae00_action": False,
        "registered": False,
        "requires": ("target", "on_event", "retire_reason", "hooks.load_spec", "hooks.answer",
            "persisted_checkpoint_version_to_record_and_request_mapping", "authenticated_actor_and_live_eligibility",
            "fenced_core_transaction_and_budget", "durable_idempotency"),
    }
