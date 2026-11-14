"""A browser connector step that ends without a code check that held waits for a person (Q05 B5-G).

A step carrying the browser universal connector is done only when code checked its postconditions on the page
(``browser_decide_act`` → ``done_verified``, ampuly/layer4_tools/browser.py): the model saying it is done is not done.
A turn of such a step that ends DONE without that verdict is neither failed nor run again on its own. It waits for the
owner on the ``task_user_input`` rail the budget stop uses (budget_stop_wait): the same card, durable checkpoint,
restart behaviour and way an answer reaches the item. The finished result is kept with the wait:

- "我已核对，算完成" completes the step with the kept result -- no turn runs again, and the owner's word is recorded;
- "让它继续做" runs the step's turn again (it checks again before it may finish);
- any other reply, or a release without an answer, asks again: never a guess.

The kernel calls ``as_pause`` where a turn's result arrives and ``take_answer`` where a work item is about to run again.

4.7-AS: a step that works in an app on this computer (``workflow_node.desktop``) rides the same rail, with a third
answer (「判这一步失败」) and a stop before its turn while the owner's desktop channel is closed
(``desktop_channel_hold``); see the section at the end of this module.
"""

from __future__ import annotations

from dataclasses import replace
import time
from datetime import datetime
from typing import Any

from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.i18n_runtime import is_zh
from ampuly.layer2_organization.budget_stop_wait import waits_for_a_person
from ampuly.layer2_organization.phase import is_review_execution_work_item_metadata

WAIT_KEY = "connector_done_wait"
# When this work item's current turn started: a done check from before it (an earlier run, a rework) does not stand.
TURN_KEY = "connector_turn_started_at"
QUESTION_ID = "connector_done"
ACCEPT = "accept_done"
CONTINUE = "continue_step"
# Which answers finish the step with the kept result rather than running the turn again. The batch sibling has its
# own, longer list; the caller reads this off whichever module owns the step.
COMPLETING = (ACCEPT,)
UNRECOGNISED = "unrecognised"
ACCEPTED_BY_OWNER = "accepted_by_owner"

_TEXT = {
    "zh": {
        "header": "浏览器步骤核验",
        "question": "这一步算完成吗？",
        ACCEPT: "我已核对，算完成",
        CONTINUE: "让它继续做",
        "headline": "「{title}」说做完了，但代码没有核验通过：{why}",
        "reason": "这一步在内置浏览器里工作。它说做完了，但页面上的完成条件没有被代码核验通过（{why}）。结果已原样保留，"
                  "没有判失败，也不会自动重跑。请在浏览器画面或交付物里核对：确认完成就选「我已核对，算完成」（会记下是您确认的，"
                  "不再跑模型）；没做完就选「让它继续做」，它会再跑一回合，完成前还要再核验一次。",
        "again": "上一次的答复没有说明（收到的是：{reply}），这一步仍在等待。请只回「我已核对，算完成」或「让它继续做」。",
        "asked_again": "这一步在没有收到回答的情况下被重新放行（卡片丢失、重启，或回答没有送达），所以再问一次；没有运行，也没有花费。",
        "accepted": "owner 已核对并确认这一步完成（代码核验没有通过：{why}）。",
        "why": {"no_check": "它没有运行完成核验", "condition_not_met": "有完成条件不成立", "condition_unanswered": "页面没有读全",
                "held_before_the_work": "条件在它动手之前就已成立", "no_postconditions": "这一步没有写完成条件",
                "unresolved_outcome": "有一次写入的结果未知", "page_unreadable": "页面读不了",
                "no_page": "这一步没有打开的浏览器页面", "off_connector_domain": "页面不在这一步指定的网站上",
                "changed_after_check": "核验通过后它又在页面上做了操作，或核验不是这一回合做的",
                "only_the_address_changed": "只有网址变了，页面上的完成条件没有新成立",
                "key_unresolved": "读不到本单的查询钥匙", "key_too_short": "本单的查询钥匙太短，无法可靠辨认",
                "not_this_turn": "页面不是这一回合打开的", "answer_not_in_one_block": "页面上本单的钥匙和答案不在同一块里",
                "key_not_matched": "页面上的是另一个编号", "no_result": "页面显示查无结果",
                "answer_not_found": "页面上摘不到要的答案", "lookup_step_wrote": "查询步骤发生了写入",
                "answer_ambiguous": "本单这一块里答案的标签出现不止一次，无法确定是哪一个"},
    },
    "en": {
        "header": "Browser step check",
        "question": "Is this step done?",
        ACCEPT: "I checked it: done",
        CONTINUE: "Let it keep working",
        "headline": "“{title}” says it is done, but the code check did not pass: {why}",
        "reason": "This step works in the built-in browser. It says it is done, but its done-when conditions were not "
                  "verified on the page by code ({why}). Its result is kept as it is; it has not failed and will not run "
                  "again on its own. Check the live page or the deliverable: choose “I checked it: done” to complete it "
                  "(recorded as your confirmation; no model runs), or “Let it keep working” to run its turn again (it is "
                  "checked again before it may finish).",
        "again": "The last answer said neither (it was: {reply}); this step is still waiting. Answer with exactly one of the two choices.",
        "asked_again": "This step was released without an answer (a lost card, a restart, or an answer that never arrived), so it is asked again; nothing ran and nothing was spent.",
        "accepted": "The owner checked this step and confirmed it done (the code check had not passed: {why}).",
        "why": {"no_check": "it never ran the done check", "condition_not_met": "a condition does not hold",
                "condition_unanswered": "the page was not read in full", "held_before_the_work": "the conditions held before it acted",
                "no_postconditions": "the step states no done-when conditions", "unresolved_outcome": "a write ended with an unknown outcome",
                "page_unreadable": "the page could not be read", "no_page": "the step has no open browser page",
                "off_connector_domain": "the page is not on the step's own sites",
                "changed_after_check": "it acted on the page after the check passed, or the check was not made in this turn",
                "only_the_address_changed": "only the address changed; no done-when condition on the page turned true",
                "key_unresolved": "the order's lookup key could not be read", "key_too_short": "the order's lookup key is too short to tell apart",
                "not_this_turn": "the page was not opened in this turn", "answer_not_in_one_block": "the key and the answer are not in one block of the page",
                "key_not_matched": "the page shows another number", "no_result": "the page says nothing was found",
                "answer_not_found": "the answer could not be taken off the page", "lookup_step_wrote": "the lookup step wrote to the site",
                "answer_ambiguous": "the answer's label appears more than once in this order's part of the page"},
    },
}


def _text() -> dict[str, Any]:
    return _TEXT["zh" if is_zh() else "en"]


def connector_of(task: Any) -> dict[str, Any] | None:
    """The browser connector of the step a task runs, or None (``workflow_node.connector``, written at turn start)."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node")
    connector = node.get("connector") if isinstance(node, dict) else None
    return connector if isinstance(connector, dict) else None


def note_turn_start(task: Task) -> None:
    """A connector step's turn is about to run: a done check made before now does not complete it.

    ``time.time()``, not ``datetime.now().timestamp()`` — and that is not a style choice. The stamps this is
    compared against (the Gateway's events, and bulk P1's ``page_opened``) all come from ``time.time()``, while
    ``datetime.now().timestamp()`` reaches the same instant through a timezone conversion and a little more float
    arithmetic. Measured on this machine: in 200,000 pairs, a ``time.time()`` taken AFTER a
    ``datetime.now().timestamp()`` came back SMALLER about 2% of the time, by up to 0.2 microseconds. Two percent
    of turns would then judge their own page as 「不是这一回合打开的」 — a rare, unreproducible failure that
    looks like anything but a clock. One clock, one comparison.

    A desktop step (4.7-A06, ``workflow_node.desktop``) is stamped the same way: its done check
    (desktop_bridge.finish) counts only the actions of this turn, and ``as_pause`` asks whether this turn's check
    still stands (4.7-AS). The batch card is a browser step's only.
    """
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node")
    desktop = isinstance(node, dict) and isinstance(node.get("desktop"), dict)
    if connector_of(task) is not None or desktop:
        task.metadata = {**dict(task.metadata or {}), TURN_KEY: time.time()}


def _check_of(task: Any) -> tuple[bool, dict[str, Any] | None]:
    """(whether the step's done check stands, its last verdict). It stands only when it held, on the conditions the step
    has now, in this turn, and nothing was written -- and no outcome became unknown -- after it
    (browser_bridge.done_verified). Anything unreadable: it does not stand."""
    try:
        from ampuly.computer_use import browser_bridge
        from ampuly.layer4_tools.browser_sessions import BrowserBinding

        binding = BrowserBinding.for_task(task)
        connector = connector_of(task) or {}
        posts = [p for p in list(connector.get("postconditions", []) or []) if isinstance(p, dict)]
        since = dict(task.metadata or {}).get(TURN_KEY)
        stands = browser_bridge.done_verified(binding, since=float(since) if since is not None else None,
                                              conditions=browser_bridge.done_conditions_digest(posts))
        return stands, browser_bridge.done_verdict(binding)
    except Exception:  # noqa: BLE001 - a verdict that cannot be read is no verdict: the step waits for a person
        return False, None


def _why(verdict: dict[str, Any] | None) -> str:
    reasons = dict(_text()["why"])
    if verdict is None:
        return reasons["no_check"]
    if verdict.get("status") == "done_verified":
        return reasons["changed_after_check"]   # it held, and does not stand any more
    reason = str(verdict.get("reason", "") or "")
    return reasons.get(reason, reason or reasons["no_check"])


def _pause_request(task: Task, why: str, *, reply: str = "", kept: dict[str, Any] | None = None) -> dict[str, Any]:
    text = _text()
    metadata = dict(task.metadata or {})
    previous = dict(metadata.get(WAIT_KEY, {}) or {})
    # Each park is its own event: checkpoints are keyed by source event and an answered one is a tombstone.
    seq = int(previous.get("seq", 0) or 0) + 1
    # An answer the step had asked for itself ("which account?") is kept and put back when the wait ends: the engine
    # overwrites those two context keys with this card's reply.
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    earlier = snapshot.get("requested_user_input")
    if isinstance(earlier, dict) and str(earlier.get(QUESTION_ID, "") or "").strip():
        carried = dict(previous.get("carried", {}) or {})
    else:
        carried = {key: snapshot[key] for key in ("user_supplied_input", "requested_user_input") if snapshot.get(key) not in (None, "", {})}
    fresh = kept is not None   # a new result to keep, or the same wait asked again
    kept = kept if kept is not None else previous.get("kept")
    task.metadata = {**metadata, WAIT_KEY: {"seq": seq, "why": why, "waiting": True, "at": datetime.now().isoformat(),
                                            **({"carried": carried} if carried else {}), **({"kept": kept} if kept else {})}}
    reason = text["reason"].format(why=why)
    if reply:
        reason = text["again"].format(reply=reply[:120]) + chr(10) + chr(10) + reason
    elif seq > 1 and not fresh:
        reason = text["asked_again"] + chr(10) + chr(10) + reason
    return {
        "requires_user_input": True,
        "reason": reason,
        "headline": text["headline"].format(title=str(task.title or task.id)[:80], why=why),
        QUESTION_ID: why,
        # Whether a step's outside work is done is the owner's to say (a reviewer's sub-account does not sign it).
        "assignee_employee_id": "owner",
        "request_id": f"connector-done:{task.id}:{seq}",
        "input_questions": [{
            "id": QUESTION_ID, "header": text["header"], "question": text["question"],
            "options": [{"id": ACCEPT, "label": text[ACCEPT]}, {"id": CONTINUE, "label": text[CONTINUE]}],
            "allow_freeform": False, "required": True,
        }],
        "required_fields": [QUESTION_ID],
    }


def waiter_for(task: Any) -> Any:
    """Which module owns this connector step's completion card: the batch sibling for a batch step, else this one.

    One funnel decides, so a step can never be offered both cards, and an answer to one can never be read as an
    answer to the other (they keep separate keys and question ids).
    """
    import sys

    from ampuly.layer2_organization import connector_batch_wait

    # The card that was RAISED is the card that gets answered. A batch step whose order had no list falls back to
    # this module's card, and its answer must reach this module's parser -- picking by step type alone would leave
    # that answer read by a parser that knows nothing about it, and the item waiting for ever.
    metadata = dict(getattr(task, "metadata", {}) or {})
    if metadata.get(connector_batch_wait.WAIT_KEY):
        return connector_batch_wait
    if metadata.get(WAIT_KEY):
        return sys.modules[__name__]
    return connector_batch_wait if connector_batch_wait.handles(task) else sys.modules[__name__]


def as_pause(task: Task, result: TaskResult) -> TaskResult:
    """A connector step's DONE without a verified done check, as a wait for the owner; anything else, unchanged.

    A batch step is the sibling's (批量自动化 P4): its rows say whether it is done, and it raises one card listing
    only its exceptions -- or none at all. Delegated here rather than added beside the engine's call site, so there
    stays exactly one place that decides which card a connector step gets.
    """
    if getattr(result, "status", None) != TaskStatus.DONE or not waits_for_a_person(task):
        return result
    if connector_of(task) is None:
        return _desktop_as_pause(task, result)   # 4.7-AS: a desktop step's DONE; any other step's, unchanged
    from ampuly.layer2_organization import connector_batch_wait

    parked = connector_batch_wait.as_pause(task, result)
    if parked is not None:
        return parked
    if is_review_execution_work_item_metadata(dict(task.metadata or {})):
        return result
    stands, verdict = _check_of(task)
    if stands:
        return result
    kept = {"content": str(result.content or ""), "artifacts": dict(result.artifacts or {}),
            "cost": float(result.cost or 0.0), "token_usage": dict(result.token_usage or {})}
    request = _pause_request(task, _why(verdict), kept=kept)
    artifacts = {key: value for key, value in dict(result.artifacts or {}).items() if key != "pause_request"}
    return replace(result, status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                   artifacts={**artifacts, "pause_request": request})


def is_waiting_result(result: Any) -> bool:
    """True for the result of a turn that ended in this module's wait."""
    if getattr(result, "status", None) != TaskStatus.AWAITING_HUMAN:
        return False
    request = dict(getattr(result, "artifacts", None) or {}).get("pause_request")
    return isinstance(request, dict) and bool(str(request.get(QUESTION_ID, "") or "").strip())


def _named_answer(reply: str, requested: dict[str, Any]) -> str:
    """The one answer the WHOLE reply names, or ``UNRECOGNISED`` (the chat card sends "- <header>: <label>", the
    approval centre the option id, a channel whatever was typed; a sentence that merely contains a label is not one)."""
    import re

    questions = [item for item in list(requested.get("input_questions", []) or []) if isinstance(item, dict)]
    labels = {str(option.get("id", "") or ""): " ".join(str(option.get("label", "") or "").lower().split())
              for question in questions for option in list(question.get("options", []) or []) if isinstance(option, dict)}
    headers = [" ".join(str(question.get(key, "") or "").lower().split()) for question in questions for key in ("header", "question", "id")]
    said = " ".join(str(reply or "").lower().split())
    structured = re.search(r"selected_option_id['\"]?\s*[:=]\s*['\"]?([a-z_]+)", said)
    if structured:
        said = structured.group(1)
    else:
        said = said.lstrip("-").strip()
        for header in filter(None, headers):
            for colon in (":", "："):
                if said.startswith(header + colon):
                    said = said[len(header) + 1:].strip()
        said = said.strip(" 。.!！")
    # The card's own labels, and both languages' (the console's language may change between the card and the answer).
    fixed = {key: {" ".join(str(_TEXT[lang][key]).lower().split()) for lang in _TEXT} for key in (ACCEPT, CONTINUE)}
    accepts = said in {ACCEPT, labels.get(ACCEPT) or ACCEPT, *fixed[ACCEPT]}
    continues = said in {CONTINUE, labels.get(CONTINUE) or CONTINUE, *fixed[CONTINUE]}
    if accepts == continues:
        return UNRECOGNISED
    return ACCEPT if accepts else CONTINUE


def take_answer(task: Task) -> tuple[str, str]:
    """``(answer, reply)`` for a work item about to run again: ``""`` when this run has nothing to do with the wait,
    else ``ACCEPT``, ``CONTINUE`` or ``UNRECOGNISED``. A waiting item that runs again WITHOUT an answer is
    ``UNRECOGNISED`` (asked again, never run). The reply and the request leave the task's context -- they are the
    owner's words to this module, not input for the model -- and what the context held before the wait is put back."""
    if _desktop_wait(task):
        return _desktop_take_answer(task)   # 4.7-AS: a desktop step's card has three answers and its own record
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    requested = snapshot.get("requested_user_input")
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if not isinstance(requested, dict) or not str(requested.get(QUESTION_ID, "") or "").strip():
        if wait.get("waiting"):
            return UNRECOGNISED, ""
        return "", ""
    reply = str(snapshot.pop("user_supplied_input", "") or "").strip()
    snapshot.pop("requested_user_input", None)
    snapshot.update(dict(wait.get("carried", {}) or {}))
    task.context_snapshot = snapshot
    answer = _named_answer(reply, requested)
    if answer == ACCEPT:
        task.metadata = {**metadata, WAIT_KEY: {**{k: v for k, v in wait.items() if k != "carried"}, "waiting": False,
                                                ACCEPTED_BY_OWNER: datetime.now().isoformat()}}
    elif answer == CONTINUE:
        task.metadata = {**metadata, WAIT_KEY: {**{k: v for k, v in wait.items() if k not in {"carried", "kept"}}, "waiting": False}}
    if answer in (ACCEPT, CONTINUE):
        _note_owner_review(task, accepted=answer == ACCEPT, card=f"connector-done:{task.id}:{int(wait.get('seq', 0) or 0)}")
    return answer, reply


def _note_owner_review(task: Task, *, accepted: bool, card: str, outcome: str = "") -> None:
    """4.7-AR: the owner's answer joins the step's decision chain (ampuly/computer_use/audit.py): reviewed by the owner,
    when, and which choice -- so the audit view and the order's timeline say 「你已核对，算完成」 instead of stopping
    at 「交给你决定」. Written like a decision the step journals itself (the live Gateway session, else the journal
    directly: the owner may answer after a restart). Once per card: a run interrupted before its commit reads the
    same answer again. Ids and vocabulary only; never raises -- a record that cannot be written never blocks the step.

    ``outcome`` (LK-1, 2026-09-27): an answer the two words of ``audit.owner_review_provenance`` cannot say -- the
    batch card's 「例外判失败，其余完成」 is neither 「算完成」 nor 「继续做」. It replaces the outcome word and the step
    is nobody's afterwards (it finishes); still reviewed by the owner. Empty: exactly as before."""
    try:
        from ampuly.computer_use import audit, browser_bridge
        from ampuly.layer4_tools.browser_sessions import BrowserBinding

        binding = BrowserBinding.for_task(task)
        events = browser_bridge.events_for(binding)
        if any(isinstance(event, dict) and event.get("type") == "decision_trail" and event.get("card") == card
               and event.get("reviewed_by") == "owner" for event in events):
            return
        metadata = dict(task.metadata or {})
        model = audit.step_model(metadata)
        since = metadata.get(TURN_KEY)
        fields = audit.owner_review_provenance(
            accepted, step_model=model,
            suggested_by=audit.done_suggested_by(events, model, since=float(since) if since is not None else None))
        if outcome:
            fields = {**fields, "outcome": str(outcome), "fallback_to": "none"}
        event = {"type": "decision_trail", **fields, "card": card, "at": time.time()}
        current = browser_bridge.gateway()
        try:
            current.annotate(binding.key(), event)
        except Exception:  # noqa: BLE001 - no live session (closed, or a restart): the journal alone keeps it
            journal = getattr(current, "_journal", None)
            if journal is not None:
                journal.append(binding.key(), event)
    except Exception:  # noqa: BLE001 - the owner's answer stands whether or not it could be journaled
        return


def kept_result_of(task: Task) -> TaskResult | None:
    """The finished result the owner accepted, with their confirmation said in it -- ``None`` when there is none.

    Reading does not remove it: it stays with the task until the step is committed done (``clear_kept``). Removed on
    reading, a suspension, a restart or a lost lease between the answer and the commit left nothing to complete with,
    and the turn ran again -- a paid turn nobody asked for (review, 2026-09-22; the same lesson as budget_stop_wait).
    """
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    kept = wait.get("kept")
    if not isinstance(kept, dict) or wait.get("waiting") or not wait.get(ACCEPTED_BY_OWNER):
        return None
    note = _text()["accepted"].format(why=str(wait.get("why", "") or ""))
    content = str(kept.get("content", "") or "")
    result = TaskResult(status=TaskStatus.DONE, content=(content + chr(10) + chr(10) + note) if content else note,
                        artifacts={**dict(kept.get("artifacts", {}) or {}), "connector_done": ACCEPTED_BY_OWNER},
                        cost=float(kept.get("cost", 0.0) or 0.0), token_usage=dict(kept.get("token_usage", {}) or {}))
    task.result = {"content": result.content, "artifacts": dict(result.artifacts)}
    return result


def clear_kept(task: Task) -> None:
    """The step is being committed done: the kept result has served, and a later run of the item is checked again."""
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if "kept" in wait or ACCEPTED_BY_OWNER in wait:
        task.metadata = {**metadata, WAIT_KEY: {k: v for k, v in wait.items() if k not in {"kept", ACCEPTED_BY_OWNER}}}


def clear_kept_any(task: Task) -> None:
    """Whichever completion card this connector step had, its kept result has served (批量自动化 P4).

    One call site rather than two side by side: a step has exactly one card, and forgetting the other module here
    is how a kept result outlives the step it belonged to.
    """
    from ampuly.layer2_organization import connector_batch_wait

    clear_kept(task)
    connector_batch_wait.clear_kept(task)


def park_again(task: Task, reply: str) -> TaskResult:
    """The same wait, asked again, for an answer that named neither choice."""
    if _desktop_wait(task):
        return _desktop_park_again(task, reply)
    wait = dict(dict(task.metadata or {}).get(WAIT_KEY, {}) or {})
    request = _pause_request(task, str(wait.get("why", "") or _why(None)), reply=reply)
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"], artifacts={"pause_request": request})


# ── 4.7-AS: a step that works in an app on this computer (workflow_node.desktop) ──────────────────────────────────
#
# The same rail as a browser step, with two differences the owner asked for:
#
# - the card has three answers: 「我已核对，算完成」 (the kept result completes the step, no model runs), 「打回重做」
#   (the turn runs again and checks again), 「判这一步失败」 (the step ends failed; what depends on it does not start);
# - a desktop step whose channel is closed is stopped BEFORE its turn (``desktop_channel_hold``): the owner is asked
#   with the desktop's own card (desktop_connector.stop_request), and no model is called while it stays closed.
#
# Done is this turn's desktop check still standing at the turn's end (desktop_bridge.done_standing). A deliverable the
# model wrote with an ordinary file tool is not a check: without one, the DONE waits for the owner. Every answer the
# owner gives is journaled in the step's decision chain, reviewed_by=owner (desktop_bridge.note_decision).

DESKTOP = "desktop"
FAIL_STEP = "fail_step"
FAILED_BY_OWNER = "failed_by_owner"
# The work item's phase reason when the owner ended a desktop step failed (the done card, or 「先不做这一步」).
FAILED_BY_OWNER_REASON = "desktop_step_failed_by_owner"
# The closed-channel card: its mark in the card, and its two answers (desktop_connector._STOP_TEXT's option ids).
CHANNEL_MARK = "desktop_channel"
CHANNEL_SKIP = "skip_step"
CHANNEL_RESUME = "resume_when_open"
DECLINED_BY_OWNER = "declined_by_owner"
_DONE_CARD = "done"

_DESKTOP_TEXT = {
    "zh": {
        "header": "桌面步骤核验",
        "question": "这一步算完成吗？",
        ACCEPT: "我已核对，算完成",
        CONTINUE: "打回重做",
        FAIL_STEP: "判这一步失败",
        "headline": "「{title}」要在本机桌面应用里做的事没有核验通过：{why}",
        "reason": "这一步要在本机桌面应用里做的事没有核验通过：{why}。结果已原样保留，没有判失败，也不会自动重跑。"
                  "请到这台电脑上或交付物里核对。您可以：「我已核对，算完成」（会记下是您确认的，不再跑模型）；"
                  "「打回重做」（它会再做一回合，完成前还要在桌面上再核验一次）；"
                  "「判这一步失败」（这一步记为没做成，依赖它的步骤不会自动开始）。",
        "again": "上一次的答复没有说明（收到的是：{reply}），这一步仍在等待。请只回「我已核对，算完成」「打回重做」「判这一步失败」中的一句。",
        "failed": "负责人判这一步失败（代码核验没有通过：{why}）。依赖它的步骤不会自动开始。",
        "channel_headline": "「{title}」要操作本机桌面应用，桌面通道还没打开",
        "channel_again": "上一次的答复没有说明（收到的是：{reply}），桌面通道也还没打开，这一步仍在等待。",
        "channel_still_closed": "您选了接着做，但桌面通道还没打开，所以再问一次；这一步没有运行，也没有花费。",
        "declined": "您选了「先不做这一步」：桌面通道没打开，这一步没有做，已标记为未完成；依赖它的步骤不会自动开始。",
        "why": {"no_check": "它没有在桌面上运行完成核验", "channel_closed": "桌面通道没有打开，这一步没有在桌面上做任何事",
                "no_helper": "本机的桌面执行程序还没接上，这一步没有在桌面上做任何事",
                "not_this_turn": "核验不是这一回合做的", "no_work": "这一回合没有在桌面上点过或输入过任何东西",
                "unresolved_outcome": "有一次桌面操作的结果不明，要人先看过", "desktop_unreadable": "读不到桌面现在的样子",
                "app_not_allowed": "前台的程序不在这一步允许的应用里", "no_postconditions": "这一步没有写完成条件",
                "condition_not_met": "有完成条件不成立",
                "held_before_the_work": "完成条件在它动手之前就已成立，证明不了是这一回合做成的",
                "changed_after_check": "核验通过后它又在桌面上做了操作，或完成条件改过了", "": "代码核对没有通过"},
    },
    "en": {
        "header": "Desktop step check",
        "question": "Is this step done?",
        ACCEPT: "I checked it: done",
        CONTINUE: "Send it back to redo",
        FAIL_STEP: "Fail this step",
        "headline": "What “{title}” had to do in a desktop app did not pass the code check: {why}",
        "reason": "What this step had to do in an app on this computer did not pass the code check: {why}. Its result is "
                  "kept as it is; it has not failed and will not run again on its own. Check the computer or the "
                  "deliverable. You can: “I checked it: done” (recorded as your confirmation; no model runs); “Send it "
                  "back to redo” (it runs one more turn and is checked on the desktop again before it may finish); "
                  "“Fail this step” (recorded as not done; nothing that depends on it starts on its own).",
        "again": "The last answer said none of the three (it was: {reply}); this step is still waiting. Answer with exactly one of them.",
        "failed": "The owner failed this step (the code check had not passed: {why}). Nothing that depends on it starts on its own.",
        "channel_headline": "“{title}” works in a desktop app, and the desktop channel is closed",
        "channel_again": "The last answer said neither (it was: {reply}), and the desktop channel is still closed; this step is still waiting.",
        "channel_still_closed": "You chose to go on, but the desktop channel is still closed, so this asks again; the step did not run and nothing was spent.",
        "declined": "You chose not to do this step now: the desktop channel is closed, the step was not done and is marked not done; nothing that depends on it starts on its own.",
        "why": {"no_check": "it never ran the done check on the desktop", "channel_closed": "the desktop channel is closed; nothing was done on the desktop",
                "no_helper": "this computer's desktop helper is not connected; nothing was done on the desktop",
                "not_this_turn": "the check was not made in this turn", "no_work": "nothing was clicked or typed on the desktop in this turn",
                "unresolved_outcome": "an action on the desktop ended with an unknown outcome; a person has to look first",
                "desktop_unreadable": "the desktop could not be read", "app_not_allowed": "the program in front is not one this step may use",
                "no_postconditions": "the step states no done-when conditions", "condition_not_met": "a condition does not hold",
                "held_before_the_work": "the conditions held before it acted, so they prove nothing about this turn",
                "changed_after_check": "it acted on the desktop after the check passed, or the conditions changed", "": "the code check did not pass"},
    },
}


def _desktop_text() -> dict[str, Any]:
    return _DESKTOP_TEXT["zh" if is_zh() else "en"]


def desktop_of(task: Any) -> dict[str, Any] | None:
    """The desktop connector of the step a task runs, or None (``workflow_node.desktop``, written at turn start)."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node")
    desktop = node.get("desktop") if isinstance(node, dict) else None
    return desktop if isinstance(desktop, dict) else None


def desktop_why_zh(reason: str) -> str:
    """Why a desktop check did not pass, in Chinese whatever the console's language: what the model is handed beside
    the reason code, so the code is never what reaches a person."""
    words = dict(_DESKTOP_TEXT["zh"]["why"])
    return words.get(str(reason or ""), words[""])


def _desktop_why(reason: str) -> str:
    words = dict(_desktop_text()["why"])
    return words.get(str(reason or ""), words[""])


def _desktop_wait(task: Any) -> bool:
    """Whether the wait this task holds is a desktop step's (its record says so)."""
    return dict(dict(getattr(task, "metadata", {}) or {}).get(WAIT_KEY, {}) or {}).get("channel") == DESKTOP


def _node_id(task: Any) -> str:
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node")
    return str(node.get("id", "") or "") if isinstance(node, dict) else ""


def _desktop_check(task: Task, desktop: dict[str, Any]) -> tuple[bool, str]:
    """(whether this turn's desktop check stands, the reason code when it does not). Unreadable: it does not stand."""
    try:
        from ampuly.computer_use import desktop_bridge

        stands, why = desktop_bridge.done_standing(task, desktop, _node_id(task))
        if not stands and why == "no_check":
            # No check at all: when the channel is closed (or no helper is bound) that is why, and it is said.
            why = desktop_bridge.gate(task) or why
        return stands, why
    except Exception:  # noqa: BLE001 - a verdict that cannot be read is no verdict: the step waits for a person
        return False, "no_check"


def _carried(task: Task, previous: dict[str, Any], mark: str) -> dict[str, Any]:
    """What the context held before this wait, put back when it ends (the engine overwrites the two keys with this
    card's reply). A reply to this module's own card is not carried: what it carried before is."""
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    earlier = snapshot.get("requested_user_input")
    if isinstance(earlier, dict) and str(earlier.get(mark, "") or "").strip():
        return dict(previous.get("carried", {}) or {})
    return {key: snapshot[key] for key in ("user_supplied_input", "requested_user_input") if snapshot.get(key) not in (None, "", {})}


def _desktop_pause_request(task: Task, why_code: str, *, reply: str = "", kept: dict[str, Any] | None = None) -> dict[str, Any]:
    text = _desktop_text()
    metadata = dict(task.metadata or {})
    previous = dict(metadata.get(WAIT_KEY, {}) or {})
    seq = int(previous.get("seq", 0) or 0) + 1
    carried = _carried(task, previous, QUESTION_ID)
    fresh = kept is not None
    kept = kept if kept is not None else (previous.get("kept") if previous.get("kind") == _DONE_CARD else None)
    why = _desktop_why(why_code)
    task.metadata = {**metadata, WAIT_KEY: {"seq": seq, "channel": DESKTOP, "kind": _DONE_CARD, "why": why, "why_code": why_code,
                                            "waiting": True, "at": datetime.now().isoformat(),
                                            **({"carried": carried} if carried else {}), **({"kept": kept} if kept else {})}}
    reason = text["reason"].format(why=why)
    if reply:
        reason = text["again"].format(reply=reply[:120]) + chr(10) + chr(10) + reason
    elif seq > 1 and not fresh:
        reason = _text()["asked_again"] + chr(10) + chr(10) + reason
    return {
        "requires_user_input": True,
        "reason": reason,
        "headline": text["headline"].format(title=str(task.title or task.id)[:80], why=why),
        QUESTION_ID: why,
        # Whether a step's work on this computer is done is the owner's to say.
        "assignee_employee_id": "owner",
        "request_id": f"connector-done:{task.id}:{seq}",
        "input_questions": [{
            "id": QUESTION_ID, "header": text["header"], "question": text["question"],
            "options": [{"id": key, "label": text[key]} for key in (ACCEPT, CONTINUE, FAIL_STEP)],
            "allow_freeform": False, "required": True,
        }],
        "required_fields": [QUESTION_ID],
    }


def _desktop_as_pause(task: Task, result: TaskResult) -> TaskResult:
    """A desktop step's DONE without this turn's desktop check standing, as a wait for the owner; anything else,
    unchanged. The deliverable the turn wrote -- by whatever tool -- stays with the kept result."""
    desktop = desktop_of(task)
    if desktop is None or is_review_execution_work_item_metadata(dict(task.metadata or {})):
        return result
    stands, why_code = _desktop_check(task, desktop)
    if stands:
        return result
    kept = {"content": str(result.content or ""), "artifacts": dict(result.artifacts or {}),
            "cost": float(result.cost or 0.0), "token_usage": dict(result.token_usage or {})}
    request = _desktop_pause_request(task, why_code, kept=kept)
    artifacts = {key: value for key, value in dict(result.artifacts or {}).items() if key != "pause_request"}
    return replace(result, status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                   artifacts={**artifacts, "pause_request": request})


def _named_option(reply: str, requested: Any, keys: tuple[str, ...], fixed: dict[str, set[str]]) -> str:
    """The ONE option of ``keys`` the whole reply names, or ``UNRECOGNISED``: the option id, the card's own label, or
    the fixed label in either language (the chat card sends "- <header>: <label>", the approval centre the option id).
    Only options the card offered count."""
    import re

    asked = requested if isinstance(requested, dict) else {}
    questions = [item for item in list(asked.get("input_questions", []) or []) if isinstance(item, dict)]
    options = [option for question in questions for option in list(question.get("options", []) or []) if isinstance(option, dict)]
    offered = {str(option.get("id", "") or "") for option in options}
    labels = {str(option.get("id", "") or ""): " ".join(str(option.get("label", "") or "").lower().split()) for option in options}
    headers = [" ".join(str(question.get(key, "") or "").lower().split()) for question in questions for key in ("header", "question", "id")]
    said = " ".join(str(reply or "").lower().split())
    structured = re.search(r"selected_option_id['\"]?\s*[:=]\s*['\"]?([a-z_]+)", said)
    if structured:
        said = structured.group(1)
    else:
        said = said.lstrip("-").strip()
        for header in filter(None, headers):
            for colon in (":", "："):
                if said.startswith(header + colon):
                    said = said[len(header) + 1:].strip()
        said = said.strip(" 。.!！")
    named = [key for key in keys if (not offered or key in offered)
             and said in {key, labels.get(key) or key, *{" ".join(label.lower().split()) for label in fixed.get(key, set())}}]
    return named[0] if len(named) == 1 else UNRECOGNISED


def _note_desktop_decision(task: Task, outcome: str, *, action: str, reason: str, handed_back: bool = False,
                           suggested: str = "", card: str = "") -> None:
    """The owner's answer on a desktop step's card, in the step's decision chain: reviewed by the owner, the choice as
    the outcome (the browser's own vocabulary where it has one, ampuly/computer_use/audit.py). Never raises."""
    try:
        from ampuly.computer_use import audit, desktop_bridge

        model = audit.step_model(dict(task.metadata or {}))
        desktop_bridge.note_decision(task, _node_id(task), {
            "outcome": outcome, "reason": reason, "action": action, "suggested_by": suggested or model,
            "reviewed_by": "owner", "fallback_to": model if handed_back else "none",
        }, card=card)
    except Exception:  # noqa: BLE001 - the owner's answer stands whether or not it could be journaled
        return


def _desktop_take_answer(task: Task) -> tuple[str, str]:
    """``take_answer`` for a desktop step's wait: ``ACCEPT``, ``CONTINUE``, ``FAIL_STEP``, ``UNRECOGNISED`` or ``""``.
    The closed-channel card is not answered here -- ``desktop_channel_hold`` reads it where the turn would start."""
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if wait.get("kind") != _DONE_CARD:
        return "", ""
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    requested = snapshot.get("requested_user_input")
    if not isinstance(requested, dict) or not str(requested.get(QUESTION_ID, "") or "").strip() or requested.get(CHANNEL_MARK):
        return (UNRECOGNISED, "") if wait.get("waiting") else ("", "")
    reply = str(snapshot.pop("user_supplied_input", "") or "").strip()
    snapshot.pop("requested_user_input", None)
    snapshot.update(dict(wait.get("carried", {}) or {}))
    task.context_snapshot = snapshot
    fixed = {key: {str(_DESKTOP_TEXT[lang][key]) for lang in _DESKTOP_TEXT} for key in (ACCEPT, CONTINUE, FAIL_STEP)}
    answer = _named_option(reply, requested, (ACCEPT, CONTINUE, FAIL_STEP), fixed)
    now = datetime.now().isoformat()
    kept_off = {k: v for k, v in wait.items() if k not in {"carried", "kept"}}
    if answer == ACCEPT:
        task.metadata = {**metadata, WAIT_KEY: {**{k: v for k, v in wait.items() if k != "carried"}, "waiting": False,
                                                ACCEPTED_BY_OWNER: now}}
    elif answer == CONTINUE:
        task.metadata = {**metadata, WAIT_KEY: {**kept_off, "waiting": False}}
    elif answer == FAIL_STEP:
        task.metadata = {**metadata, WAIT_KEY: {**kept_off, "waiting": False, FAILED_BY_OWNER: now}}
    if answer in (ACCEPT, CONTINUE, FAIL_STEP):
        outcome = {ACCEPT: "owner_accepted_done", CONTINUE: "owner_continued", FAIL_STEP: "owner_failed_step"}[answer]
        _note_desktop_decision(task, outcome, action=answer, reason=str(wait.get("why_code", "") or ""),
                               handed_back=answer == CONTINUE, card=f"connector-done:{task.id}:{int(wait.get('seq', 0) or 0)}")
    return answer, reply


def failed_by_owner(task: Task) -> TaskResult:
    """The owner answered 「判这一步失败」: the step ends failed, with their word in it. No model runs."""
    wait = dict(dict(task.metadata or {}).get(WAIT_KEY, {}) or {})
    note = _desktop_text()["failed"].format(why=str(wait.get("why", "") or _desktop_why("")))
    return TaskResult(status=TaskStatus.FAILED, content=note, artifacts={"connector_done": FAILED_BY_OWNER})


def _desktop_park_again(task: Task, reply: str) -> TaskResult:
    wait = dict(dict(task.metadata or {}).get(WAIT_KEY, {}) or {})
    if wait.get("kind") == CHANNEL_MARK:
        return _channel_pause(task, reply=reply)
    request = _desktop_pause_request(task, str(wait.get("why_code", "") or "no_check"), reply=reply)
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"], artifacts={"pause_request": request})


def _channel_pause(task: Task, *, reply: str = "", still_closed: bool = False) -> TaskResult:
    """The closed-channel card (desktop_connector.stop_request), raised by the kernel before the step's turn: the
    same words the step's own tools would stop with, on this module's rail and record."""
    from ampuly.computer_use import desktop_bridge
    from ampuly.plugins.office_ui.desktop_connector import stop_request

    text = _desktop_text()
    metadata = dict(task.metadata or {})
    previous = dict(metadata.get(WAIT_KEY, {}) or {})
    seq = int(previous.get("seq", 0) or 0) + 1
    carried = _carried(task, previous, CHANNEL_MARK)
    was_waiting = previous.get("kind") == CHANNEL_MARK and bool(previous.get("waiting"))
    why = _desktop_why(desktop_bridge.STOP_CHANNEL_CLOSED)
    task.metadata = {**metadata, WAIT_KEY: {"seq": seq, "channel": DESKTOP, "kind": CHANNEL_MARK, "why": why,
                                            "why_code": desktop_bridge.STOP_CHANNEL_CLOSED, "waiting": True,
                                            "at": datetime.now().isoformat(), **({"carried": carried} if carried else {})}}
    card = stop_request(desktop_bridge.STOP_CHANNEL_CLOSED)
    reason = str(card.get("reason", "") or "")
    if reply:
        reason = text["channel_again"].format(reply=reply[:120]) + chr(10) + chr(10) + reason
    elif still_closed:
        reason = text["channel_still_closed"] + chr(10) + chr(10) + reason
    elif was_waiting:
        reason = _text()["asked_again"] + chr(10) + chr(10) + reason
    question = [str(item.get("id", "") or "") for item in list(card.get("input_questions", []) or []) if isinstance(item, dict)]
    request = {**card, "reason": reason, CHANNEL_MARK: desktop_bridge.STOP_CHANNEL_CLOSED, QUESTION_ID: why,
               "headline": text["channel_headline"].format(title=str(task.title or task.id)[:80]),
               # Only the owner can open the channel.
               "assignee_employee_id": "owner", "request_id": f"desktop-channel:{task.id}:{seq}",
               "required_fields": [item for item in question if item]}
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"], artifacts={"pause_request": request})


def desktop_channel_hold(task: Task) -> TaskResult | None:
    """Where a work item is about to run a turn: a desktop step whose channel is closed does not run.

    ``None``: the turn may run (not a desktop step, not a company work item, or the channel is open -- a closed-channel
    wait it held is then over). Otherwise, no model is called and the caller settles what this returns:

    - the closed-channel card (a wait for the owner): the first time, again while the channel stays closed, and again
      for a reply that named neither choice or for a release without an answer;
    - a failed result, when the owner answered 「先不做这一步」: the step is marked not done (what depends on it does
      not start on its own), and the answer is journaled in the step's decision chain.
    """
    if desktop_of(task) is None or connector_of(task) is not None or not waits_for_a_person(task):
        return None
    metadata = dict(task.metadata or {})
    if is_review_execution_work_item_metadata(metadata):
        return None
    from ampuly.computer_use import desktop_bridge

    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    ours = wait.get("channel") == DESKTOP and wait.get("kind") == CHANNEL_MARK
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    requested = snapshot.get("requested_user_input")
    answered = isinstance(requested, dict) and bool(str(requested.get(CHANNEL_MARK, "") or "").strip())
    reply, answer = "", ""
    if answered:
        # The owner's words to this card are not input for the model; what the context held before comes back.
        reply = str(snapshot.pop("user_supplied_input", "") or "").strip()
        snapshot.pop("requested_user_input", None)
        snapshot.update(dict(wait.get("carried", {}) or {}))
        task.context_snapshot = snapshot
        labels = {key: set() for key in (CHANNEL_SKIP, CHANNEL_RESUME)}
        from ampuly.plugins.office_ui.desktop_connector import _STOP_TEXT

        for option in _STOP_TEXT[desktop_bridge.STOP_CHANNEL_CLOSED]["options"]:
            if isinstance(option, dict) and option.get("id") in labels:
                labels[str(option["id"])].add(str(option.get("label", "") or ""))
        answer = _named_option(reply, requested, (CHANNEL_SKIP, CHANNEL_RESUME), labels)
    seq = int(wait.get("seq", 0) or 0)
    if answer == CHANNEL_SKIP:
        task.metadata = {**metadata, WAIT_KEY: {"seq": seq, "channel": DESKTOP, "kind": CHANNEL_MARK, "waiting": False,
                                                DECLINED_BY_OWNER: datetime.now().isoformat()}}
        _note_desktop_decision(task, "owner_declined_step", action=CHANNEL_SKIP, reason=desktop_bridge.STOP_CHANNEL_CLOSED,
                               suggested="none", card=f"desktop-channel:{task.id}:{seq}")
        return TaskResult(status=TaskStatus.FAILED, content=_desktop_text()["declined"],
                          artifacts={"connector_done": DECLINED_BY_OWNER})
    if desktop_bridge.channel_open(task):
        if ours:
            task.metadata = {**metadata, WAIT_KEY: {"seq": seq, "waiting": False}}
        return None
    if answered and answer == UNRECOGNISED:
        return _channel_pause(task, reply=reply)
    return _channel_pause(task, still_closed=answer == CHANNEL_RESUME)
