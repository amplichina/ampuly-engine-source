"""A work item the order's allowance stopped waits for a person; it does not fail and it is not retried.

Under a bound budget a model call the allowance refuses -- or one whose paid result nobody knows -- ends the turn as
an ordinary failed result carrying a mark (``ampuly.llm.budget.BUDGET_STOP_ARTIFACT``). The engine used to retry such a
result like any other failure: every retry was refused before dispatch (no money lost), the retries were spent, an
``uncertain`` stop was overwritten by the ``unavailable`` that the now-held budget answered on the second attempt, the
work item failed and everything depending on it was doomed. None of that needed to happen: the work was fine, the
order needed a person.

This module is the whole of that decision, kept out of the kernel files: the kernel calls ``as_pause`` where a turn's
result arrives and ``take_answer`` where a work item is about to run again. The wait itself is the existing
``task_user_input`` rail (the one ``request_user_input`` and human nodes use), so the card, the durable checkpoint,
the restart behaviour and the way an answer reaches the item are the ones already in production.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.i18n_runtime import is_zh
from ampuly.layer2_organization.phase import (
    is_report_execution_work_item_metadata,
    is_review_execution_work_item_metadata,
    is_runtime_auxiliary_work_item,
)
from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task
from ampuly.layer2_organization.work_item_runtime import is_work_item_runtime_metadata
from ampuly.llm.budget import BUDGET_STOP_ARTIFACT

WAIT_KEY = "budget_stop_wait"
QUESTION_ID = "budget_stop"
CONTINUE = "continue"
FAIL = "fail_item"
UNRECOGNISED = "unrecognised"
FAILED_BY_OWNER_REASON = "budget_stop_failed_by_owner"

# What a reply may say for each answer. A reply reaches the work item as text only: the chat card sends
# "- <header>: <label>", the approval centre sends the option id, a channel sends whatever the person typed. Anything
# that names neither answer -- or both -- parks the item again: "approve" must not run a turn and "deny" must not fail
# finished upstream work by guesswork.
_SAYS_CONTINUE = ("continue", "继续")
_SAYS_FAIL = ("fail_item", "fail this item", "判这一项失败", "判失败")

_TEXT = {
    "zh": {
        "header": "订单预算",
        "question": "怎么处理这一项？",
        CONTINUE: "继续这一项",
        FAIL: "判这一项失败",
        "exhausted": "订单预算用尽了。这一项已停下来等您，没有判失败，也不会自动重试。请在订单行上点「提高上限」，"
                     "再选「继续这一项」（若改为「取消上限」再继续，订单照样逐笔记账，只是不再受上限约束）；也可以直接判这一项失败。",
        "unavailable": "这一项所用的订单预算已被关闭或挂起，模型调用在发出前就被拒绝。这一项已停下来等您，没有判失败。"
                       "为订单重新发放预算（或先处理挂起的那次调用）后，选「继续这一项」；也可以判这一项失败。",
        "uncertain": "一次已经发出的付费模型调用没有拿到明确结果，订单预算已被挂起待核实。这一项已停下来等您，没有判失败。"
                     "请先在订单行上说明那次调用的结果，预算恢复后再选「继续这一项」；也可以判这一项失败。"
                     "注意：继续会重跑这一回合，回合里在那次调用之前已经完成的动作可能被重复。",
        "refused": "订单预算拒绝了这次模型调用（预算记录缺失、范围不符，或设置了尚不支持的金额上限）。这一项已停下来等您，没有判失败。"
                   "修正订单的预算后，选「继续这一项」；也可以判这一项失败。",
        "detail": "预算编号：{budget_id}；预算所属订单：{order_id}。拒绝原文：{detail}",
        "again": "上一次的答复没有说明是继续还是判失败（收到的是：{reply}），这一项仍在等待。请只回「继续这一项」或「判这一项失败」。",
        "asked_again": "这一项在没有收到回答的情况下被重新放行（卡片丢失、重启，或回答没有送达），所以再问一次；没有运行，也没有花费。",
        "failed": "订单预算拦下了这一项，owner 判定这一项失败（不是模型或产出的问题）。",
        "failed_route_refused": "这一项的模型调用在发出前被拒绝（密钥没有设置、模型不在目录里或暂不可用），owner 判定这一项失败。",
        "failed_outcome_unknown": "这一项的一次模型调用没有收到回答、结果不明，没有自动重发；owner 判定这一项失败。",
        "failed_external_failed": "这一项交给的外部代理没有完成，没有换人重做；owner 判定这一项失败。",
        "failed_ledger_unavailable": "预算账本读写不了，模型调用没有发出；owner 判定这一项失败（不是预算用尽）。",
        "headline": "订单预算拦下了{title}：{what}",
        "review": "被拦下的是一张【评审卡】（对另一项产出的评审），被评审的产出已完成并原样保留。这张卡只有「继续这一项」一个回答：评审没有做，就不能当作通过，也不能替您判被评审的那一项失败；不想继续评审，请取消订单。",
        "report": "被拦下的是这一步的【汇报】：活已经干完，产出原样保留，只差把结果整理成汇报交给评审。这张卡只有「继续这一项」一个回答："
                  "汇报没写成，不代表这一步做坏了，所以不会因此判这一步失败；不想继续，请取消订单。",
        "tool": "被拦下的是一次付费工具调用（{resource}），不是模型调用：在任何请求发出之前就被订单预算拒绝了，没有花费。",
        "resources": {"image": "出图", "model_call": "模型调用", "retry": "重试"},
        "judge": "被拦下的是这一项的【闸门裁判】，不是这一项的工作：产出已经完成并原样保留。「继续这一项」不会重做这一项，只重新裁判一次。",
        "route_refused": "这次模型调用在发出之前就被拒绝了：没有发出任何请求，没有花费，订单预算也没有被挂起。"
                         "常见原因：所用模型的密钥没有设置、模型不在模型目录里，或这个模型现在不可用。"
                         "请在「配置 → 模型」里设置密钥或换一个目录里的模型，然后选「继续这一项」；也可以判这一项失败。",
        "outcome_unknown": "一次已经发出的模型调用没有收到回答（超时、断线或服务端出错），结果不明。系统不会自动重发，"
                           "以免同一次调用付两次钱。这一项已停下来等您，没有判失败。选「继续这一项」会重跑这一回合"
                           "（回合里在那次调用之前已经完成的动作可能被重复）；也可以判这一项失败。",
        "external_failed": "这一项交给的外部代理没有完成（失败、被您拒绝，或者现在不可用）。订单里的工作不会自动换另一个代理，"
                           "也不会改由内置代理重做，以免同一项工作做两遍、付两份钱。这一项已停下来等您，没有判失败。"
                           "选「继续这一项」会让同一个代理再做一次；也可以判这一项失败。",
        "ledger_unavailable": "预算账本这会儿读写不了（数据库正忙或打不开），这次模型调用在发出之前就停下了：没有发出请求，"
                              "也没有花费。这不是预算用尽，也不是预算被关闭。稍后选「继续这一项」即可；也可以判这一项失败。",
        "review_unreadable": "评审连续几次都没有给出能读懂的结论（通过还是返工）。评审没做成，就不能当作通过，所以没有放行；"
                             "干活的一方也没有因此被要求返工。选「继续这一项」会让评审再做一次。",
        "review_header": "评审",
        "review_headline": "{title}没有给出能读懂的结论，没有放行",
        "ledger_header": "预算账本",
        "ledger_headline": "{title}的模型调用因预算账本暂时读写不了而没有发出",
        "external_header": "外部代理",
        "external_headline": "{title}的外部代理没有完成，没有自动换人",
        "route_header": "模型调用",
        "route_headline": "{title}的模型调用在发出前被拒绝",
        "outcome_headline": "{title}的一次模型调用没有收到回答，没有自动重发",
        "report_outcome_unknown": "这一轮汇报的模型调用结果未知（请求已经发出，但没有收到回答），可能已计费。系统没有自动重试，"
                                  "以免同一次调用付两次钱；这一轮也不算汇报失败。要重试这一轮汇报，请选「继续这一项」；"
                                  "汇报不能跳过（评审要看它），不想重试请取消订单。",
        "report_outcome_headline": "{title}的模型调用结果未知，可能已计费，没有自动重试",
        "what": {"exhausted": "预算用尽", "unavailable": "预算已关闭或被挂起", "uncertain": "一次调用结果不明，预算被挂起待核实",
                 "refused": "预算拒绝了这次调用", "route_refused": "调用在发出前被拒绝，没有花费",
                 "outcome_unknown": "一次调用没有收到回答，没有自动重发",
                 "external_failed": "外部代理没有完成，没有自动换人",
                 "ledger_unavailable": "预算账本暂时读写不了，调用没有发出",
                 "review_unreadable": "评审没有给出能读懂的结论，没有放行"},
    },
    "en": {
        "header": "Order allowance",
        "question": "What should happen to this work item?",
        CONTINUE: "Continue this item",
        FAIL: "Fail this item",
        "exhausted": "The order's allowance is used up. This work item has stopped and is waiting for you; it has not "
                     "failed and will not be retried on its own. Use “Raise ceiling” on the order row, then choose "
                     "“Continue this item” (lifting the ceilings instead runs this item without one -- every call is "
                     "still recorded) -- or fail this item.",
        "unavailable": "The allowance this work item spends from is closed or held, so the model call was refused before "
                       "it was sent. The item has stopped and is waiting for you; it has not failed. Grant the order a "
                       "new allowance (or settle the held call first), then choose “Continue this item” -- or fail it.",
        "uncertain": "A paid model call that had already been sent ended without a known result, and the order's "
                     "allowance is now held for verification. The item has stopped and is waiting for you; it has not "
                     "failed. Say on the order row what happened to that call; once the allowance is released choose "
                     "“Continue this item” -- or fail it. Note: continuing runs this turn again, and whatever the turn "
                     "had finished before that call may be repeated.",
        "refused": "The order's allowance refused this model call (no budget record, a scope that does not match, or a "
                   "money ceiling that cannot be enforced yet). The item has stopped and is waiting for you; it has not "
                   "failed. Correct the order's allowance, then choose “Continue this item” -- or fail it.",
        "detail": "Budget: {budget_id}; it belongs to order: {order_id}. The refusal, verbatim: {detail}",
        "again": "The last answer said neither continue nor fail (it was: {reply}); this item is still waiting. Answer with exactly one of the two choices.",
        "asked_again": "This item was released without an answer (a lost card, a restart, or an answer that never arrived), so it is asked again; nothing ran and nothing was spent.",
        "failed": "The order's allowance stopped this work item and the owner failed it (not a fault of the model or the work).",
        "failed_route_refused": "This item's model call was refused before it was sent (a key not set, a model not in the catalogue or not available); the owner failed it.",
        "failed_outcome_unknown": "A model call of this item got no answer and was not sent again; the owner failed the item.",
        "failed_external_failed": "The external agent this item was given did not finish it, and no other one was tried; the owner failed it.",
        "failed_ledger_unavailable": "The budget ledger could not be read or written, so the model call was not sent; the owner failed the item (the allowance was not used up).",
        "headline": "The order's allowance stopped {title}: {what}",
        "review": "What was stopped is a REVIEW card (the review of another item's output); the reviewed output is finished and kept. This card has one answer, “Continue this item”: a review that did not happen cannot count as a pass, and it cannot fail the reviewed item on your behalf either. To stop instead, cancel the order.",
        "report": "What was stopped is this step's REPORT: the work is done and its output is kept; only the report that "
                  "hands it to the reviewer is missing. This card has one answer, “Continue this item”: a report that "
                  "was not written does not mean the step went wrong, so the step is not failed for it. To stop "
                  "instead, cancel the order.",
        "tool": "What was refused is a paid tool call ({resource}), not a model call: the order's allowance said no before anything was sent, and nothing was spent.",
        "resources": {"image": "images", "model_call": "model calls", "retry": "retries"},
        "judge": "What was stopped is this item's GATE JUDGE, not its work: the output is finished and kept as it is. “Continue this item” does not redo the item; it only judges it again.",
        "route_refused": "This model call was refused before it was sent: no request left, nothing was spent and the "
                         "order's allowance is not held. Usually the model's key is not set, the model is not in the "
                         "catalogue, or the model is not available right now. Set the key or pick a model from the "
                         "catalogue under Settings → Models, then choose “Continue this item” -- or fail it.",
        "outcome_unknown": "A model call that had already been sent got no answer (a timeout, a dropped connection or a "
                           "server error), so its outcome is not known. It is not sent again on its own, so the same "
                           "call is never paid for twice. The item has stopped and is waiting for you; it has not "
                           "failed. “Continue this item” runs this turn again (whatever the turn had finished before "
                           "that call may be repeated) -- or fail it.",
        "external_failed": "The external agent this item was given did not finish it (it failed, you denied it, or it is "
                           "not available right now). Order work is never handed to another agent or redone by the "
                           "built-in agent on its own, so the same work is never done -- and paid for -- twice. The item "
                           "has stopped and is waiting for you; it has not failed. “Continue this item” has the same "
                           "agent do it again -- or fail it.",
        "ledger_unavailable": "The budget ledger could not be read or written just now (the database was busy or "
                              "could not be opened), so the model call stopped before it was sent: nothing left and "
                              "nothing was spent. The allowance is neither used up nor closed. Choose “Continue this "
                              "item” in a moment -- or fail it.",
        "review_unreadable": "The review gave no verdict that could be read (pass or rework), several times over. A "
                             "review that did not happen cannot count as a pass, so nothing was passed; the worker was not "
                             "sent back either. “Continue this item” has the review done again.",
        "review_header": "Review",
        "review_headline": "{title} gave no readable verdict, and nothing was passed",
        "ledger_header": "Budget ledger",
        "ledger_headline": "The model call of {title} was not sent: the budget ledger could not be read or written",
        "external_header": "External agent",
        "external_headline": "The external agent of {title} did not finish, and no other one was tried",
        "route_header": "Model call",
        "route_headline": "The model call of {title} was refused before it was sent",
        "outcome_headline": "A model call of {title} got no answer and was not re-sent",
        "report_outcome_unknown": "The model call of this report turn has no known outcome (the request went out and no "
                                  "answer came back); it may have been charged. It is not retried on its own, so the "
                                  "same call is never paid for twice, and the turn is not counted as a failed report. "
                                  "To retry the report, choose “Continue this item”; a report cannot be skipped (the "
                                  "review needs it) -- to stop instead, cancel the order.",
        "report_outcome_headline": "The model call of {title} has no known outcome and may have been charged; not retried",
        "what": {"exhausted": "it is used up", "unavailable": "it is closed or held", "refused": "it refused the call",
                 "uncertain": "a call ended without a known result and the allowance is held for verification",
                 "route_refused": "refused before sending, nothing spent",
                 "outcome_unknown": "a call got no answer and was not re-sent",
                 "external_failed": "the external agent did not finish, no other one was tried",
                 "ledger_unavailable": "the budget ledger could not be read or written; nothing was sent",
                 "review_unreadable": "the review gave no readable verdict; nothing was passed"},
    },
}


def _text() -> dict[str, Any]:
    return _TEXT["zh" if is_zh() else "en"]


def stopped_result(exc: BaseException) -> TaskResult:
    """The result of a turn the allowance refused, in the shape the native agent gives it: failed, with the mark.

    An external harness (the Codex SDK provider) raises the refusal instead of returning it -- a ``ModelBudgetStop`` for
    a binding it cannot honour, the ledger's own ``BudgetExceeded`` / ``OrderTransitionError`` when the turn cannot be
    reserved. Unmarked, that exception left the engine as a crash: the work item was re-dispatched by the crash-streak
    rule and failed after three refusals, with nothing telling the owner it was the allowance.
    """
    from ampuly.llm.budget import budget_stop_kind

    return TaskResult(status=TaskStatus.FAILED, content=str(exc), artifacts={BUDGET_STOP_ARTIFACT: budget_stop_kind(exc)})


def external_failed_result(agent_name: str, *, result: TaskResult | None = None, detail: str = "") -> TaskResult:
    """An external agent that did not finish order work, as a stopped result (model-routing phase 0, C11).

    Marked like a budget stop: the engine does not re-run it, and a company work item waits for a person (``as_pause``)
    who can have the same agent go again or fail the item. Nothing tries another agent or native in its place.
    """
    content = str(getattr(result, "content", "") or detail or "").strip()
    artifacts = dict(getattr(result, "artifacts", None) or {})
    return TaskResult(status=TaskStatus.FAILED, content=f"[{agent_name}] {content}".strip(),
                      artifacts={**artifacts, BUDGET_STOP_ARTIFACT: "external_failed", "external_failed_agent": agent_name},
                      cost=float(getattr(result, "cost", 0.0) or 0.0),
                      token_usage=dict(getattr(result, "token_usage", None) or {}))


def review_unreadable_result(task: Task, *, attempts: int) -> TaskResult:
    """A review whose verdict could not be read after its retries, as a wait for a person (model-routing phase 0, C5b).

    It used to close the review as APPROVED: a review nobody could read passed the work. Now the review card waits;
    its one answer, "continue", runs the review again, and the reviewed output stays as it is.
    """
    request = _pause_request(task, "review_unreadable", detail=f"verdict unreadable after {int(attempts)} attempt(s)")
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                      artifacts={BUDGET_STOP_ARTIFACT: "review_unreadable", "pause_request": request})


def budget_refusals() -> tuple[type[BaseException], ...]:
    """What a harness turn raises when it is the allowance that said no (imported late: the ledger lives in a plugin)."""
    from ampuly.llm.budget import ModelBudgetStop
    from ampuly.plugins.office_ui.business_store import OrderTransitionError

    return (ModelBudgetStop, OrderTransitionError)


def stop_kind(result: Any) -> str:
    """The budget-stop kind a failed result carries, or ``""``."""
    if getattr(result, "status", None) != TaskStatus.FAILED:
        return ""
    return str(dict(getattr(result, "artifacts", None) or {}).get(BUDGET_STOP_ARTIFACT, "") or "").strip()


OUTCOME_UNKNOWN = "outcome_unknown"


def stop_kind_or_unknown(result: Any) -> str:
    """``stop_kind``, or ``"outcome_unknown"`` for a failed result that carries only ``model_outcome_unknown``.

    For the report-turn park only (LK-1,, runtime contract). The native agent marks a model
    request that went out without an answer as a stop; a report turn that reached the executor without that mark was
    read as an ordinary failure, counted toward the report chain's consecutive-failure limit, and a new report card
    then paid for the call again. The engine already never retries such a result; a report turn now waits for the owner.
    Execute and review turns keep reading ``stop_kind``.
    """
    kind = stop_kind(result)
    if kind or getattr(result, "status", None) != TaskStatus.FAILED:
        return kind
    return OUTCOME_UNKNOWN if dict(getattr(result, "artifacts", None) or {}).get("model_outcome_unknown") else ""


def is_waiting_result(result: Any) -> bool:
    """True for the result of a turn that ended in this module's wait."""
    if getattr(result, "status", None) != TaskStatus.AWAITING_HUMAN:
        return False
    request = dict(getattr(result, "artifacts", None) or {}).get("pause_request")
    return isinstance(request, dict) and bool(str(request.get("budget_stop", "") or "").strip())


def waits_for_a_person(task: Any) -> bool:
    """Only a work item of a company run waits: it has an order, an owner and a card to show. A trial, a plain task or
    a nested assessment has none of these -- those stop (and are not retried), which is what their callers expect."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    if bool(metadata.get("company_runtime_auxiliary_task", False)):
        return False
    if (is_runtime_auxiliary_work_item(metadata) and not is_review_execution_work_item_metadata(metadata)
            and not is_report_execution_work_item_metadata(metadata)):
        # A hidden attention card is settled by its own finalizer the moment its turn returns; a wait parked on it
        # would be a card nobody can usefully answer. It stops (and is not retried), as before.
        return False
    # A REPORT card waits too (LK-1, 2026-09-27): the executor parks a stopped report turn for the owner (AN), and
    # the engine's one write of the turn's result now stores that wait -- a crash between the engine's write and the
    # park can no longer leave the report FAILED. Report cards carry no connector or desktop step, so the connector
    # waits that also ask this question leave them alone.
    # A REVIEW card does wait. It is the judge of the multi-team runtime: stopped and left to its finalizer it has no
    # verdict, "no verdict" is retried as a parse failure, and after the retries the review is closed as APPROVED --
    # the allowance running out would pass work nobody looked at (found ).
    return bool(is_work_item_runtime_metadata(metadata) or linked_work_item_id_for_task(task))


def _pause_request(task: Task, kind: str, *, detail: str, reply: str = "", judged: dict[str, Any] | None = None) -> dict[str, Any]:
    from ampuly.layer4_tools.spend_guard import budget_order_id

    text = _text()
    metadata = dict(task.metadata or {})
    previous = dict(metadata.get(WAIT_KEY, {}) or {})
    # Each park is its own event: checkpoints are keyed by source event and an answered one is a tombstone, so a second
    # stop with the same id would get no card at all.
    seq = int(previous.get("seq", 0) or 0) + 1
    # The item may be holding an answer it had asked for itself ("which colour?" -> "blue"): the engine overwrites those
    # two context keys with this card's reply, so they are kept here and put back when the wait ends.
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    earlier = snapshot.get("requested_user_input")
    if isinstance(earlier, dict) and str(earlier.get("budget_stop", "") or "").strip():
        carried = dict(previous.get("carried", {}) or {})
    else:
        carried = {key: snapshot[key] for key in ("user_supplied_input", "requested_user_input") if snapshot.get(key) not in (None, "", {})}
    budget_id = str(metadata.get("budget_id", "") or "").strip()
    judged = judged if judged is not None else previous.get("judged")
    task.metadata = {**metadata, WAIT_KEY: {"seq": seq, "kind": kind, "budget_id": budget_id, "waiting": True,
                                            "at": datetime.now().isoformat(), **({"carried": carried} if carried else {}),
                                            **({"judged": judged} if judged else {})}}
    reason = text.get(kind) or text["refused"]
    # LK-1: a report turn whose model call has no known outcome says so in its own words (retry, or cancel the order).
    report_unknown = (kind == OUTCOME_UNKNOWN and is_report_execution_work_item_metadata(metadata)
                      and not is_review_execution_work_item_metadata(metadata))
    if report_unknown:
        reason = text["report_outcome_unknown"]
    if judged:
        reason = text["judge"] + chr(10) + chr(10) + reason
    if reply:
        reason = text["again"].format(reply=reply[:120]) + chr(10) + chr(10) + reason
    elif seq > 1 and not detail:
        reason = text["asked_again"] + chr(10) + chr(10) + reason
    review_card = is_review_execution_work_item_metadata(metadata)
    # A report card (AN): the step's work is done and kept; "fail" would only fail the handoff, and a new report card
    # would then run on its own. Like a review card it has one answer, and cancelling the order is the way to stop.
    report_card = not review_card and is_report_execution_work_item_metadata(metadata)
    if review_card:
        reason = text["review"] + chr(10) + chr(10) + reason
    elif report_card and not report_unknown:
        # The unknown-outcome text already says what a report card is and how to stop; the 被拦下 preface would be wrong.
        reason = text["report"] + chr(10) + chr(10) + reason
    one_answer = review_card or report_card
    options = [{"id": CONTINUE, "label": text[CONTINUE]}] + ([] if one_answer else [{"id": FAIL, "label": text[FAIL]}])
    # Which ceiling said no: a model call unless the refusal names another resource (a paid tool does, e.g. images).
    named = _RESOURCE.search(detail or "")
    # Carried only into the same wait asked again (no new detail); a new stop names its own resource or is a model
    # call (review J3): only a paid tool puts [budget:<resource>] in its refusal.
    resource = named.group(1) if named else (str(previous.get("resource", "") or "model_call") if not detail else "model_call")
    task.metadata = {**task.metadata, WAIT_KEY: {**task.metadata[WAIT_KEY], "resource": resource}}   # the next card of this wait names it too
    if resource != "model_call":
        reason = text["tool"].format(resource=dict(text["resources"]).get(resource, resource)) + chr(10) + chr(10) + reason
    what = dict(text["what"])
    # A model call refused before sending, or sent without an answer, is not the allowance's doing: its own header.
    # Neither is an external agent that did not finish order work, nor a ledger that could not be read.
    header_key = {"route_refused": "route_header", "outcome_unknown": "route_header", "external_failed": "external_header",
                  "ledger_unavailable": "ledger_header", "review_unreadable": "review_header"}.get(kind, "header")
    headline_key = {"route_refused": "route_headline", "outcome_unknown": "outcome_headline",
                    "external_failed": "external_headline", "ledger_unavailable": "ledger_headline",
                    "review_unreadable": "review_headline"}.get(kind, "headline")
    if report_unknown:
        headline_key = "report_outcome_headline"
    # The owner reads the turn by what it is, not by its runtime title ("Report #1: X" -> 「X」的汇报; AN).
    from ampuly.layer2_organization.company_mode import owner_facing_turn_title

    subject = owner_facing_turn_title(str(task.title or task.id), metadata)
    headline = text[headline_key].format(title=subject, what=what.get(kind) or what["refused"])
    headline = headline[:1].upper() + headline[1:]
    return {
        "requires_user_input": True,
        "reason": reason,
        # The card shows the reason and the turn's content one under the other; the content is the one-line version.
        "headline": headline,
        "context_note": text["detail"].format(budget_id=budget_id or "-", order_id=budget_order_id(metadata) or "-",
                                              detail=(detail or "-")[:400]),
        "budget_stop": kind,
        "budget_id": budget_id,
        "budget_resource": resource,
        # The order's allowance is the owner's to decide. Without this the engine hands the card to the person who
        # signs the node's gate, and a reviewer's sub-account could fail the item "as the owner".
        "assignee_employee_id": "owner",
        "request_id": f"budget-stop:{task.id}:{seq}",
        "input_questions": [{
            "id": QUESTION_ID, "header": text[header_key], "question": text["question"],
            "options": options,
            "allow_freeform": False, "required": True,
        }],
        "required_fields": [QUESTION_ID],
    }


def as_pause(task: Task, result: TaskResult) -> TaskResult:
    """A work item's budget-stopped result, as a wait for a person; anything else, unchanged."""
    kind = stop_kind(result)
    artifacts = dict(result.artifacts or {})
    if not kind and is_report_execution_work_item_metadata(dict(getattr(task, "metadata", {}) or {})):
        # LK-1: the report park's own reading, taken here so the engine's single write already stores the wait.
        kind = stop_kind_or_unknown(result)
        if kind:
            artifacts[BUDGET_STOP_ARTIFACT] = kind
    if not kind or not waits_for_a_person(task):
        return result
    request = _pause_request(task, kind, detail=str(result.content or ""))
    return replace(result, status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                   artifacts={**artifacts, "pause_request": request})


def judge_stop_kind(decision: Any) -> str:
    """The budget-stop kind of a gate decision whose judge the allowance stopped, or ``""``."""
    if decision is None or str(getattr(decision, "source", "") or "") != "budget_stop":
        return ""
    for note in list(getattr(decision, "notes", []) or []):
        if str(note).startswith("budget_stop:"):
            return str(note).split(":", 1)[1].strip() or "refused"
    return "refused"


def park_for_judge(task: Task, kind: str, result: TaskResult, *, detail: str) -> TaskResult:
    """The item's turn is finished; its judge was stopped. The finished result is kept with the wait, so that
    "continue" judges it again instead of paying for the work a second time."""
    judged = {"content": str(result.content or ""), "artifacts": dict(result.artifacts or {}),
              "cost": float(result.cost or 0.0), "token_usage": dict(result.token_usage or {})}
    request = _pause_request(task, kind, detail=detail, judged=judged)
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                      artifacts={BUDGET_STOP_ARTIFACT: kind, "pause_request": request})


def judged_result_of(task: Task) -> TaskResult | None:
    """The finished result kept by ``park_for_judge`` -- ``None`` when there is none, or the item is still waiting.

    Reading does not remove it: it stays with the task until the judge has DECIDED (``clear_judged``). Removed on
    reading, a crash or a suspension during the judge call left nothing to judge and the paid turn ran a second time.
    The result is put back on ``task.result`` as well: the engine's resume clears it, and that is where the judge's
    evidence packet reads the output from -- without this the judge was handed an empty output (review).
    """
    wait = dict(dict(task.metadata or {}).get(WAIT_KEY, {}) or {})
    judged = wait.get("judged")
    if not isinstance(judged, dict) or wait.get("waiting"):
        return None
    result = TaskResult(status=TaskStatus.DONE, content=str(judged.get("content", "") or ""),
                        artifacts=dict(judged.get("artifacts", {}) or {}), cost=float(judged.get("cost", 0.0) or 0.0),
                        token_usage=dict(judged.get("token_usage", {}) or {}))
    task.result = {"content": result.content, "artifacts": dict(result.artifacts)}
    return result


def clear_judged(task: Task) -> None:
    """The judge decided (or the owner failed the item): the kept result has served."""
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if "judged" in wait:
        wait.pop("judged")
        task.metadata = {**metadata, WAIT_KEY: wait}


def park_again(task: Task, kind: str, *, reply: str) -> TaskResult:
    """The same wait, asked again, for an answer that named neither choice."""
    request = _pause_request(task, kind, detail="", reply=reply)
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                      artifacts={BUDGET_STOP_ARTIFACT: kind, "pause_request": request})


def checkpoint_for(task: Task, result: TaskResult, *, plan: Any, extra: dict[str, Any]) -> dict[str, Any]:
    """The ``task_user_input`` checkpoint of a wait the executor enters itself (the engine writes the first one)."""
    request = dict(dict(result.artifacts or {}).get("pause_request", {}) or {})
    metadata = dict(task.metadata or {})
    return {
        "checkpoint_type": "task_user_input",
        "project_id": task.project_id,
        "session_id": task.session_id,
        "task_id": task.id,
        "payload": {
            "task_id": task.id,
            "session_id": task.session_id,
            "execution_mode": str(metadata.get("execution_mode", "") or "company_mode"),
            "task_ids": [task.id],
            "org_version": metadata.get("org_version", 1),
            "runtime_topology_version": metadata.get("runtime_topology_version", 1),
            "reorg_proposal_id": metadata.get("reorg_proposal_id", ""),
            "company_work_item_plan": plan,
            "prompt": result.content,
            "pause_request": request,
            "review_level": "human",
            "review_target_role_id": "",
            "review_chain_role_ids": [],
            "source_event_id": str(request.get("request_id", "") or ""),
            "review_feedback_version": 0,
            "revision": 0,
            **dict(extra or {}),
        },
    }


def failed_by_owner(task: Task, kind: str) -> TaskResult:
    clear_judged(task)
    text = _text()
    # The recorded reason names what really stopped the item (review J4); the allowance text is for the allowance's own.
    return TaskResult(status=TaskStatus.FAILED, content=text.get(f"failed_{kind}") or text["failed"],
                      artifacts={BUDGET_STOP_ARTIFACT: kind, "budget_stop_decision": FAIL})


_RESOURCE = re.compile(r"\[budget:([a-z_]+)\]")
_SELECTED = re.compile(r"selected_option_id['\"]?\s*[:=]\s*['\"]?([a-z_]+)")


def _named_answer(reply: str, requested: dict[str, Any]) -> str:
    """The one answer the WHOLE reply names, or ``UNRECOGNISED``.

    A sentence that merely contains one of the words is not an answer: "先别判失败，我去加预算" is not a fail and "不要继续"
    is not a continue (adversarial review, -- a substring match read both the wrong way round, and a fail
    cannot be undone). Only prefixes this module itself produced are taken off; "打回：..." from a channel is not one.
    """
    questions = [item for item in list(requested.get("input_questions", []) or []) if isinstance(item, dict)]
    labels = {str(option.get("id", "") or ""): " ".join(str(option.get("label", "") or "").lower().split())
              for question in questions for option in list(question.get("options", []) or []) if isinstance(option, dict)}
    headers = [" ".join(str(question.get(key, "") or "").lower().split()) for question in questions for key in ("header", "question", "id")]
    said = " ".join(reply.lower().split())
    structured = _SELECTED.search(said)
    if structured:
        said = structured.group(1)
    else:
        said = said.lstrip("-").strip()
        for header in filter(None, headers):
            for colon in (":", "\uff1a"):
                if said.startswith(header + colon):
                    said = said[len(header) + 1:].strip()
        said = said.strip(" \u3002.!\uff01")
    continues = said in {*_SAYS_CONTINUE, labels.get(CONTINUE) or CONTINUE}
    # A card that does not offer "fail" (a review card) does not take it either.
    fails = FAIL in labels and said in {*_SAYS_FAIL, labels[FAIL]}
    if continues == fails:
        return UNRECOGNISED
    return CONTINUE if continues else FAIL


# The kinds that are the allowance's own answer: only these can be put right by a ceiling.
ALLOWANCE_KINDS = frozenset({"exhausted", "unavailable", "uncertain", "refused"})


def refusal_before_continue(requested: Any, reply: str, task_metadata: dict[str, Any] | None, budget_of: Any,
                            route_check: Any = None) -> str:
    """Why "continue" would only stop again, in the owner's language -- or ``""`` when there is nothing to say.

    Asked before the answer is accepted, so the card stays where it is instead of being answered, refused before
    dispatch, and replaced by a new one. Only a reply that names "continue" on a budget card is looked at. A task that
    carries no budget any more (its allowance was closed and none granted) is let through: nothing stops it, and the
    card has already said it would then run without a ceiling. ``budget_of(budget_id)`` returns the budget or ``None``.
    """
    if not isinstance(requested, dict) or not str(requested.get("budget_stop", "") or "").strip():
        return ""
    if _named_answer(str(reply or ""), requested) != CONTINUE:
        return ""
    zh = is_zh()
    kind = str(requested.get("budget_stop", "") or "").strip()
    if kind == "route_refused" and callable(route_check):
        # A dry run that sends nothing (review J5): while the route still refuses, the card stays and says why.
        try:
            still = str(route_check() or "").strip()
        except Exception:  # noqa: BLE001 - a check that cannot run says nothing; the engine decides
            still = ""
        if still:
            return (f"这一项的模型调用仍会在发出前被拒绝（{still}），没有继续。请先到「配置 → 模型」设置密钥或换一个目录里的模型。" if zh
                    else f"This item's model call would still be refused before it is sent ({still}); not continued. "
                         "Set the key or pick a model from the catalogue under Settings → Models first.")
    budget_id = str(dict(task_metadata or {}).get("budget_id", "") or "").strip()
    if not budget_id:
        return ""
    try:
        budget = budget_of(budget_id)
    except Exception as exc:  # noqa: BLE001 - a ledger that cannot be read is not a ledger that says yes
        return (f"读不到预算 {budget_id}（{type(exc).__name__}），没有继续。" if zh
                else f"Budget {budget_id} could not be read ({type(exc).__name__}); not continued.")
    if not budget:
        return (f"这一项带着的预算 {budget_id} 已不存在，继续只会再次被拒。" if zh
                else f"The budget this item carries ({budget_id}) no longer exists; continuing would only be refused again.")
    state = str(budget.get("state", "") or "")
    why = str(budget.get("state_reason", "") or "").strip()
    if state == "closed":
        return ("这一项带着的预算已被关闭，继续只会再次被拒。请先为订单重新发放预算。" if zh
                else "The allowance this item carries is closed; continuing would only be refused again. Grant the order a new allowance first.")
    if state != "open":
        if zh:
            return "预算仍被挂起待核实" + (f"（{why}）" if why else "") + "。请先在订单行上说明那次调用的结果。"
        return "The allowance is still held for verification" + (f" ({why})" if why else "") + ". Say on the order row what happened to that call first."
    if kind not in ALLOWANCE_KINDS:
        # Not the allowance's stop (a route refusal, an unknown outcome, an external agent, the ledger, a review):
        # a ceiling has nothing to do with whether continuing can work (review J3).
        return ""
    resource = str(requested.get("budget_resource", "") or "model_call")
    left = dict(budget.get("available", {}) or {}).get(resource)
    if left is not None and float(left) <= 0:
        name = dict(_TEXT["zh" if zh else "en"]["resources"]).get(resource, resource)
        return (f"「{name}」的上限仍然用尽，继续只会再次被拒。请先在订单行上「提高上限」。" if zh
                else f"The ceiling for {name} is still used up; continuing would only be refused again. Use “Raise ceiling” on the order row first.")
    return ""


def take_answer(task: Task) -> tuple[str, str, str]:
    """``(answer, kind, reply)`` for a work item about to run again.

    ``answer`` is ``""`` when this run has nothing to do with a budget stop, else ``CONTINUE``, ``FAIL`` or
    ``UNRECOGNISED``. A waiting item that runs again WITHOUT an answer (the healer released it, a restart requeued it,
    an answer was accepted and lost on the way) is ``UNRECOGNISED`` too: it is asked again, never run -- running is the
    paid turn nobody approved. The wait ends only on ``CONTINUE``; a fail or a re-park that dies half-way finds the
    flag still set and asks again. The reply and the request leave the task's context: they are the owner's words to
    this module, not input for the model; what the context held before the stop (an earlier answer the item had asked
    for) is put back.
    """
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    requested = snapshot.get("requested_user_input")
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    if not isinstance(requested, dict) or not str(requested.get("budget_stop", "") or "").strip():
        if wait.get("waiting"):
            return UNRECOGNISED, str(wait.get("kind", "") or "refused"), ""
        return "", "", ""
    kind = str(requested.get("budget_stop", "") or "").strip()
    reply = str(snapshot.pop("user_supplied_input", "") or "").strip()
    snapshot.pop("requested_user_input", None)
    snapshot.update(dict(wait.get("carried", {}) or {}))
    task.context_snapshot = snapshot
    answer = _named_answer(reply, requested)
    if answer == CONTINUE:
        task.metadata = {**metadata, WAIT_KEY: {**{k: v for k, v in wait.items() if k != "carried"}, "waiting": False}}
    return answer, kind, reply
