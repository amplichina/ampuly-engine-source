"""A step whose last turn ended in an unknown state at a console restart waits for a person; it is not replayed.

Seen 2026-09-25 19:50 (order wo-20260925-83c4bc): step 2 was waiting on a browser_navigate permission when the
console restarted. Startup parked the run, the automatic resume re-dispatched the step, the harness refused the
replay (``HarnessSessionBusy``: the earlier turn has no terminal record), the refusal was logged as a crash with a
traceback, the item failed, and the lead went on to final delivery with steps 2 and 3 undone.

This module is the whole of that decision, kept out of the kernel files the way ``budget_stop_wait`` and
``connector_done_wait`` are. Three places call it:

- startup reconcile (engine) writes ``MARKER_KEY`` on the step's work item -- which permission it was waiting on,
  which harness turn is unsettled -- and retires the permission card the restart made unanswerable;
- the executor (company_mode) turns the harness refusal into a wait on the ``task_user_input`` rail: the same card,
  durable checkpoint and restart behaviour as every other wait for a person;
- the engine's task-checkpoint resume reads the answer. "重做这一步" settles the unknown turn (recorded as unknown,
  never as a pass), drops the step's harness session and runtime continuation, and runs the step again from the
  start. "跳过并标记未完成" closes the step as not done; what depends on it does not start on its own. Anything
  else asks again.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.i18n_runtime import is_zh
from ampuly.layer3_agent.harness_sessions import HarnessSessionBusy

WAIT_KEY = "restart_unknown_wait"      # task metadata: the wait itself (seq / waiting / decision)
MARKER_KEY = "restart_unknown_turn"    # task or work item metadata: what the step was doing when the console restarted
QUESTION_ID = "restart_unknown"
REDO = "redo_step"
SKIP = "skip_step"
UNRECOGNISED = "unrecognised"
# Why a permission card was closed at startup (``administrative_reason`` on the superseded checkpoint).
SUPERSEDED_REASON = "restart_unknown_turn"
UNCLAIMED_ANSWER_REASON = "restart_recovery_unclaimed_answer"

# What a reply may say for each answer. The chat card sends "- <header>: <label>", the approval centre sends the
# option id, a channel sends whatever the person typed. Naming neither, or both, asks again.
_SAYS_REDO = ("redo_step", "重做", "redo")
_SAYS_SKIP = ("skip_step", "跳过", "skip")

_TOOL_ACTIONS_ZH = {
    "browser_navigate": "打开网页",
    "browser_navigate_back": "网页后退",
    "browser_click": "点击网页",
    "browser_type": "在网页里输入",
    "browser_submit_enter": "在网页里提交",
    "browser_select_option": "在网页里选择",
    "browser_scroll": "滚动网页",
    "browser_take_screenshot": "网页截图",
    "browser_snapshot": "读取网页",
    "browser_observe": "查看网页",
    "browser_wait_for": "等待网页",
    "browser_close": "关闭网页",
    "browser_switch_tab": "切换网页标签",
    "browser_request_human": "请人接手网页",
    "desktop_observe": "查看本机桌面",
    "desktop_act": "在本机桌面应用里操作",
    "desktop_finish": "核对桌面步骤是否做完",
    "web_fetch": "抓取网页",
    "web_search": "搜索网页",
    "shell": "运行命令",
    "run_command": "运行命令",
    "file_write": "写文件",
    "write_file": "写文件",
    "file_edit": "改文件",
    "send_email": "发邮件",
    "send_dm": "发消息",
}

_TEXT = {
    "zh": {
        "header": "重启后确认",
        "question": "这一步怎么处理？",
        REDO: "重做这一步",
        SKIP: "跳过并标记未完成",
        "reason_permission": "控制台重启前这一步正在等一个权限请求（{action}），重启后没法确定它做到哪了。"
                             "请选：重做这一步 / 跳过并标记未完成。",
        "reason_turn": "控制台重启前这一步正在运行，重启后没法确定它做到哪了。请选：重做这一步 / 跳过并标记未完成。",
        "note": "重做会从头再跑这一步（重启前那一回合里已经做过的动作可能重复）；跳过会把这一步标记为未完成，"
                "依赖它的步骤不会自动开始。这一步没有重跑，也没有花费。",
        "headline": "{title}重启后需要您确认怎么继续",
        "again": "上一次的答复没有说明（收到的是：{reply}），这一步仍在等待。请只回「重做这一步」或「跳过并标记未完成」。",
        "parked": "重启后这一步的上一轮结果未知，已停下等人确认，没有重跑。",
        "redo": "已按您的选择重做这一步；重启前那一回合按结果未知记录，没有当作完成。",
        "skip": "已按您的选择跳过这一步并标记为未完成；依赖它的步骤不会自动开始。",
        "unclaimed_answer": "重启前有一条已收到但没来得及处理的答复（{text}），已作废；这一步会重新问一次。",
    },
    "en": {
        "header": "Confirm after restart",
        "question": "What should happen to this step?",
        REDO: "Redo this step",
        SKIP: "Skip it and mark it not done",
        "reason_permission": "When the console restarted, this step was waiting on a permission request ({action}); "
                             "after the restart nobody can tell how far it got. Choose: redo this step / skip it and "
                             "mark it not done.",
        "reason_turn": "When the console restarted, this step was running; after the restart nobody can tell how far "
                       "it got. Choose: redo this step / skip it and mark it not done.",
        "note": "Redo runs the step again from the start (what the interrupted turn had already done may be "
                "repeated); skip marks the step not done, and nothing that depends on it starts on its own. The "
                "step has not run again and nothing was spent.",
        "headline": "{title} needs your decision after the restart",
        "again": "The last answer said neither (it was: {reply}); this step is still waiting. Answer with exactly one "
                 "of the two choices.",
        "parked": "After the restart the outcome of this step's last turn is unknown; it waits for a person and was "
                  "not run again.",
        "redo": "Redoing this step as you chose; the turn before the restart is recorded as unknown, not as done.",
        "skip": "Skipped this step as you chose and marked it not done; nothing that depends on it starts on its own.",
        "unclaimed_answer": "An answer received before the restart was never taken ({text}); it is retired and the "
                            "step asks again.",
    },
}


def _text() -> dict[str, Any]:
    return _TEXT["zh" if is_zh() else "en"]


def tool_action_label(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """What the step was asking permission for, in the owner's words: 「打开网页 https://…」, not ``browser_navigate``."""
    name = str(tool_name or "").strip()
    args = dict(arguments or {})
    target = ""
    for key in ("url", "path", "command", "to_agent", "query"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            break
    label = (_TOOL_ACTIONS_ZH.get(name) if is_zh() else "") or name or ("未知操作" if is_zh() else "an action")
    return f"{label} {target[:120]}".strip() if target else label


def marker_for(
    *,
    tool_name: str = "",
    arguments: dict[str, Any] | None = None,
    checkpoint_id: str = "",
    harness_session_id: str = "",
    turn_id: str = "",
) -> dict[str, Any]:
    """What startup found the step doing: the permission it waited on and/or the harness turn left unsettled."""
    return {
        "tool_name": str(tool_name or "").strip(),
        "action": tool_action_label(tool_name, arguments) if str(tool_name or "").strip() else "",
        "checkpoint_id": str(checkpoint_id or "").strip(),
        "harness_session_id": str(harness_session_id or "").strip(),
        "turn_id": str(turn_id or "").strip(),
        "found_at": datetime.now().isoformat(),
    }


def is_recovery_conflict(exc: BaseException | None) -> bool:
    """True when the harness refused to run a turn because the previous one has no known outcome.

    That refusal is the boundary doing its job, not a crash: nothing was sent, nothing was spent.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, HarnessSessionBusy):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def marker_of(task: Any, work_item_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """The startup marker for this step, from the task first and the work item second; ``{}`` when there is none."""
    for source in (dict(getattr(task, "metadata", {}) or {}), dict(work_item_metadata or {})):
        marker = source.get(MARKER_KEY)
        if isinstance(marker, dict) and marker:
            return dict(marker)
    return {}


def is_wait_request(request: Any) -> bool:
    """Whether a pause request (a card's ``pause_request``) is this module's."""
    return isinstance(request, dict) and bool(request.get(QUESTION_ID, False))


def _pause_request(task: Task, marker: dict[str, Any], *, reply: str = "") -> dict[str, Any]:
    text = _text()
    metadata = dict(task.metadata or {})
    previous = dict(metadata.get(WAIT_KEY, {}) or {})
    # Each park is its own event: checkpoints are keyed by source event and an answered one is a tombstone.
    seq = int(previous.get("seq", 0) or 0) + 1
    task.metadata = {
        **metadata,
        WAIT_KEY: {"seq": seq, "waiting": True, "at": datetime.now().isoformat(), "marker": dict(marker or {})},
    }
    action = str(dict(marker or {}).get("action", "") or "").strip()
    reason = text["reason_permission"].format(action=action) if action else text["reason_turn"]
    reason = reason + chr(10) + chr(10) + text["note"]
    if reply:
        reason = text["again"].format(reply=reply[:120]) + chr(10) + chr(10) + reason
    # The owner reads the step by what it is, not by its runtime title ("Report #1: X" -> 「X」的汇报; AN).
    from ampuly.layer2_organization.company_mode import owner_facing_turn_title

    headline = text["headline"].format(title=owner_facing_turn_title(str(task.title or task.id), metadata))
    return {
        "requires_user_input": True,
        "reason": reason,
        "headline": headline[:1].upper() + headline[1:],
        QUESTION_ID: True,
        "marker": dict(marker or {}),
        # The restart is the owner's to decide; the gate signer of the node has nothing to sign here.
        "assignee_employee_id": "owner",
        "request_id": f"restart-unknown:{task.id}:{seq}",
        "input_questions": [{
            "id": QUESTION_ID,
            "header": text["header"],
            "question": text["question"],
            "options": [{"id": REDO, "label": text[REDO]}, {"id": SKIP, "label": text[SKIP]}],
            "allow_freeform": False,
            "required": True,
        }],
        "required_fields": [QUESTION_ID],
    }


def as_pause(task: Task, marker: dict[str, Any], *, reply: str = "") -> TaskResult:
    """The wait for a person, as a turn result: the same shape a budget stop or a browser check produces."""
    request = _pause_request(task, marker, reply=reply)
    return TaskResult(
        status=TaskStatus.AWAITING_HUMAN,
        content=request["headline"],
        artifacts={"pause_request": request, QUESTION_ID: True},
    )


def is_waiting_result(result: Any) -> bool:
    if getattr(result, "status", None) != TaskStatus.AWAITING_HUMAN:
        return False
    return is_wait_request(dict(getattr(result, "artifacts", None) or {}).get("pause_request"))


def checkpoint_for(task: Task, result: TaskResult, *, plan: Any, extra: dict[str, Any]) -> dict[str, Any]:
    """The ``task_user_input`` checkpoint of this wait (the same rail as ``budget_stop_wait.checkpoint_for``)."""
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


def named_answer(reply: str, requested: dict[str, Any] | None = None) -> str:
    """``REDO``, ``SKIP`` or ``UNRECOGNISED`` for what a person wrote back to this wait."""
    head = str(reply or "").strip().lower()
    if not head:
        return UNRECOGNISED
    labels: dict[str, str] = {}
    for question in list(dict(requested or {}).get("input_questions", []) or []):
        for option in list(dict(question or {}).get("options", []) or []):
            option_id = str(dict(option or {}).get("id", "") or "")
            label = str(dict(option or {}).get("label", "") or "").strip().lower()
            if option_id and label:
                labels[option_id] = label
    says_redo = any(word in head for word in _SAYS_REDO) or (labels.get(REDO, "") and labels[REDO] in head)
    says_skip = any(word in head for word in _SAYS_SKIP) or (labels.get(SKIP, "") and labels[SKIP] in head)
    if says_redo and not says_skip:
        return REDO
    if says_skip and not says_redo:
        return SKIP
    if says_redo and says_skip:
        # 「重做这一步，不要跳过」 names both words; only an answer that names exactly one option id or label counts.
        exact = re.sub(r"\s+", "", head)
        for option_id, label in labels.items():
            if exact in {option_id, re.sub(r"\s+", "", label)}:
                return option_id
    return UNRECOGNISED


def end_wait(task: Task, choice: str, *, reply: str = "", settled_turn_id: str = "") -> None:
    """The person decided: the wait is over, and what they chose is written beside it."""
    metadata = dict(task.metadata or {})
    wait = dict(metadata.get(WAIT_KEY, {}) or {})
    wait.update({
        "waiting": False,
        "decision": str(choice or ""),
        "decided_at": datetime.now().isoformat(),
        "reply": str(reply or "")[:400],
        "settled_turn_id": str(settled_turn_id or ""),
    })
    task.metadata = {**metadata, WAIT_KEY: wait}


def parked_text() -> str:
    return _text()["parked"]


def again_text(reply: str) -> str:
    return _text()["again"].format(reply=str(reply or "")[:120])


def decision_text(choice: str) -> str:
    return _text()["skip"] if choice == SKIP else _text()["redo"]


def unclaimed_answer_text(answer: str) -> str:
    return _text()["unclaimed_answer"].format(text=str(answer or "")[:120] or "-")


def with_reply(result: TaskResult, task: Task, marker: dict[str, Any], reply: str) -> TaskResult:
    """The same wait asked again, for an answer that named neither choice."""
    request = _pause_request(task, marker, reply=reply)
    return replace(result, status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                   artifacts={"pause_request": request, QUESTION_ID: True})
