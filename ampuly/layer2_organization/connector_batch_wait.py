"""A batch step ends with ONE card that lists only its exceptions -- or with no card at all (批量自动化 P4).

A step the owner set to batch mode works a list from the order, row by row, and each row is judged on its own
(``browser_decide_act(item=…)`` → a terminal ledger row, ampuly/computer_use/browser_bridge.py). This module reads
that ledger where a turn's result arrives:

- every row verified → the step completes, **and nothing is raised**. 「全部核对通过，直接完成，不弹卡」;
- anything else → one card per step, listing only the rows that are not verified, with three whole-sentence
  options. Not one card per row: that is the whole point of the phase.

An item the ledger has no row for counts as 结果不明 and is listed. Never as passed -- a row lost to a hard kill
must cost a person one look, never a silent success.

It rides the same ``task_user_input`` rail as :mod:`connector_done_wait` and the budget stop: same card shape,
same durable checkpoint, same restart behaviour, its own key and its own question id so an answer to one is never
read as an answer to the other.

**Ledger rows are not verdicts.** A verdict is measured against the turn it was made in (``TURN_KEY``) and dies
with that turn; a row records what was true of one item when that item closed, and it stays true across turns.
That distinction is the one new rule this phase introduces, and it is recorded in runtime contract §八.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.i18n_runtime import is_zh
from ampuly.layer2_organization.budget_stop_wait import waits_for_a_person
from ampuly.layer2_organization.connector_done_wait import connector_of
from ampuly.layer2_organization.phase import is_review_execution_work_item_metadata

WAIT_KEY = "connector_batch_wait"
QUESTION_ID = "connector_batch"
ACCEPT = "accept_exceptions"
REVIEW = "review_each"
FAIL = "fail_exceptions"
UNRECOGNISED = "unrecognised"
ANSWERED_BY_OWNER = "answered_by_owner"
# The console shows at most three options and lettered A/B/C; a fourth would be dropped without a word.
_OPTIONS = (ACCEPT, REVIEW, FAIL)
# Which answers finish the step with the kept result rather than running the turn again. 「例外判失败，其余
# 完成」 finishes too: the exceptions are recorded as such in the ledger, and re-running would redo the rows
# that already passed. 「逐条审批」 runs the step again, so it is not here.
COMPLETING = (ACCEPT, FAIL)
# How many exception rows the card spells out. Past it the count is stated and the rest are in the delivery file:
# a card nobody can read is not a card.
ROWS_SHOWN = 20
MISSING = "missing"


_TEXT = {
    "zh": {
        "header": "批量步骤例外",
        "question": "这一批里有几条没通过，怎么处理？",
        ACCEPT: "接受这些例外并结单",
        REVIEW: "逐条审批这些例外",
        FAIL: "例外判失败，其余完成",
        "headline": "「{title}」这一批 {total} 条里有 {bad} 条没通过",
        "reason": "这一步按条目做完了 {total} 条，其中 {good} 条由代码核验通过，{bad} 条没有。下面只列没通过的：\n\n{rows}\n\n"
                  "已通过的那些不会因为这几条而作废，结果都在交付里。请选一项："
                  "「接受这些例外并结单」把这几条按现状记下并完成这一步；"
                  "「逐条审批这些例外」把这几条退回去按今天的规矩逐条处理；"
                  "「例外判失败，其余完成」把这几条判为失败，其余算完成。",
        "again": "上一次的答复没有说明（收到的是：{reply}），这一步仍在等待。请只回上面三句中的一句。",
        "asked_again": "这一步在没有收到回答的情况下被重新放行（卡片丢失、重启，或回答没有送达），所以再问一次；没有运行，也没有花费。",
        "more": "……另有 {n} 条未在此列出，完整清单见交付。",
        "answered": "owner 已处理这一批的例外（{answer}）。",
        "row": "- **{item}**：{why}",
        "answers": {ACCEPT: "接受例外并结单", REVIEW: "退回逐条审批", FAIL: "例外判失败，其余完成"},
        "why": {MISSING: "没有台账行，结果不明", "": "没通过"},
    },
    "en": {
        "header": "Batch exceptions",
        "question": "Some rows in this batch did not pass. What now?",
        ACCEPT: "Accept these exceptions and finish",
        REVIEW: "Approve these exceptions one by one",
        FAIL: "Fail the exceptions, finish the rest",
        "headline": "{bad} of {total} rows in “{title}” did not pass",
        "reason": "This step worked {total} rows; {good} were verified by code and {bad} were not. Only the ones that "
                  "did not pass are listed:\n\n{rows}\n\nThe verified rows are not cancelled by these, and every "
                  "result is in the delivery. Choose one: accept them and finish the step; send them back to be "
                  "approved one by one the way every action is today; or fail them and finish the rest.",
        "again": "The last reply did not say which (it read: {reply}); this step is still waiting. Reply with one of "
                 "the three sentences above.",
        "asked_again": "This step was released again without an answer (a lost card, a restart, or a reply that never "
                       "arrived), so it asks once more; nothing ran and nothing was spent.",
        "more": "… and {n} more, not listed here; the full list is in the delivery.",
        "answered": "The owner handled this batch's exceptions ({answer}).",
        "row": "- **{item}**: {why}",
        "answers": {ACCEPT: "accepted and finished", REVIEW: "sent back for one-by-one approval",
                    FAIL: "failed the exceptions, finished the rest"},
        "why": {MISSING: "no ledger row; the outcome is unknown", "": "did not pass"},
    },
}


def _text() -> dict[str, Any]:
    return _TEXT["zh" if is_zh() else "en"]


def handles(task: Any) -> bool:
    """Whether this module owns the step's completion card. A batch step's rows say it; the step-level card does not
    apply to one, and :mod:`connector_done_wait` steps aside for it."""
    try:
        from ampuly.computer_use import browser_bridge

        return bool(browser_bridge.batch_step(task))
    except Exception:  # noqa: BLE001 - unreadable: not ours, and the step-level card still applies
        return False


def ledger(task: Any) -> list[dict[str, Any]]:
    """One row per row of the ORDER'S list, in the order's own order.

    Built from the list, never from the ledger: an item the ledger has no row for still appears, as ``missing``.
    Reading it the other way round would let a lost row disappear, and a row that disappears reads as a row that
    never needed doing.
    """
    try:
        from ampuly.computer_use import browser_bridge
        from ampuly.layer4_tools.browser_sessions import BrowserBinding

        rows = {str(row.get("item", "") or ""): row for row in browser_bridge.item_results(BrowserBinding.for_task(task))}
        items = browser_bridge.batch_items(task) or []
        field = browser_bridge.batch_item_key(task)
    except Exception:  # noqa: BLE001 - a ledger that cannot be read leaves the step to the person
        return []
    if not items or not field:
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get(field, "") or "").strip()
        found = rows.get(key)
        out.append(dict(found) if found else {"item": key, "status": MISSING, "reason": MISSING})
    return out


def exceptions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows a person has to look at. Only a code verdict of ``verified`` keeps a row off this list -- page
    content can push a row onto it and never off it (design §3.4)."""
    return [row for row in rows if str(row.get("status", "") or "") != "verified"]


def _reason_words() -> dict[str, str]:
    """Every done-check reason in the owner's words, shared with the step-level card so one vocabulary serves both."""
    from ampuly.layer2_organization.connector_done_wait import _TEXT as DONE_TEXT

    return {**dict(DONE_TEXT["zh" if is_zh() else "en"]["why"]), MISSING: dict(_text()["why"])[MISSING]}


def write_delivery(task: Any, rows: list[dict[str, Any]]) -> Any:
    """Write the ledger into the step's declared deliverable, and never fail the step over it.

    In batch mode CODE writes this file, not the model: the model's own write is a whole-file replace, so a second
    item's write would silently clobber the first's. The file is a re-render of the journal's rows, so losing it
    loses nothing -- the next turn writes it again. A refusal from the order's own write fence means no file and
    no exception: the rows are still in the journal and the card still raises.
    """
    try:
        from ampuly.core import item_ledger
        from ampuly.core.order_layout import order_area_of, order_write_decision
        from ampuly.layer2_organization.node_contract import task_write_roots

        connector = connector_of(task) or {}
        relative = str(dict(connector.get("output", {}) or {}).get("path", "") or "").strip()
        area = order_area_of(getattr(task, "project_id", ""), dict(getattr(task, "metadata", {}) or {}))
        if not relative or area is None:
            return None
        # Re-checked here rather than trusted: this runs outside the model's tool path, so outside the fence the
        # file tools apply. The area is rebuilt from the workplace, never taken from a metadata string.
        if order_write_decision(area, [relative], task_write_roots(task)) is not None:
            return None
        text = item_ledger.render(rows, order_id=str(dict(getattr(task, "metadata", {}) or {}).get("order_id", "") or ""),
                                  step=str(connector.get("goal", "") or "")[:80], why=_reason_words())
        return item_ledger.publish(text, area.output_root / relative)
    except Exception:  # noqa: BLE001 - a ledger file that cannot be written is not a reason to fail the batch
        return None


def _why_row(row: dict[str, Any]) -> str:
    text = _text()
    reasons = dict(text["why"])
    reason = str(row.get("reason", "") or "")
    if reason == MISSING or str(row.get("status", "") or "") == MISSING:
        return reasons[MISSING]
    # The step-level card already speaks every done-check reason; a batch row means the same things.
    return _reason_words().get(reason, reason or reasons[""])


def _rows_markdown(bad: list[dict[str, Any]]) -> str:
    text = _text()
    shown = [text["row"].format(item=str(row.get("item", "") or "")[:80], why=_why_row(row)) for row in bad[:ROWS_SHOWN]]
    if len(bad) > ROWS_SHOWN:
        shown.append(text["more"].format(n=len(bad) - ROWS_SHOWN))
    return chr(10).join(shown)


def _pause_request(task: Task, rows: list[dict[str, Any]], *, reply: str = "",
                   kept: dict[str, Any] | None = None) -> dict[str, Any]:
    text = _text()
    metadata = dict(task.metadata or {})
    previous = dict(metadata.get(WAIT_KEY, {}) or {})
    seq = int(previous.get("seq", 0) or 0) + 1
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    earlier = snapshot.get("requested_user_input")
    if isinstance(earlier, dict) and str(earlier.get(QUESTION_ID, "") or "").strip():
        carried = dict(previous.get("carried", {}) or {})
    else:
        carried = {key: snapshot[key] for key in ("user_supplied_input", "requested_user_input")
                   if snapshot.get(key) not in (None, "", {})}
    bad = exceptions(rows)
    fresh = kept is not None
    kept = kept if kept is not None else previous.get("kept")
    task.metadata = {**metadata, WAIT_KEY: {
        "seq": seq, "waiting": True, "at": datetime.now().isoformat(),
        "total": len(rows), "bad": [str(row.get("item", "") or "") for row in bad][:ROWS_SHOWN],
        "bad_count": len(bad),
        **({"carried": carried} if carried else {}), **({"kept": kept} if kept else {})}}
    reason = text["reason"].format(total=len(rows), good=len(rows) - len(bad), bad=len(bad), rows=_rows_markdown(bad))
    if reply:
        reason = text["again"].format(reply=reply[:120]) + chr(10) + chr(10) + reason
    elif seq > 1 and not fresh:
        reason = text["asked_again"] + chr(10) + chr(10) + reason
    return {
        "requires_user_input": True,
        "reason": reason,
        "headline": text["headline"].format(title=str(task.title or task.id)[:80], total=len(rows), bad=len(bad)),
        QUESTION_ID: str(len(bad)),
        # Whether a batch's exceptions are acceptable is the owner's to say.
        "assignee_employee_id": "owner",
        "request_id": f"connector-batch:{task.id}:{seq}",
        "input_questions": [{
            "id": QUESTION_ID, "header": text["header"], "question": text["question"],
            "options": [{"id": key, "label": text[key]} for key in _OPTIONS],
            "allow_freeform": False, "required": True,
        }],
        "required_fields": [QUESTION_ID],
    }


def as_pause(task: Task, result: TaskResult) -> TaskResult | None:
    """A batch step's DONE, or ``None`` when this module has nothing to say.

    Three answers, and the caller must be able to tell them apart:

    * ``None`` -- not a batch step, or its order carries no list at all. The step-level card applies as it always
      has. Returning the result unchanged here would COMPLETE the step instead, skipping the check it still owes.
    * ``result`` unchanged -- every row passed. 「全部核对通过，直接完成，不弹卡」.
    * a parked result -- one card listing only the exceptions.

    A step whose order DOES carry a list but whose ledger is empty is the third case, not the first: every row is
    reported 结果不明. That says which rows nobody accounted for, which the step-level card cannot, and it is the
    safe direction -- not one of them is counted as passed.
    """
    if getattr(result, "status", None) != TaskStatus.DONE or connector_of(task) is None or not handles(task):
        return None
    if not waits_for_a_person(task) or is_review_execution_work_item_metadata(dict(task.metadata or {})):
        return None
    rows = ledger(task)
    if not rows:
        return None
    # Written on both endings: 「结果写进交付」 is owed whether or not anybody has to look at a card.
    write_delivery(task, rows)
    if not exceptions(rows):
        return result   # 全部核对通过，直接完成，不弹卡
    kept = {"content": str(result.content or ""), "artifacts": dict(result.artifacts or {}),
            "cost": float(result.cost or 0.0), "token_usage": dict(result.token_usage or {})}
    request = _pause_request(task, rows, kept=kept)
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
    """The ONE option the whole reply names, or ``UNRECOGNISED``.

    Three options rather than two, so it counts instead of comparing: a reply that names two of them names none.
    """
    import re

    questions = [item for item in list(requested.get("input_questions", []) or []) if isinstance(item, dict)]
    labels = {str(option.get("id", "") or ""): " ".join(str(option.get("label", "") or "").lower().split())
              for question in questions for option in list(question.get("options", []) or []) if isinstance(option, dict)}
    headers = [" ".join(str(question.get(key, "") or "").lower().split())
               for question in questions for key in ("header", "question", "id")]
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
    named = []
    for key in _OPTIONS:
        fixed = {" ".join(str(_TEXT[lang][key]).lower().split()) for lang in _TEXT}
        if said in {key, labels.get(key) or key, *fixed}:
            named.append(key)
    return named[0] if len(named) == 1 else UNRECOGNISED


def take_answer(task: Task) -> tuple[str, str]:
    """``(answer, reply)`` for a work item about to run again; ``""`` when this run has nothing to do with the wait.

    A waiting item that runs again WITHOUT an answer is ``UNRECOGNISED`` -- asked again, never run.
    """
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    requested = snapshot.get("requested_user_input")
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if not isinstance(requested, dict) or not str(requested.get(QUESTION_ID, "") or "").strip():
        return (UNRECOGNISED, "") if wait.get("waiting") else ("", "")
    reply = str(snapshot.pop("user_supplied_input", "") or "").strip()
    snapshot.pop("requested_user_input", None)
    snapshot.update(dict(wait.get("carried", {}) or {}))
    task.context_snapshot = snapshot
    answer = _named_answer(reply, requested)
    if answer in {ACCEPT, FAIL}:
        task.metadata = {**metadata, WAIT_KEY: {**{k: v for k, v in wait.items() if k != "carried"},
                                                "waiting": False, ANSWERED_BY_OWNER: answer}}
    elif answer == REVIEW:
        # Back to today's rules for those rows: the kept result is dropped, and the step runs again.
        task.metadata = {**metadata, WAIT_KEY: {**{k: v for k, v in wait.items() if k not in {"carried", "kept"}},
                                                "waiting": False, ANSWERED_BY_OWNER: answer}}
    if answer in _OPTIONS:
        # LK-1 (runtime contract): the owner's answer is a human review in the step's decision
        # chain, as on the single-step card -- not only a metadata mark the commit deletes. Accept: 「已核对，算完成」;
        # fail the exceptions: its own word, ``owner_failed_exceptions`` (the step finishes); review each: continued
        # (the step runs again). Once per card.
        from ampuly.layer2_organization.connector_done_wait import _note_owner_review

        card = f"connector-batch:{task.id}:{int(wait.get('seq', 0) or 0)}"
        _note_owner_review(task, accepted=answer == ACCEPT, card=card,
                           outcome="owner_failed_exceptions" if answer == FAIL else "")
    return answer, reply


def kept_result_of(task: Task) -> TaskResult | None:
    """The finished result the owner accepted, with their decision said in it; ``None`` when there is none.

    Reading does not remove it -- the same lesson as the step-level wait: removed on reading, a restart between
    the answer and the commit left nothing to complete with and paid for a turn nobody asked for.
    """
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    kept = wait.get("kept")
    answer = str(wait.get(ANSWERED_BY_OWNER, "") or "")
    if not isinstance(kept, dict) or wait.get("waiting") or answer not in {ACCEPT, FAIL}:
        return None
    text = _text()
    note = text["answered"].format(answer=dict(text["answers"]).get(answer, answer))
    content = str(kept.get("content", "") or "")
    result = TaskResult(status=TaskStatus.DONE, content=(content + chr(10) + chr(10) + note) if content else note,
                        artifacts={**dict(kept.get("artifacts", {}) or {}), "connector_batch": answer},
                        cost=float(kept.get("cost", 0.0) or 0.0), token_usage=dict(kept.get("token_usage", {}) or {}))
    task.result = {"content": result.content, "artifacts": dict(result.artifacts)}
    return result


def clear_kept(task: Task) -> None:
    """The step is being committed done: the kept result has served."""
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if "kept" in wait or ANSWERED_BY_OWNER in wait:
        task.metadata = {**metadata, WAIT_KEY: {k: v for k, v in wait.items() if k not in {"kept", ANSWERED_BY_OWNER}}}


def park_again(task: Task, reply: str) -> TaskResult:
    """The same wait, asked again, for an answer that named none of the three."""
    request = _pause_request(task, ledger(task), reply=reply)
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                      artifacts={"pause_request": request})
