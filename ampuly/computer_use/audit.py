"""One task's Computer Use story in one view (4.3 X01 / C24).

The Gateway journals observations, admissions, refusals and receipts; the
Effect journal holds each submit's proposal, approval and outcome; the order
carries the scope the master delegated; the session registry says who holds
the page. Each is complete on its own and none tells the whole story. This
joins them into one timeline for the owner and the auditor -- counts, kinds,
states, ids and times, never a target, a page's text, a typed value or a
credential (C27). Nothing here decides anything.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

# What a Gateway event may contribute to the view. Anything else it carries
# (a target, an observation payload) stays in the journal.
GATEWAY_KEYS = frozenset({"type", "kind", "risk", "outcome", "reason", "attempt_id", "action_id", "sequence", "state", "note", "action", "actor",
                          "effect_id", "checkpoint_id", "function", "model", "decided_on", "observed", "stage", "at",
                          "suggested_by", "reviewed_by", "fallback_to"})
DELEGATION_KEYS = ("browser_allowed_domains", "browser_allow_vision", "browser_allow_decider", "browser_allow_downloads", "browser_lease_minutes",
                   "browser_delegated_by", "browser_delegated_at", "browser_combination_executor", "browser_combination_missing")
SESSION_KEYS = frozenset({"state", "allowed_domains", "live", "lease_seconds_left", "unavailable"})
COMBINATION_KEYS = frozenset({
    "available", "executor", "browser_executor", "platform", "verified", "failed", "missing", "admitted",
})


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _safe_page(value: Any) -> str:
    """Keep the destination identity, never credentials, query values or fragments."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//{text}")
    host = parsed.hostname or ""
    path = parsed.path or ""
    if not host:
        return text.split("?", 1)[0].split("#", 1)[0][:300]
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    port = f":{parsed_port}" if parsed_port else ""
    return f"{scheme}{host}{port}{path}"[:300]


# 4.7-A17: where one browser decision came from, who checked it, and who has it now. Three fields, each from a small
# vocabulary, filled from what happened -- never guessed:
#   suggested_by  puly | model:<name> (the step's own model; "model" when the task records no name) | none
#   reviewed_by   code_check | owner | employee:<id> | none
#   fallback_to   owner | model:<name> | employee:<id> | none
PROVENANCE_KEYS = ("suggested_by", "reviewed_by", "fallback_to")
_DONE = frozenset({"done_verified", "done_unverified"})
# A turn that was cancelled: nobody takes it over.
_ENDS_THE_TURN = frozenset({"cancelled", "turn_cancelled"})
_PULY_DOWN_WORDS = {"decider_budget_stopped": "PULY 的额度用完了", "no_decision_provider": "PULY 没有接上",
                    "decider_too_large": "页面太大，PULY 问不了"}
_OP_WORDS = {"click": "点一下", "select": "在列表里选一项", "scroll": "往下翻", "back": "返回上一页", "wait": "等页面"}
_STOP_WORDS = {
    "off_connector_domain": "页面离开了这一步指定的网站", "no_progress": "连着几步页面都没变化",
    "target_not_found": "要点的东西找不到了", "unresolved_outcome": "有一步的结果还不明",
    "page_unreadable": "页面读不了", "connector_step_limit": "这一步能走的步数用完了",
    "agent_not_in_control": "页面在人手里", "no_observation": "还没看过页面", "no_delegated_hosts": "没有授权的网站",
    "decider_switched_off": "PULY 被关掉了", "cancelled": "这一回合被取消了", "turn_cancelled": "这一回合被取消了",
    "undeclared_control": "这个按钮不在这一步允许点的范围里",
}
# The action was sent: code let it through, and then it ended this way.
_AFTER_SENT = {"outcome_unknown": "结果不明", "action_failed": "没做成"}


def step_model(task_metadata: dict[str, Any] | None) -> str:
    """The step's own model as provenance names it: ``model:<name>``, or ``model`` when the task records no name."""
    route = dict(dict(task_metadata or {}).get("model_route", {}) or {})
    name = str(route.get("model", "") or "").strip()[:80]
    return f"model:{name}" if name else "model"


def reviewer(actor: Any) -> str:
    """Who reviewed, from an effect journal actor: an employee stays ``employee:<id>``; anyone else who approved is the
    owner (an effect's approval is the owner's alone: engine._effect_authorization_actor). Nobody: ``none``."""
    text = str(actor or "").strip()
    if not text or text == "system":
        return "none"
    return text[:80] if text.startswith("employee:") else "owner"


def decision_provenance(status: str, reason: str = "", *, puly_answered: bool, step_model: str,
                        check_only: bool = False) -> dict[str, str]:
    """The three fields for one ``browser_decide_act`` ending (ampuly/layer4_tools/browser.py).

    ``puly_answered``: PULY gave an answer to the last question this call asked (a choice or 「没把握」); a failure,
    an unknown ending, a budget stop or no PULY at all is not an answer. ``check_only``: the step's own model said the
    work is done and asked code to check it. The code check and the Gateway's gates are ``code_check``; a person's
    review is recorded where it happens (an effect's approval, below), never here.
    """
    status, reason = str(status or ""), str(reason or "")
    model = str(step_model or "") or "model"
    suggested = model if check_only else ("puly" if puly_answered else "none")
    if status in _DONE or status == "needs_submit" or (status == "hand_off" and reason == "a_secret_field"):
        reviewed = "code_check"
    elif status == "stopped" and puly_answered:
        reviewed = "code_check"   # PULY's line was held to the gates, and a gate is what ended it
    else:
        reviewed = "none"
    if status == "done_verified":
        fallback = "none"
    elif status in {"done_unverified", "needs_submit", "hand_off"} or reason == "agent_not_in_control":
        fallback = "owner"
    elif reason in _ENDS_THE_TURN:
        fallback = "none"
    else:
        fallback = model
    return {"suggested_by": suggested, "reviewed_by": reviewed, "fallback_to": fallback}


# 4.7-AR: the owner's answer on the card a browser step raises when it said done and code could not confirm it
# (ampuly/layer2_organization/connector_done_wait.py). The person's review is one more link of the same chain, journaled
# as a decision_trail with the choice as its outcome -- so the audit view and the order's timeline stop at 「交给你决定」
# only while the owner has not answered.
OWNER_ACCEPTED_DONE = "owner_accepted_done"
OWNER_CONTINUED = "owner_continued"
OWNER_ANSWERS = frozenset({OWNER_ACCEPTED_DONE, OWNER_CONTINUED})


def done_suggested_by(events: list[Any], step_model: str, *, since: float | None = None) -> str:
    """Who said the step was done: the last done check journaled in this turn (at or after ``since``) names it -- PULY,
    or the step's model asking code to check. With none, the step's own model finished its turn without one."""
    for event in reversed(list(events or [])):
        if not isinstance(event, dict) or event.get("type") != "decision_trail" or event.get("outcome") not in _DONE:
            continue
        at = _epoch(event.get("at"))
        if since is not None and (at is None or at < float(since)):
            break   # older than this turn: this turn's done was not checked
        return str(event.get("suggested_by", "") or "") or str(step_model or "") or "model"
    return str(step_model or "") or "model"


def owner_review_provenance(accepted: bool, *, suggested_by: str, step_model: str) -> dict[str, str]:
    """The owner answered the card: accepted, nobody else has it; sent back, the step's own model works on."""
    model = str(step_model or "") or "model"
    return {"outcome": OWNER_ACCEPTED_DONE if accepted else OWNER_CONTINUED,
            "suggested_by": str(suggested_by or "") or model, "reviewed_by": "owner",
            "fallback_to": "none" if accepted else model}


def effect_provenance(record: Any, events: list[Any], *, suggested_by: str) -> dict[str, str]:
    """The three fields for one browser submit, from the effect journal: who approved or refused it, and whose it is now."""
    state = str(getattr(getattr(record, "state", ""), "value", getattr(record, "state", "")) or "")
    authorized = str(getattr(record, "authorized_by", "") or "")
    reviewed = reviewer(authorized) if authorized else "none"
    if state == "rejected":
        refusal = next((dict(getattr(e, "payload", {}) or {}) for e in reversed(events)
                        if str(getattr(e, "event_type", "") or "") == "effect_rejected"), {})
        # An approval that ran out of time was reviewed by nobody.
        reviewed = "none" if str(refusal.get("reason", "") or "") == "approval_expired" else reviewer(refusal.get("rejected_by"))
    # Waiting for approval is the owner's; so is a submit whose outcome is unknown: only the owner may ask for the
    # look-back, and what it cannot settle stays for a person to decide (effects/gateway.py look_back). A failed or
    # refused one is closed -- a new attempt is a new proposal with its own row.
    fallback = "owner" if (state in {"proposed", "awaiting_approval"} and not authorized) or state == "outcome_unknown" else "none"
    return {"suggested_by": str(suggested_by or "") or "model", "reviewed_by": reviewed, "fallback_to": fallback}


def _model_words(value: str) -> str:
    name = value.split(":", 1)[1] if ":" in value else ""
    return f"模型（{name}）" if name else "模型"


def _person_words(value: str) -> str:
    if value == "owner":
        return "你"
    return f"员工 {value.split(':', 1)[1]}" if value.startswith("employee:") else ""


def _person_says(value: str, verb: str) -> str:
    """「你已批准」, 「员工 e-7 已批准」; '' for nobody."""
    who = _person_words(value)
    if not who:
        return ""
    return f"{who}{verb}" if who == "你" else f"{who} {verb}"


def _effect_text(suggested: str, reviewed: str, fallback: str, state: str) -> str:
    source = "PULY 建议提交" if suggested == "puly" else f"{_model_words(suggested)}提出提交"
    parts = [source]
    if _person_words(reviewed):
        parts.append(_person_says(reviewed, "没有批准" if state == "rejected" else "已批准"))
    elif state == "rejected":
        parts.append("审批过期")
    if fallback == "owner":
        parts.append("结果不明，等你回查" if state == "outcome_unknown" else "等你批准")
    else:
        parts.append({"applied": "已执行", "failed": "执行失败", "outcome_unknown": "结果不明，待回查", "claimed": "正在执行",
                      "rejected": "没有执行"}.get(state, "等待执行" if reviewed != "none" else "已结束"))
    return " → ".join(parts)


def provenance_text(entry: dict[str, Any]) -> str:
    """The chain in words for the owner, e.g. 「PULY 建议点一下 → 代码核对通过 → 已执行」. '' without the fields."""
    suggested = str(entry.get("suggested_by", "") or "")
    reviewed = str(entry.get("reviewed_by", "") or "")
    fallback = str(entry.get("fallback_to", "") or "")
    if not (suggested or reviewed or fallback):
        return ""
    if entry.get("source") == "effect" or entry.get("type") == "effect_proposed":
        return _effect_text(suggested, reviewed, fallback, str(entry.get("state_now", "") or entry.get("state", "") or ""))
    outcome = str(entry.get("outcome", "") or "")
    reason = str(entry.get("reason", "") or "")
    if outcome in OWNER_ANSWERS:
        # 4.7-AR: the owner's answer on the done card, e.g. 「PULY 说做完了 → 代码核对没通过 → 你已核对，算完成」.
        said_done = ("PULY 说做完了" if suggested == "puly"
                     else f"{_model_words(suggested)}说做完了" if suggested.startswith("model") else "这一步说做完了")
        who = _person_words(reviewed) or "你"
        if outcome == OWNER_ACCEPTED_DONE:
            answer = f"{who}已核对，算完成" if who == "你" else f"{who} 已核对，算完成"
        else:
            back = _model_words(fallback) if fallback.startswith("model") else "模型"
            answer = f"{who}让它继续做，交回{back}接着做" if who == "你" else f"{who} 让它继续做，交回{back}接着做"
        return " → ".join([said_done, "代码核对没通过", answer])
    if suggested == "puly":
        source = {"decider_not_sure": "PULY 判断没把握", "decider_answer_not_offered": "PULY 的回答不在可选范围里",
                  "decider_chose_a_person": "PULY 判断这里要人来做", "a_secret_field": "PULY 建议填写一个字段",
                  "decider_chose_a_submitting_choice": "PULY 建议在列表里选一项"}.get(reason, "")
        if not source:
            source = {"done_verified": "PULY 说做完了", "done_unverified": "PULY 说做完了", "needs_submit": "PULY 建议点一下",
                      "needs_text": "PULY 建议填写一个字段", "suggested": "PULY 给了建议"}.get(outcome, "")
        if not source:
            op = _OP_WORDS.get(str(entry.get("function", "") or ""), "")
            source = f"PULY 建议{op}" if op else "PULY 建议"
    elif suggested.startswith("model"):
        source = f"{_model_words(suggested)}{'说做完了' if outcome in _DONE else '提出'}"
    elif reason in _PULY_DOWN_WORDS or (reason.startswith("decider_") and reason != "decider_switched_off"):
        source = _PULY_DOWN_WORDS.get(reason, "PULY 不可用")
    elif reason == "decider_switched_off":
        source = "PULY 被关掉了"
    else:
        source = "没有问 PULY" + (f"（{_STOP_WORDS[reason]}）" if reason in _STOP_WORDS else "")
    parts = [source]
    if reviewed == "code_check":
        if outcome == "done_unverified":
            parts.append("代码核对没通过")
        elif outcome == "needs_submit":
            parts.append("代码发现它会往网站写数据，没有自己点" if reason == "control_writes_to_the_server" else "代码判定会提交，没有自己点")
        elif outcome == "hand_off":
            parts.append("代码判定这是密码一类的字段，不让模型填")
        elif outcome in {"done_verified", "acted"} or reason in _AFTER_SENT or reason == "step_budget_for_this_call":
            parts.append("代码核对通过")
        else:
            parts.append("代码拦下" + (f"（{_STOP_WORDS[reason]}）" if reason in _STOP_WORDS else ""))
    elif _person_words(reviewed):
        parts.append(_person_says(reviewed, "已复核"))
    if fallback == "owner":
        parts.append({"needs_submit": "交给你批准", "hand_off": "交给你来做"}.get(outcome, "交给你决定"))
    elif fallback.startswith("model"):
        who = _model_words(fallback)
        lead = _AFTER_SENT.get(reason, "这次的几步走完了" if reason == "step_budget_for_this_call" else "")
        tail = {"needs_text": f"交回{who}来填", "suggested": f"由{who}决定照不照做"}.get(outcome, f"交回{who}接着做")
        parts.append(f"{lead}，{tail}" if lead else tail)
    elif _person_words(fallback):
        parts.append(f"交给{_person_words(fallback)}")
    else:
        parts.append({"done_verified": "已完成", "acted": "已执行"}.get(outcome, "已停下" if reason in _ENDS_THE_TURN else "已结束"))
    return " → ".join(parts)


def _gateway_entry(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") == "console_control":
        return {"source": "console", "type": str(event.get("action", "") or ""),
                "actor": str(event.get("actor", "") or ""), "at": _epoch(event.get("at")), "durable": True}
    entry = {k: v for k, v in event.items() if k in GATEWAY_KEYS}
    entry["source"] = "gateway"
    entry["at"] = _epoch(event.get("at"))
    if any(key in entry for key in PROVENANCE_KEYS):
        entry["said"] = provenance_text(entry)
    return entry


def _proposer_of(record: Any, gateway_events: list[dict[str, Any]], model: str) -> str:
    """Who suggested this submit (4.7-A17): PULY when the last decision journaled before it handed this very control
    to the owner as a submit -- the same ref on the same page state (a ref names a control of one snapshot and is
    reused on the next page, so the ref alone is not the control); otherwise the step's own model, whose tool call
    proposed it."""
    created = _epoch(getattr(record, "created_at", ""))
    payload = dict(getattr(record, "payload", {}) or {})
    ref = str(dict(payload.get("element", {}) or {}).get("ref", "") or "")
    page_state = str(payload.get("observation_hash", "") or "")[:16]
    if created is None or not ref or not page_state:
        return model
    # The effect journal stamps to the microsecond and the Gateway's clock finer: a millisecond's allowance.
    before = [e for e in gateway_events if isinstance(e, dict) and e.get("type") == "decision_trail"
              and (_epoch(e.get("at")) or 0.0) <= created + 0.001]
    last = before[-1] if before else {}
    handed = (last.get("outcome") == "needs_submit" and last.get("suggested_by") == "puly" and str(last.get("ref", "") or "") == ref
              and str(last.get("decided_on", "") or "") == page_state)
    return "puly" if handed else model


def _effect_entries(record: Any, events: list[Any], *, suggested_by: str = "") -> list[dict[str, Any]]:
    payload = dict(getattr(record, "payload", {}) or {})
    out = [{
        "source": "effect", "type": "effect_proposed", "at": _epoch(getattr(record, "created_at", "")),
        "effect_id": str(getattr(record, "effect_id", "") or ""), "kind": str(getattr(record, "kind", "") or ""),
        "page": _safe_page(payload.get("page")), "expect": sorted(dict(payload.get("expect", {}) or {})),
        "state_now": str(getattr(getattr(record, "state", ""), "value", getattr(record, "state", "")) or ""),
        "authorized_by": str(getattr(record, "authorized_by", "") or ""),
    }]
    if suggested_by:
        out[0].update(effect_provenance(record, events, suggested_by=suggested_by))
        out[0]["said"] = provenance_text(out[0])
    for event in events:
        payload = dict(getattr(event, "payload", {}) or {})
        out.append({
            "source": "effect", "type": str(getattr(event, "event_type", "") or ""), "at": _epoch(getattr(event, "created_at", "")),
            "effect_id": str(getattr(event, "effect_id", "") or ""), "from": str(getattr(event, "from_state", "") or ""),
            "to": str(getattr(event, "to_state", "") or ""),
            **({"status": str(payload.get("status", ""))} if payload.get("status") else {}),
        })
    return out


def audit_trail(*, binding_key: str, task_id: str, gateway_events: list[dict[str, Any]], effects: list[Any],
                effect_events: dict[str, list[Any]], task_metadata: dict[str, Any] | None, session: dict[str, Any] | None,
                combination: dict[str, Any] | None = None,
                console_events: list[dict[str, Any]] | None = None,
                artifacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The joined view. Entries are ordered by time when every source stamps one; ties keep source order."""
    metadata = dict(task_metadata or {})
    delegation = {k: metadata[k] for k in DELEGATION_KEYS if k in metadata}
    timeline: list[dict[str, Any]] = [_gateway_entry(e) for e in gateway_events if isinstance(e, dict)]
    model = step_model(metadata)
    raw_events = [e for e in gateway_events if isinstance(e, dict)]
    for record in effects:
        timeline.extend(_effect_entries(record, list(effect_events.get(str(getattr(record, "effect_id", "") or ""), []) or []),
                                        suggested_by=_proposer_of(record, raw_events, model)))
    if delegation:
        timeline.append({"source": "delegation", "type": "browser_delegated", "at": _epoch(delegation.get("browser_delegated_at")),
                         "domains": list(delegation.get("browser_allowed_domains", []) or []),
                         "vision": bool(delegation.get("browser_allow_vision", False)),
                         "downloads": bool(delegation.get("browser_allow_downloads", False)),
                         "lease_minutes": delegation.get("browser_lease_minutes"), "by": str(delegation.get("browser_delegated_by", "") or "")})
    durable_controls = {(str(entry.get("type", "") or ""), entry.get("at")) for entry in timeline
                        if entry.get("source") == "console" and entry.get("durable") is True}
    for event in (console_events or []):
        # Only the action and its time reach the view -- never the note (C27: actions, ids and times,
        # not free text). The note stays on the live session state for the console to show.
        legacy = {"source": "console", "type": str(event.get("action", "") or ""), "at": _epoch(event.get("at"))}
        if (legacy["type"], legacy["at"]) not in durable_controls:
            timeline.append(legacy)
    for art in (artifacts or []):
        # A screenshot carries its product-generated name; a download carries only its time -- its
        # suggested filename is page-controlled and never reaches the view (C27).
        entry = {"source": "artifact", "type": str(art.get("kind", "") or ""), "at": _epoch(art.get("at"))}
        name = str(art.get("name", "") or "")
        if name:
            entry["name"] = name
        timeline.append(entry)
    indexed = list(enumerate(timeline))
    indexed.sort(key=lambda pair: (pair[1]["at"] if pair[1]["at"] is not None else float("inf"), pair[0]))
    timeline = [entry for _index, entry in indexed]

    admitted = [e for e in timeline if e["source"] == "gateway" and e.get("type") == "admitted"]
    receipts = [e for e in timeline if e["source"] == "gateway" and e.get("type") == "receipt"]
    refused = [e for e in timeline if e["source"] == "gateway" and e.get("type") == "refused"]
    receipted_attempts = {e.get("attempt_id") for e in receipts}
    outcomes: dict[str, int] = {}
    for e in receipts:
        outcomes[str(e.get("outcome", ""))] = outcomes.get(str(e.get("outcome", "")), 0) + 1
    reasons: dict[str, int] = {}
    for e in refused:
        reasons[str(e.get("reason", ""))] = reasons.get(str(e.get("reason", "")), 0) + 1
    effect_states: dict[str, int] = {}
    for record in effects:
        state = str(getattr(getattr(record, "state", ""), "value", getattr(record, "state", "")) or "")
        effect_states[state] = effect_states.get(state, 0) + 1
    summary = {
        "admitted": len(admitted), "refused": reasons, "receipts": outcomes,
        "in_flight": len([e for e in admitted if e.get("attempt_id") not in receipted_attempts]),
        "proposals": len([e for e in timeline if e["source"] == "gateway" and e.get("type") == "proposed"]),
        "effects": effect_states,
        "open_questions": sorted(str(getattr(r, "effect_id", "")) for r in effects
                                 if str(getattr(getattr(r, "state", ""), "value", "") or "") == "outcome_unknown"),
        "lease_changes": len([e for e in timeline if e["source"] == "gateway" and e.get("type") == "lease"]),
        "console": {a: sum(1 for e in timeline if e["source"] == "console" and e.get("type") == a)
                    for a in sorted({str(e.get("type", "")) for e in timeline if e["source"] == "console"})},
        "artifacts": {k: sum(1 for e in timeline if e["source"] == "artifact" and e.get("type") == k)
                      for k in sorted({str(e.get("type", "")) for e in timeline if e["source"] == "artifact"})},
    }
    safe_session = {key: dict(session or {})[key] for key in SESSION_KEYS if key in dict(session or {})}
    safe_combination = {key: dict(combination or {})[key] for key in COMBINATION_KEYS if key in dict(combination or {})}
    return {
        "binding_key": binding_key, "task_id": task_id, "delegation": delegation, "session": safe_session,
        "combination": safe_combination, "summary": summary, "timeline": timeline,
    }
