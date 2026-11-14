"""What the master conversation's tool loop is allowed to reach, and how.

:mod:`master_runtime` is the executor and :mod:`master_tools` is the
declaration of what may be called. Neither knows about the console, on
purpose. This is the piece in between: the object the loop calls when the
model asks for something, bound to one console handler, one socket, one engine
and one chat line.

It is deliberately thin, and everything it refuses to do is the point:

**A read answers from the builders master control already has.** The same
:func:`~master_control.build_system_context` snapshot the single-JSON turn is
assembled from, the same :func:`~master_control.capability_snapshot`, the same
:func:`~master_control.process_drafts`. A second query written here would be a
second answer to "what is running", and the two would agree until they didn't.

**A write goes through the console handler that already implements it.**
``master_tools.write_handlers()`` names the method per tool and this looks it
up by that name -- so a rename that orphans a tool fails here rather than
silently taking a different road. That funnel is what makes the
``client_request_id`` idempotency key and the checkpoint compare-and-set mean
anything; a tool reaching a domain service directly would be the second path
the plan forbids.

**The model proposing an action is not the action being allowed.** Every write
is rebuilt into the action dict shape ``actions_from_model`` produces and run
through that same validation against a *freshly read* context: a checkpoint id
must be one that is pending right now, a stop must name an order that is
actually open. Arriving as a tool call instead of as JSON changes the road, not
the gate.

**A chat line is not a batch job.** The single-JSON path takes at most one
order draft and one control verb per turn. So does this, counted across the
whole loop rather than per call -- otherwise ten iterations would be ten work
orders waiting on the owner for one sentence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

# The switch. Declared in settings_service.RUNTIME_FLAGS so it renders as a
# checkbox and persists to the user environment; read through that module so
# there is one place that decides what "on" means.
# Opt-OUT. The loop is the default; this switch restores
# the single-call turn. Named for what setting it does, because the settings
# reader's one rule is "absent means off" and what is off by default now is
# the fallback, not the loop.
MASTER_LOOP_ENV = "AMPULY_MASTER_SINGLE_SHOT"
# A proposal the builder canvas previews; a table past this is not something a person reviews on one screen.
MAX_PROPOSED_NODES = 200
# What master control answers when a tool names a flow another project owns. Said in words: the model repeats it
# to the owner, who reads no ids.
OTHER_PROJECTS_FLOW = "这条流程属于别的项目，这里不能动它"


class MasterLoopFailed(RuntimeError):
    """The looped turn did not finish, and what it had already done.

    The delegate travels with the failure so a turn that already wrote can be
    reported without a second model call. A loop that drafted an order and then
    ran out of iterations must not be answered by the single-call path on the
    same message: that path would see the same request and could draft the
    same order a second time. A turn that wrote nothing is also not retried:
    the original goal stays on the task, and only an explicit single-shot
    switch, set before the turn, uses the JSON path.
    """

    def __init__(self, reason: str, actions: Any = None) -> None:
        super().__init__(str(reason))
        self.reason = str(reason)
        self.actions = actions


def loop_enabled() -> bool:
    """Whether the multi-step turn runs. On by default; the switch turns it off.

    Tool calls have a per-message cap of ten iterations.
    The console shows every tool call as it happens.
    The settings-page switch restores the single-call turn
    immediately.

    An unreadable flag is treated as loop-off for *routing into the multi-step
    turn* only. The paid single-call path must still see an affirmative
    :func:`single_shot_explicitly_enabled` (AUD49-R01 / 4.9-A01-WS-01).
    """
    from ampuly.plugins.office_ui.settings_service import runtime_flag_enabled

    try:
        return not runtime_flag_enabled(MASTER_LOOP_ENV)
    except Exception:
        # Not knowing the switch: do not enter the multi-step turn. The
        # single-call consult is gated separately by
        # single_shot_explicitly_enabled() so an unreadable registry cannot
        # silently spend.
        return False


def single_shot_explicitly_enabled() -> bool:
    """True only when the owner affirmatively turned multi-step off.

    Unlike :func:`loop_enabled`, an unreadable settings/env state does **not**
    count as explicit single-shot. Used by ``ws_handler`` so AUD49-R01 /
    ``4.9-A01-WS-01`` stays closed: no paid single-call without
    ``AMPULY_MASTER_SINGLE_SHOT`` (or the settings checkbox that writes it).
    """
    from ampuly.plugins.office_ui.settings_service import runtime_flag_enabled

    try:
        return bool(runtime_flag_enabled(MASTER_LOOP_ENV))
    except Exception:
        return False


def loop_system_prompt() -> str:
    """What the looping conversation is told about itself.

    Deliberately not the single-JSON prompt with the JSON stripped out. That
    one hands over a snapshot and asks for one object back; this one has no
    snapshot and has to say so, because a model that assumes it was given the
    state will answer about orders it never looked at.
    """
    from ampuly.i18n_runtime import is_zh
    from ampuly.plugins.office_ui.master_tools import describe_for_prompt
    from ampuly.plugins.office_ui.product_name import PRODUCT_NAME_EN, PRODUCT_NAME_ZH

    lines = [
        f"- {tool['name']}: {tool['summary']}" + ("（会花钱）" if tool.get("spends_money") else "")
        for tool in describe_for_prompt()
    ] if is_zh() else [
        f"- {tool['name']}: {tool['summary']}" + (" (spends money)" if tool.get("spends_money") else "")
        for tool in describe_for_prompt()
    ]
    tool_list = "\n".join(lines)

    if is_zh():
        return (
            f"你是{PRODUCT_NAME_ZH}引擎的『总控』——面向老板的系统级指挥台助手。\n"
            "这一轮你可以先查再决定：下面的工具是你唯一的信息来源，也是你唯一能做的事。\n"
            f"{tool_list}\n"
            "规则：\n"
            "1. 你没有拿到任何系统快照。要谈订单、阻塞、费用、能力或流程草稿，先调用对应的查询工具，"
            "   只依据返回结果回答，绝不编造，也不要复述上一轮的旧状态。\n"
            "2. 查询返回里读不到的字段表示读不出来，不表示答案是『没有』或『不能』；如实说读不到。\n"
            "3. 费用单位是美元（usd），取自费用记录表。报给用户时换算成人民币（×7.2，保留两位小数）。\n"
            "   total_cost_usd 为 null 表示读不到，直接说读不到，不要换算成 ¥0。\n"
            "4. 只有用户在这条消息里明确下令，才调用会改变系统的工具（起草工单、批准/驳回、停止）。"
            "   绝不因推测而批准。一轮最多起草一张工单、最多一个控制动作。\n"
            "5. checkpoint_id 必须来自 master_list_blockers 的返回，task_id 必须来自 master_list_orders 的返回，"
            "   都不能自己编。\n"
            "6. 执行回执由系统追加，你不要自行宣布『已起草工单』『已批准』『已停止』。没有真的调用对应工具就绝不能说做了：\n"
            "   系统会在同一条回复里当场纠正并记录（2026-09-14 真机出现过一次）。\n"
            "7. 你不能发起流程试跑（试跑要花钱），只能告诉用户去工作流页面点。\n"
            "8. 每一次工具调用都是一次付费模型调用。查够了就回答，不要为了保险多查一遍。\n"
            "9. 老板要开一张新单时，先调用 master_list_order_flows 看这个项目能接单的业务流和它们的开单项，再调用 master_create_order "
            "   起草一张工单：workflow_id 只能取自 order_flows；fields 只按 start_fields 的 id 填老板说过的内容，不编造；必填项缺了先问老板，"
            "   不要硬起草。起草的工单在「业务工单」的「待接单」里等老板放行：不会开跑、不开会话、不花钱，所以绝不能说『已发单』『已开始处理』；"
            "   工单号不用念给老板，回复下面会带去业务工单的链接。"
            "   没有合适的业务流就如实说，并提出可以用 master_propose_flow_draft 先起草一条新流程。"
            "   工单只在它的业务流步骤允许的网站（sites）上操作：老板点名的网站不在里面时，告诉老板要在搭建台改那一步，"
            "   可以用 master_propose_candidate_nodes 提出改法。只有给**已开**的单加网站时才调用 master_delegate_browser；"
            "   站点只能是用户说出的域名，截图与下载默认不允许，除非用户明确允许；task_id 必须来自 master_list_orders。\n"
            "10. 流程的节点表你只能用 master_propose_candidate_nodes 提议，系统不会保存：老板会在搭建台画布上看改动、"
            "   应用到草稿后自己保存。绝不能说『已修改』『已保存』，要说『已提出方案，请在搭建台查看』。\n"
            "11. 搭建或调整流程时，先用 master_search_components 找所有可能有用的组件（内部库、本地资源库目录、老板在组件库里已加载或检索过的在线市场结果）；"
            "   你不能联网，市场上没找到合适的，就请老板在组件库「在线市场」里按关键词检索后再让你看。内部库的技能可以直接放到步骤上；"
            "   在线市场和本地资源库目录的也可以放，提议里会标成『外部候选』，老板在搭建台调用测试并录入内部库后才能保存；"
            "   本地资源库目录的技能要从本机资源库导入，检索结果里 can_try 为 false（本机没配置资源库）时只能作参考，提议时向老板说明。"
            "   只放检索结果里真实出现过的组件，绝不编造名字，不提议付费技能，也不能说已下载、已安装或已录入。\n"
            "12. 只有老板明确要求把一个改进建议记入治理台账，且你已经拥有完整候选、精确目标指纹和证据摘要时，才调用 "
            "master_create_improvement_proposal。它只创建草稿，不等于已修改、已测试、已批准、已启用、已发布或可跨客户分享。"
            "老板只用一句话说想改进某条已发布流程、某个内部库技能或某个已注册连接器时，也用它：proposal 只写 "
            "{target: {kind: process|skill|connector, id}, request: 老板原话}，系统按现状生成惰性候选；"
            "告诉老板去哪里改（流程草稿在搭建台、技能候选在组件工作台），批准前要按真实订单评估收益。\n"
            "13. 老板要求「把这单被客户打回的教训记成改进」时，用 master_create_improvement_from_acceptance：只给订单号、标题、理由和完整候选，"
            "目标流程、指纹和拒收证据由系统按该订单的事件链绑定；同样只是草稿。\n"
            "14. 老板要你根据一段需求或一份文件**新搭**一条流程时，把需求整理成步骤，调用 master_propose_flow_draft：每步写清谁做（ai / human / review）、"
            "做什么、交付什么文件、需要什么文件、在哪几步之后、需要什么能力（capability，几个词即可，组件由系统去内部库匹配）以及出处（source）。"
            "拿不准的写进 open_questions，自己做的低风险假设写进 assumptions；不要编组件名。系统只生成草案卡片，不会建流程或保存："
            "要告诉老板哪些组件没配上、哪些步骤还没指派角色、还缺什么，请老板点「在搭建台打开」。修改已有流程仍用 master_propose_candidate_nodes。"
            "某一步老板点名了网站就写进 site；只有老板明确说了就是这个网站，才把 lock_site 设为 true，系统会把它写成这一步唯一允许访问的网站；"
            "老板没说过的网站不要编，也不要锁。整条流程一次调用写全；同一条回复里再调用一次，会替换掉前一次的草案。\n"
            "15. 说一张单在什么状态，只用查询结果里它的 status_zh；属于工单的，用工单状态 order_status_zh，并用 order_label 或标题称呼它。"
            "绝不写英文状态码。task_id、checkpoint_id、工单号这类编号只用来调用工具，不写进给老板的回复（老板自己先提到某个编号时除外）；"
            "也不要把工具名写给老板，要说就说做了什么（比如「我查了一下工单」）。\n"
            "16. 施工模式：老板要搭一条新业务线，或者要改一条业务线的技能、步骤、提示词、测试时，不要零碎地提议，"
            "用 master_start_build 开一个施工任务（先用 master_list_builds 看有没有正在干的、能在哪条线上施工）：goal 写老板的原话；"
            "改已有的线给 line_id（取自 master_list_builds 的 lines），新线留空，可以给 name，老板点名了团队就给 team。"
            "施工在后台、在本项目的草稿里干，干完在对话里交一个候选版本；它不能上线、不能对外发送、不能花钱买东西、不能删除，"
            "上网或越界会先问老板，上线仍由老板在搭建台自己点。同一个项目一次只能有一个施工任务在干。"
            "老板说「接着改」时用 master_continue_build（job_id 取自 master_list_builds）；正在干的施工不用它，"
            "请老板在施工卡片上插话或停下。开工后只说开工了、进展会在对话里说，绝不能说「已经改好了」「已上线」。\n"
            "永远用中文回答，语气干练。查完之后直接把答案说给用户，不要输出 JSON。"
        )
    return (
        f"You are the Master Control of {PRODUCT_NAME_EN} Engine — a system-level command assistant for the owner.\n"
        "This turn you can look something up and then decide. The tools below are your only source of "
        "information and the only things you can do.\n"
        f"{tool_list}\n"
        "Rules:\n"
        "1. You were given no snapshot of the system. To talk about orders, blockers, cost, capabilities or "
        "process drafts, call the matching read tool first and answer strictly from what came back. Never "
        "invent state and never repeat an earlier turn's state as if it were current.\n"
        "2. A field missing from a result means it could not be read, NOT that the answer is no. Say you "
        "cannot tell.\n"
        "3. Cost is in USD and comes from the recorded cost rows. total_cost_usd null means the record "
        "could not be read — say cost is unavailable; never report it as $0.\n"
        "4. Call a tool that changes the system (draft an order, approve/deny, stop) ONLY when this very message "
        "explicitly commands it; never on inference. At most one order draft and one control verb per turn.\n"
        "5. A checkpoint_id must come from master_list_blockers and a task_id from master_list_orders. "
        "Never invent either.\n"
        "6. Never claim you drafted an order, approved or stopped anything unless this turn actually called that tool; "
        "the system corrects such a claim in the same reply and records it (it happened once, 2026-09-14). "
        "The system appends the receipt — do not announce that you drafted, approved or stopped "
        "anything yourself.\n"
        "7. You cannot start a process trial (it spends money); the owner runs it from the workflows page.\n"
        "8. Every tool call is a paid model call. Answer once you know enough; do not look twice to be safe.\n"
        "9. When the owner wants a new order, call master_list_order_flows for the flows this project takes orders on "
        "and their start fields, then master_create_order to draft ONE work order: the workflow_id only from "
        "order_flows, fields keyed by its start_fields ids and filled only from what the owner said -- never invented; "
        "when a required one is missing, ask instead of drafting. The draft waits in Orders (业务工单, 待接单) for the "
        "owner to release it: nothing runs, no session starts and nothing is spent, so never say it was dispatched or "
        "started, and do not read the order id out (the reply carries a link to it). When no flow fits, say so and "
        "offer to draft a new one with master_propose_flow_draft. An order works "
        "only on the sites its flow's steps allow (sites); when the owner names another, say that step has to change on "
        "the builder (propose it with master_propose_candidate_nodes). Call master_delegate_browser only to add a "
        "website to an order that is already open; the domains are only the ones the owner named, screenshots and "
        "downloads stay off unless the owner allowed them, and the task_id must come from master_list_orders.\n"
        "10. A workflow's node table can only be PROPOSED, with master_propose_candidate_nodes; nothing is saved. "
        "The owner reviews the change on the builder canvas, applies it to a draft and saves it. Never say you "
        "changed or saved it; say you proposed it and that it is waiting on the builder canvas.\n"
        "11. When building or reshaping a workflow, first call master_search_components for every component that may help "
        "(internal library, local resource catalog, the market results the owner's Components page already loaded or searched). "
        "You cannot go online: when the market part lacks what a step needs, ask the owner to search the online market on "
        "the Components page and look again. Internal "
        "skills can go on steps as they are; market ones can too and the proposal marks them as outside candidates, which "
        "the owner tries on the builder and admits before the draft saves; local catalog skills can be candidates too (imported "
        "from the local resource library), but where a search shows can_try false no library is named on this machine, so say so. Only use components a search actually returned, never invent a name, never propose "
        "a paid skill, and never say anything was downloaded, installed or admitted.\n"
        "12. Call master_create_improvement_proposal only when the owner explicitly asks to record an improvement "
        "and you have the complete candidate, exact target fingerprint and evidence digests. It creates an inert draft only; "
        "never describe it as changed, tested, approved, active, published or shareable. When the owner only says in "
        "one sentence what to improve on a published flow, a library skill or a registered connector, call it with "
        "proposal {target: {kind: process|skill|connector, id}, request: the owner's words}; the server drafts the "
        "inert candidate from the asset as it is now. Say where to edit it and that approval waits for the real-order "
        "benefit check.\n"
        "13. When the owner asks to turn an order's customer rejection into an improvement, call "
        "master_create_improvement_from_acceptance with the order_id, a title, a rationale and the complete candidate; the "
        "target workflow, its fingerprint and the rejection evidence are bound by the server. Also an inert draft only.\n"
        "14. When the owner asks for a NEW workflow from a requirement or a file, turn it into steps and call "
        "master_propose_flow_draft: for each step who does it (ai / human / review), what it does, the files it delivers and "
        "needs, which steps it comes after, the capability it needs (a few words; the console matches the component in the "
        "internal library) and where it came from (source). Put what is undecided in open_questions and your low-risk "
        "assumptions in assumptions; never invent a component name. Nothing is created or saved -- the owner gets a draft "
        "card: tell them which components are missing, which steps have no role yet and what else is missing, and ask them to "
        "open it on the builder. To change an existing workflow keep using master_propose_candidate_nodes. "
        "When the owner named a website for a step, put it in site; set lock_site true only when the owner said that exact "
        "site, and the console writes it as the step's only allowed site. Never invent or lock a site the owner did not name. "
        "Send the whole flow in one call; a later call in the same reply replaces the earlier draft.\n"
        "15. Say an order's state only in plain words from its status_zh in what the tools returned (a work order's from "
        "order_status_zh, naming it by order_label or its title), in the owner's language; never write a raw status code. "
        "Ids -- task_id, checkpoint_id, the order number -- are for calling tools only: never put one in the reply unless "
        "the owner named it first. Never name a tool to the owner either; say what you did (\"I looked at the orders\").\n"
        "16. Build mode: when the owner asks to build a new business line, or to change a line's skills, steps, prompts "
        "or tests, do not propose it piece by piece -- start a build job with master_start_build (look at "
        "master_list_builds first for one that is working and for the lines a build may start from): goal is the owner's "
        "words; line_id (from the lines of master_list_builds) to change a line, empty for a new one, with an optional "
        "name, and team when the owner named one. The build works in the background in a draft of this project and "
        "hands a candidate over in this conversation; it cannot go live, send, pay or delete, it asks the owner before "
        "going online or outside the draft, and going live stays the owner's on the builder. One build at a time per "
        "project. When the owner says to go on, call master_continue_build (job_id from master_list_builds); a build "
        "that is still working is steered or stopped from its card instead. After starting one, say only that it started "
        "and that progress will come in this conversation -- never that anything was changed or went live.\n"
        "Answer the owner directly in plain prose once you have looked. Do not emit JSON."
    )


def _proposed_action(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """The action dict the single-JSON path would have produced.

    One shape, built here from tool arguments and everywhere else from parsed
    JSON, so both roads arrive at ``actions_from_model`` carrying the same
    thing. ``note`` is mapped onto ``text`` because that is what the checkpoint
    decision builder reads; a tool argument the validator never looks at is an
    argument the owner's words get dropped from.
    """
    if name == "master_create_order":
        # policy (M1): a work-order draft -- the flow, a name for the owner, and the flow's start fields.
        # No domains any more (X05's dispatch-time delegation left with the free session): the order's own flow steps
        # say which sites it opens. ``fields`` travels as given; the draft check names anything it cannot take.
        return {
            "kind": "create_order",
            "workflow_id": str(arguments.get("workflow_id", "") or "").strip(),
            "title": str(arguments.get("title", "") or "").strip(),
            "fields": arguments.get("fields") if arguments.get("fields") is not None else {},
        }
    if name == "master_reply_checkpoint":
        return {
            "kind": "reply_checkpoint",
            "checkpoint_id": str(arguments.get("checkpoint_id", "") or ""),
            "decision": str(arguments.get("decision", "") or ""),
            "text": str(arguments.get("note", "") or ""),
        }
    if name == "master_stop_order":
        return {
            "kind": "stop_order",
            "task_id": str(arguments.get("task_id", "") or ""),
        }
    if name == "master_interrupt_turn":
        return {
            "kind": "interrupt_turn",
            "task_id": str(arguments.get("task_id", "") or ""),
        }
    if name == "master_steer_turn":
        return {
            "kind": "steer_turn",
            "task_id": str(arguments.get("task_id", "") or ""),
            "text": str(arguments.get("text", "") or ""),
        }
    if name == "master_delegate_browser":
        return {
            "kind": "delegate_browser",
            "task_id": str(arguments.get("task_id", "") or ""),
            "domains": arguments.get("domains"),
            "vision": bool(arguments.get("vision", False)),
            "downloads": bool(arguments.get("downloads", False)),
            "lease_minutes": arguments.get("lease_minutes", 60),
            "visible": bool(arguments.get("visible", False)),
        }
    if name == "master_snooze_blocker":
        return {
            "kind": "snooze_blocker",
            "checkpoint_id": str(arguments.get("checkpoint_id", "") or ""),
            "until_at": str(arguments.get("until_at", "") or ""),
            "note": str(arguments.get("note", "") or ""),
        }
    if name == "master_run_offline_trial":
        return {
            "kind": "run_offline_trial",
            "spec_id": str(arguments.get("spec_id", "") or ""),
            "version": str(arguments.get("version", "") or ""),
        }
    if name == "master_generate_connector_candidate":
        raw_confirmed = arguments.get("confirmed_inferred_fields")
        return {
            "kind": "generate_connector_candidate",
            "contract": arguments.get("contract"),
            "catalog_id": str(arguments.get("catalog_id", "") or ""),
            "catalog_fingerprint": str(arguments.get("catalog_fingerprint", "") or ""),
            "confirmed_inferred_fields": (
                [str(item) for item in raw_confirmed]
                if isinstance(raw_confirmed, list) else []
            ),
        }
    if name == "master_create_improvement_proposal":
        return {
            "kind": "create_improvement_proposal",
            "proposal": arguments.get("proposal"),
        }
    if name == "master_create_improvement_from_acceptance":
        return {
            "kind": "create_improvement_from_acceptance",
            "order_id": str(arguments.get("order_id", "") or ""),
            "title": str(arguments.get("title", "") or ""),
            "rationale": str(arguments.get("rationale", "") or ""),
            "candidate": arguments.get("candidate"),
        }
    # 总控施工模式 (policy, B1).
    if name == "master_start_build":
        return {
            "kind": "start_build",
            "goal": str(arguments.get("goal", "") or "").strip(),
            "line_id": str(arguments.get("line_id", "") or "").strip(),
            "name": str(arguments.get("name", "") or "").strip(),
            "team": str(arguments.get("team", "") or "").strip(),
        }
    if name == "master_continue_build":
        return {
            "kind": "continue_build",
            "job_id": str(arguments.get("job_id", "") or "").strip(),
            "instruction": str(arguments.get("instruction", "") or "").strip(),
        }
    return None


# The build writes (总控施工模式, policy, B1): checked against the job records and the loaded lines, and
# counted apart from the order draft and the control verb -- one build started or continued per message.
_BUILD_KINDS = frozenset({"start_build", "continue_build"})


def _why_refused(proposed: dict[str, Any]) -> str:
    """What to tell the model when the live state would not honour its action.

    A bare "refused" makes the loop try again with the same arguments, which
    costs another paid iteration and fails the same way. Saying which
    assumption was wrong is what lets it look the id up instead.
    """
    kind = str(proposed.get("kind", "") or "")
    if kind == "create_order":
        return (
            "an order draft needs a workflow_id from master_list_order_flows and fields as an object keyed by that "
            "flow's start field ids; nothing was drafted"
        )
    if kind == "reply_checkpoint":
        if str(proposed.get("decision", "") or "").strip().lower() not in {"approve", "deny"}:
            return "decision must be approve or deny; nothing was answered"
        return (
            "that checkpoint is not pending right now -- call master_list_blockers and use an id from "
            "there; nothing was answered"
        )
    if kind == "snooze_blocker":
        return (
            "that checkpoint is not pending right now, or until_at is not an ISO timestamp -- call "
            "master_list_blockers; nothing was held"
        )
    if kind == "run_offline_trial":
        return "a trial needs a spec_id from master_process_drafts; nothing was run"
    if kind == "generate_connector_candidate":
        return "a connector candidate needs a valid formal contract; nothing was generated"
    if kind == "create_improvement_proposal":
        return "an improvement needs one complete schema-valid proposal object; no proposal was recorded"
    if kind == "create_improvement_from_acceptance":
        return (
            "an acceptance-derived improvement needs an order_id, a title, a rationale and one complete candidate "
            "object; no proposal was recorded"
        )
    if kind == "stop_order":
        return (
            "no open order has that id -- call master_list_orders and use an id from there; nothing was "
            "stopped"
        )
    if kind == "delegate_browser":
        return (
            "a browser delegation needs a task_id of an open order from master_list_orders, domains as a list "
            "of hostnames the owner named, and lease_minutes between 1 and 480; a customer's order also needs "
            "real-browser evidence for this machine (the owner runs the probe); nothing was delegated"
        )
    if kind in {"interrupt_turn", "steer_turn"}:
        return (
            "no open order has that id -- call master_list_orders and use an id from there; nothing was "
            "changed, and the order was not stopped either"
        )
    if kind == "start_build":
        return (
            "a build needs the owner's goal (at most 4000 characters) and, to change a line, a line_id from "
            "master_list_builds; nothing was started"
        )
    if kind == "continue_build":
        return (
            "a build goes on only from a job of this project that stopped or handed over (job_id from "
            "master_list_builds) with the owner's instruction; a build still working is steered from its card; "
            "nothing was started"
        )
    return "that action was not recognised; nothing was done"


class MasterConsoleActions:
    """The console, as one master-control turn is allowed to see it.

    Built per turn and thrown away with it, so the per-turn budget (one order,
    one control verb) cannot leak into the next conversation, and so the
    receipts belong to exactly the chat line that caused them.
    """

    def __init__(
        self,
        handler: Any,
        *,
        ws: Any,
        engine: Any,
        project_id: str,
        message_id: str = "",
        business: Any = None,
        console_session_id: str = "",
        thread_id: str = "",
    ) -> None:
        self._handler = handler
        self._ws = ws
        self._engine = engine
        self._project_id = str(project_id or "default")
        self._message_id = str(message_id or "")
        self._business = business
        self._console_session_id = str(console_session_id or "")
        # The conversation this turn belongs to (总控对话栏): '' = the project's first one. The two display events carry
        # it so the page shows 停止 / 插话 and the running step only in that conversation.
        self._thread_id = str(thread_id or "")
        self._snapshot: dict[str, Any] | None = None
        # The single-JSON path's rule, held across the whole loop rather than
        # per call: at most one order and one control verb for this chat line.
        self._order_taken = False
        self._control_taken = False
        # What the turn did, in the order it did it. The console renders the
        # receipts and records the calls; the runtime's TaskResult does not
        # carry them, so they are kept here rather than reconstructed.
        self.calls: list[dict[str, Any]] = []
        self.receipts: list[str] = []
        # Connector candidates this turn drafted, by id. The receipt says so in the owner's words without the id;
        # the reply carries the id in its metadata for the flow-draft card.
        self.connector_candidates: list[str] = []
        self.component_candidates: list[dict[str, Any]] = []
        self.flow_draft_resume: dict[str, Any] | None = None
        # Writes this turn STARTED. A receipt says one finished; this says
        # one may have happened, which is the only question a retry guard
        # can safely ask.
        self._attempted: list[str] = []
        # The work order this turn drafted (policy, M1): it waits in 业务工单 「待接单」; the reply links it.
        self.drafted_order_id = ""
        # A node-table proposal this turn made (at most one): it rides on the reply for the builder canvas.
        self.node_proposal: dict[str, Any] | None = None
        # A new-flow draft this turn compiled (4.7 Q04-B); shares the one-proposal-per-message rule with the above.
        self.flow_draft: dict[str, Any] | None = None
        # Outside components a search returned this turn, by id: what a proposal may put on steps as a candidate.
        self._seen_components: dict[str, dict[str, Any]] = {}
        self._activity_sequence = 0
        # 总控施工模式 (policy, B1): one build started or continued per message, and the job it named.
        self._build_taken = False
        self.build_jobs: list[str] = []

    async def notify_tool_state(self, name: str, state: str) -> None:
        """Ephemeral owner-only display event; never send arguments or results."""
        self._activity_sequence += 1
        await self._handler._send_ack(
            self._ws, ok=True, action="master_tool_activity",
            project_id=self._project_id, message_id=self._message_id,
            sequence=self._activity_sequence, tool_name=name, state=state, thread_id=self._thread_id,
        )

    async def notify_turn_state(self, session_id: str, state: str, *, turn_id: str = "") -> None:
        """Expose only the server-bound session needed for a scoped interrupt."""
        import asyncio
        try:
            await asyncio.wait_for(self._handler._send_ack(
                self._ws, ok=True, action="master_turn_activity", project_id=self._project_id,
                message_id=self._message_id, session_id=session_id, turn_id=turn_id, state=state,
                thread_id=self._thread_id,
            ), timeout=0.5)
        except Exception:
            pass  # A lost display event is not authority to replay the turn.

    # ── the snapshot every read answers from ──

    async def _context(self, *, refresh: bool = False) -> dict[str, Any]:
        from ampuly.plugins.office_ui.master_control import build_system_context

        if refresh or self._snapshot is None:
            self._snapshot = await build_system_context(
                self._engine,
                self._project_id,
                business=self._business,
                console_session_id=self._console_session_id,
            )
        return self._snapshot

    @staticmethod
    def _for_model(context: dict[str, Any]) -> dict[str, Any]:
        """The context minus the parts assembled for the handler, not the model."""
        return {key: value for key, value in context.items() if not key.startswith("_")}

    # ── MasterActions ──

    def may_have_written(self) -> bool:
        """Whether anything this turn started could already have taken effect.

        Deliberately not "did a write succeed". A write that was entered and
        never returned is indistinguishable from one that finished, and the
        safe reading of that ambiguity is that it happened -- re-running the
        message would draft the order twice, and order creation carries no
        idempotency key to catch it.
        """
        return bool(self._attempted or self.receipts)

    async def read(self, name: str, arguments: dict[str, Any]) -> Any:
        args = dict(arguments or {})
        self.calls.append({"name": name, "arguments": args, "writes": False})
        context = self._for_model(await self._context())

        if name == "master_list_orders":
            return {
                "orders": context.get("orders", []),
                "active_order_count": context.get("active_order_count"),
                "blocked_order_count": context.get("blocked_order_count"),
                "total_cost_usd": context.get("total_cost_usd"),
                "cost_call_count": context.get("cost_call_count"),
            }
        if name == "master_order_detail":
            task_id = str(args.get("task_id", "") or "").strip()
            order = next(
                (o for o in list(context.get("orders", []) or []) if str(o.get("task_id", "")) == task_id),
                None,
            )
            if order is None:
                # Not an error: the id may simply be wrong, and saying which
                # is what stops the loop guessing a second time.
                return {
                    "found": False,
                    "task_id": task_id,
                    "note": "no order with that id in this project; call master_list_orders for the ids",
                }
            return {
                "found": True,
                "order": order,
                "pending_checkpoints": [
                    item for item in list(context.get("pending_checkpoints", []) or [])
                    if str(item.get("task_id", "")) == task_id
                ],
            }
        if name == "master_list_blockers":
            return {"pending_checkpoints": context.get("pending_checkpoints", [])}
        if name == "master_capabilities":
            return context.get("capabilities", {})
        if name == "master_process_drafts":
            return {"process_drafts": context.get("process_drafts", [])}
        if name == "master_list_order_flows":
            # The loaded flows are read from disk and the adoptions from the business store: off the event loop.
            return await asyncio.to_thread(self._order_flows)
        if name == "master_connector_candidates":
            from ampuly.plugins.office_ui.connector_candidate import list_candidates, project_candidate_root

            home = getattr(self._engine, "ampuly_home", None)
            if home is None:
                return {"refused": "store_unavailable", "error": "connector candidate store is unavailable"}
            return {
                "connector_candidates": list_candidates(project_candidate_root(home, self._project_id))
            }
        if name == "master_improvement_proposals":
            from ampuly.plugins.office_ui.improvement_proposal import (
                list_improvement_proposals,
                project_improvement_root,
            )

            home = getattr(self._engine, "ampuly_home", None)
            if home is None:
                return {"refused": "store_unavailable", "error": "improvement proposal store is unavailable"}
            return {
                "improvement_proposals": list_improvement_proposals(
                    project_improvement_root(Path(home), self._project_id)
                )
            }
        if name in {"master_list_files", "master_read_file"}:
            return await self._read_files(name, args)
        if name == "master_preflight_candidate":
            return await self._preflight(args)
        if name == "master_search_components":
            # Two catalog files and the internal library are read and parsed: off the console's event loop.
            return await asyncio.to_thread(self._search_components, args)
        if name == "master_propose_candidate_nodes":
            return self._propose_nodes(args)
        if name == "master_propose_flow_draft":
            # The internal library and the team's org file are read and the spec compiled: off the event loop.
            return await asyncio.to_thread(self._propose_flow_draft, args)
        if name == "master_resume_flow_draft":
            return await self._resume_flow_draft(args)
        if name == "master_propose_component_candidate":
            return await self._propose_component_candidate(args)
        if name == "master_list_builds":
            from ampuly.plugins.office_ui.ws_handler import _ws_is_owner

            # Build mode is the owner's (policy): its jobs are listed to the owner's console only.
            if not _ws_is_owner(self._handler, self._ws):
                return {"refused": "owner_required", "error": "only the owner's console sees the build jobs"}
            runner = self._build_runner()
            if runner is None:
                return {"refused": "build_unavailable", "error": "this console has no build mode"}
            # The job records and the loaded lines are read from disk: off the event loop. Writes nothing.
            described = await asyncio.to_thread(runner.describe, self._project_id)
            # 总控对话栏 (policy): every build of the project, each saying which conversation it belongs to
            # (this one or another one, by its title) in place of the conversation's raw id.
            in_words = getattr(self._handler, "_master_builds_in_words", None)
            if callable(in_words):
                described = await in_words(self._project_id, self._console_session_id, described)
            return described
        # The registry is built from MASTER_TOOLS, so this is unreachable
        # unless the two drift apart -- which is exactly when a silent empty
        # answer would be worst.
        return {"error": f"unknown read tool: {name}"}

    def _repo_root(self) -> Path:
        override = getattr(self._handler, "_component_repo_override", None)
        return Path(override) if override else Path(__file__).resolve().parents[3]

    # ── one project is one customer ──
    # A flow derived for another project is that customer's: master control neither reads it, plans it, proposes on
    # it nor names it in a listing (usable_by_project, as the console's own entries do). A package without a manifest
    # (a test double) has no owner and is taken as global.

    def _all_packages(self) -> list[Any]:
        registry = getattr(self._handler, "_workflow_registry", None)
        return list(registry() if callable(registry) else [])

    def _own_packages(self) -> list[Any]:
        """The loaded flows this project may see: global ones and its own."""
        from ampuly.plugins.office_ui.workflow_packages import usable_by_project

        return [
            package for package in self._all_packages()
            if not isinstance(getattr(package, "manifest", None), dict) or usable_by_project(package, self._project_id)
        ]

    def _other_projects_flow(self, package: Any) -> dict[str, Any] | None:
        """The refusal for a flow another project owns, or None when this project may act on it."""
        from ampuly.plugins.office_ui.workflow_packages import usable_by_project

        if not isinstance(getattr(package, "manifest", None), dict) or usable_by_project(package, self._project_id):
            return None
        return {"refused": "other_projects_flow", "error": OTHER_PROJECTS_FLOW}

    # ── 安安起草工单 (policy, M1) ──
    # One answer to "which flows take orders here" (master_order_draft), read the way the console's own entry reads it.

    def _order_flows(self) -> dict[str, Any]:
        from ampuly.plugins.office_ui import master_order_draft as drafting

        return drafting.order_flows(self._all_packages(), self._project_id, drafting.adoption_reader(self._handler))

    def _draft_refusal(self, action: dict[str, Any]) -> dict[str, Any] | None:
        """The draft check before anything is attempted, so a wrong flow or field costs one iteration, not the turn.

        The console handler checks again (the one road still decides); this only keeps a draft that cannot be
        placed from using up the turn's one order. ``owner_note`` is the reason in the owner's words.
        """
        from ampuly.plugins.office_ui import master_order_draft as drafting

        try:
            drafting.check_draft(
                self._all_packages(), self._project_id, str(action.get("workflow_id", "") or ""),
                action.get("fields"), drafting.adoption_reader(self._handler),
            )
        except drafting.DraftRefused as refused:
            return {
                "ok": False, "refused": refused.code, "error": refused.en + "; nothing was drafted",
                "owner_note": refused.zh,
            }
        return None

    def _market_rows(self) -> list[dict[str, Any]]:
        from ampuly.plugins.office_ui.component_search import cached_market_rows

        return cached_market_rows(getattr(self._handler, "__dict__", {}).get("_market_cache"))

    def _remember(self, rows: list[dict[str, Any]]) -> None:
        """Keep what a search returned. A market row wins over a catalog row for the same id (it can be tried), and
        paid wins over free whichever source said it: one source calling a skill paid is enough to hold it back."""
        for row in rows:
            if row.get("source") not in {"skillhub", "catalog"} or not row.get("id"):
                continue
            key = str(row["id"])
            known = self._seen_components.get(key)
            merged = dict(row) if known is None or (row.get("source") == "skillhub" and known.get("source") != "skillhub") else dict(known)
            merged["paid"] = bool(row.get("paid")) or bool((known or {}).get("paid"))
            self._seen_components[key] = merged

    def _search_components(self, args: dict[str, Any]) -> Any:
        """The internal library, the local catalog and the market listing already on this machine (owner-only)."""
        from ampuly.plugins.office_ui.component_search import search_local_components
        from ampuly.plugins.office_ui.ws_handler import _ws_is_owner

        if not _ws_is_owner(self._handler, self._ws):
            return {"refused": "owner_required", "error": "only the owner's console searches the component library"}
        # "Who uses it" names this project's flows and the global ones, not another customer's.
        packages = self._own_packages()
        try:
            result = search_local_components(self._repo_root(), packages, self._market_rows(), str(args.get("query", "") or ""), limit=args.get("limit"))
        except Exception as exc:  # noqa: BLE001 - a failed lookup is a result the model can act on
            return {"refused": "search_failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        self._remember([*result["catalog"], *result["market"]])
        return result

    def _outside_candidates(self, package: Any, preview: Any) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """(candidates, unknown names, paid names): the skills a proposal adds that the save would not declare.

        A name the package declares (or the save declares from the internal library), a name the saved steps already
        carry, or one in the package's own skill folder is not a candidate. Every other name must be a component a
        search returned this turn, or one this machine lists (fetched market listing, local catalog): a name nobody
        found is refused rather than put on a step as if it existed.
        """
        from ampuly.plugins.office_ui.component_search import component_index

        declared = {str(skill) for skill in preview.skills}
        already = {str(skill) for node in package.nodes for skill in list(node.get("skills", []) or [])}
        known: dict[str, dict[str, Any]] | None = None
        library_ready: bool | None = None
        candidates: dict[str, dict[str, Any]] = {}
        unknown: list[str] = []
        paid: list[str] = []
        for node in preview.nodes:
            for skill in list(node.get("skills", []) or []):
                name = str(skill or "").strip()
                if not name or name in declared or name in already:
                    continue
                if name in candidates:
                    if str(node.get("id", "")) not in candidates[name]["node_ids"]:
                        candidates[name]["node_ids"].append(str(node.get("id", "")))
                    continue
                if self._in_own_skill_folder(package, name):
                    continue
                if self._copied_in_at_save(package, name):
                    # A runtime draft takes an internal-library skill by copying it in at the save (policy).
                    continue
                if known is None:
                    known = component_index(self._repo_root(), self._market_rows())
                found = self._seen_components.get(name)
                indexed = known.get(name)
                if found is None:
                    found = indexed
                elif indexed is not None and indexed.get("paid"):
                    found = {**found, "paid": True}
                if found is None:
                    if name not in unknown:
                        unknown.append(name)
                    continue
                if found.get("paid"):
                    if name not in paid:
                        paid.append(name)
                    continue
                source = str(found.get("source") or "")
                if source == "catalog" and library_ready is None:
                    from ampuly.plugins.office_ui.catalog_trial import library_status

                    library_ready = bool(library_status(self._repo_root())["ready"])
                candidates[name] = {
                    "id": name, "source": source, "name": str(found.get("name") or name),
                    "summary": str(found.get("summary") or "")[:300], "category": str(found.get("category") or ""),
                    "node_ids": [str(node.get("id", ""))],
                    # A local catalog skill is tried only from the library named on this machine; the builder says so.
                    "can_try": source != "catalog" or bool(library_ready),
                }
        return list(candidates.values()), unknown, paid

    @staticmethod
    def _copied_in_at_save(package: Any, name: str) -> bool:
        """A runtime draft that copies library skills, naming a plain skill the shared internal library holds."""
        from ampuly.plugins.office_ui.workflow_packages import SHARED_SKILL_LIBRARY, _plain_skill_folder, copies_library_skills

        return copies_library_skills(package) and _plain_skill_folder(SHARED_SKILL_LIBRARY, name)

    @staticmethod
    def _in_own_skill_folder(package: Any, name: str) -> bool:
        """A plain skill id naming a folder that really is inside the package's skills folder, with that exact case."""
        from ampuly.plugins.office_ui.component_catalog import _is_link
        from ampuly.plugins.office_ui.workflow_packages import LIBRARY_SKILL_ID

        if not LIBRARY_SKILL_ID.match(name):
            return False
        root = Path(package.skills_root)
        folder = root / name
        try:
            if not root.is_dir() or name not in {child.name for child in root.iterdir()}:
                return False
            if _is_link(folder) or not (folder / "SKILL.md").is_file():
                return False
            folder.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return True

    async def _preflight(self, args: dict[str, Any]) -> Any:
        """The console's own read-only preflight, summarised for a model.

        The full plan is what the page renders; the model needs the verdict
        and the reasons, not every simulated step's payload.
        """
        from ampuly.plugins.office_ui import master_files as files

        package_id = str(args.get("package_id", "") or "").strip()
        fields = args.get("fields") if isinstance(args.get("fields"), dict) else {}
        try:
            package = files.candidate_package(self._all_packages(), package_id)
        except files.MasterFileRefused as exc:
            return {"refused": exc.reason, "error": str(exc)}
        refused = self._other_projects_flow(package)
        if refused is not None:
            return refused
        console = getattr(self._handler, "_console_api", None)
        if not callable(console):
            return {"refused": "no_console", "error": "this console cannot preflight"}
        try:
            result = console().preflight_workflow(package_id, dict(fields), project_id=self._project_id)
        except Exception as exc:  # noqa: BLE001 - a refusal is a result the model can act on
            return {"refused": "preflight_failed", "error": str(exc)[:500]}
        blockers = [str(b) for b in list(result.get("blockers", []) or [])]
        return {
            "package_id": package_id,
            "runnable": not blockers,
            "blockers": blockers[:20],
            "warnings": [str(w) for w in list(result.get("warnings", []) or [])][:20],
            "step_count": len(list(result.get("steps", []) or [])),
            "simulated_effects": list(result.get("simulated_effects", []) or [])[:20],
        }

    def _propose_flow_draft(self, args: dict[str, Any]) -> Any:
        """需求 → 流程草案 (4.7 Q04-B): compile the model's requirement spec; create and save nothing.

        The spec is the model's; everything checkable is the console's (flow_compiler): components come from the
        internal library only, roles from a saved team, the graph is checked. The draft rides on this reply as a card;
        the owner opens it on the builder, where it becomes an unsaved draft of a new flow and is saved as any other.
        """
        from ampuly.plugins.office_ui.flow_compiler import (
            IntentRefused,
            compile_intent,
            flow_draft_library,
            flow_draft_team,
            normalize_intent,
        )
        from ampuly.plugins.office_ui.ws_handler import _ws_is_owner

        if not _ws_is_owner(self._handler, self._ws):
            return {"refused": "owner_required", "error": "only the owner may have a workflow drafted"}
        if self.node_proposal is not None:
            return {"refused": "one_proposal_per_message", "error": "this reply already carries a proposal; a second one needs another message"}
        # LP-B P-d : one reply still carries one draft, but a later flow-draft call REPLACES the earlier
        # one instead of being refused -- a model that sent half a flow can send the whole of it. A spec that does
        # not compile leaves the earlier draft as it was.
        replacing = self.flow_draft is not None
        try:
            intent = normalize_intent(args)
        except IntentRefused as exc:
            return {"refused": "invalid_spec", "error": str(exc)}
        team, roles = flow_draft_team(intent["team"])
        intent["team"] = team
        # The library is built from this project's flows and the global ones, never another customer's ...
        packages = self._own_packages()
        # ... and this project's registered connectors, for the same reason the console path passes them.
        roots = getattr(self._handler, "_connector_candidate_roots", None)
        registry_root = None
        if callable(roots):
            try:
                _candidate_root, registry_root = roots(self._project_id)
            except Exception:  # noqa: BLE001 - no registry means no API entries, not a failed draft
                registry_root = None
        draft = compile_intent(intent, roles=roles,
                               library=flow_draft_library(self._repo_root(), packages, registry_root=registry_root))
        self.flow_draft = draft
        return {
            "ok": True,
            "proposed": True,
            "created": False,
            "replaced_earlier_draft": replacing,
            "steps": len(draft["nodes"]),
            "team": team or None,
            "matched": [{"step": item["step"], "component": item["component"]} for item in draft["matches"]],
            "missing_components": draft["missing_components"],
            "unassigned_steps": draft["unassigned_steps"],
            "incomplete_steps": draft["incomplete_steps"],
            "undelivered_inputs": draft["undelivered_inputs"],
            "note": "nothing was created or saved; the owner sees this draft on a card, opens it on the builder and saves it there. "
            "Tell the owner what is missing (components, roles, deliverables, open questions) rather than presenting it as finished.",
        }

    async def _retained_component_source(self, args):
        from .flow_draft_resume import validate_reference, resolve_exact_draft
        from .ws_handler import _master_message_by_id, _ws_is_owner
        if not _ws_is_owner(self._handler, self._ws):
            raise ValueError("owner_required")
        ref = validate_reference(args.get("source"), project_id=self._project_id)
        stored = await _master_message_by_id(self._handler.chat_store, self._project_id, ref["message_id"])
        return stored, resolve_exact_draft(stored, ref, project_id=self._project_id)

    async def _resume_flow_draft(self, args):
        from .flow_draft_resume import resolve_materialized_draft
        try:
            stored, draft = await self._retained_component_source(args)
            materialized = await asyncio.to_thread(resolve_materialized_draft, self._own_packages(),
                draft["reference"], project_id=self._project_id)
        except (TypeError, ValueError, OSError) as error:
            return {"refused": getattr(error, "code", "draft_source_unavailable")}
        self.flow_draft = draft
        self.flow_draft_resume = materialized
        return {"ok": True, "source": draft["reference"], "draft": draft,
            "materialized": materialized["materialized"] if materialized else None,
            "effects_produced": False, "note": "Continue this exact original card; no latest-session lookup or draft save occurred."}

    async def _propose_component_candidate(self, args):
        import copy
        from datetime import datetime, timezone
        from .flow_compiler import flow_draft_team, flow_draft_library
        from .flow_component_candidates import build_component_candidate
        if len(self.component_candidates) >= 4:
            return {"refused": "component_candidate_turn_limit"}
        try:
            stored, draft = await self._retained_component_source(args)
            team, roles = await asyncio.to_thread(flow_draft_team, draft["team"])
            if team != draft["team"] or not team:
                return {"refused": "current_team_unavailable"}
            roots = getattr(self._handler, "_connector_candidate_roots", None)
            registry_root = roots(self._project_id)[1] if callable(roots) else None
            library = await asyncio.to_thread(flow_draft_library, self._repo_root(), self._own_packages(),
                registry_root=registry_root)
            candidate = await asyncio.to_thread(build_component_candidate, stored, draft["reference"],
                project_id=self._project_id, step_id=args.get("step_id"), kind=args.get("kind"),
                details=args.get("details"), generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                roles=roles, library=library)
        except (TypeError, ValueError, OSError) as error:
            return {"refused": getattr(error, "code", "component_candidate_unavailable")}
        self.component_candidates.append(candidate)
        if candidate["kind"] == "flow":
            if self.flow_draft is not None or self.node_proposal is not None:
                self.component_candidates.pop()
                return {"refused": "one_proposal_per_message"}
            self.flow_draft = copy.deepcopy(candidate["payload"]["flow_draft"])
            self.flow_draft["component_origin"] = {"source": copy.deepcopy(candidate["source"]), "step_id": candidate["step_id"],
                "candidate_id": candidate["candidate_id"]}
        return {"ok": True, "candidate_id": candidate["candidate_id"], "kind": candidate["kind"],
            "source": candidate["source"], "step_id": candidate["step_id"], "status": "draft",
            "tested": False, "admitted": False, "effects_produced": False,
            "note": "Structured preview attached to this reply; use the existing editor/writer to adopt it as a draft."}

    def _propose_nodes(self, args: dict[str, Any]) -> Any:
        """A node-table change, checked exactly as a save would check it, and kept for the owner to review.

        policy: master control only proposes. Nothing is written here -- the preview is
        the save's own checks without the replace -- and the proposal rides on this reply to the builder
        canvas, where the owner sees what changes, applies it to a draft and saves it with the ordinary save.
        Owner-only and once per message, like the verbs it replaced, because what the owner is shown is still
        a decision to make.
        """
        from ampuly.plugins.office_ui import master_files as files
        from ampuly.plugins.office_ui.workflow_packages import CANVAS_NODE_FIELDS, READ_ONLY_NODE_FIELDS, node_payload, preview_package_nodes
        from ampuly.plugins.office_ui.ws_handler import _ws_actor_login, _ws_is_owner

        if not _ws_is_owner(self._handler, self._ws):
            return {"refused": "owner_required", "error": "only the owner may propose a change to a workflow"}
        if self.node_proposal is not None:
            return {"refused": "one_proposal_per_message", "error": "this reply already carries a proposal; a second one needs another message"}
        package_id = str(args.get("package_id", "") or "").strip()
        expected_version = str(args.get("expected_version", "") or "").strip()
        new_version = str(args.get("new_version", "") or "").strip()
        nodes = args.get("nodes")
        if not package_id or not expected_version:
            return {"refused": "version_required", "error": "a proposal needs package_id and the expected_version you read from workflow.yaml"}
        if not new_version or new_version == expected_version:
            # Two saves of the same version both pass the version check; a proposal has to move it on.
            return {"refused": "new_version_required", "error": "give a new_version different from the version you read"}
        if (
            not isinstance(nodes, list) or not nodes or len(nodes) > MAX_PROPOSED_NODES
            or any(not isinstance(node, dict) for node in nodes)
        ):
            return {"refused": "nodes_required", "error": f"nodes must be a non-empty list of at most {MAX_PROPOSED_NODES} objects"}
        # The canvas shows fields it never edits (standard, writes, branch_when), so a proposal built from what the console
        # was shown carries them back. Carrying them is fine; CHANGING them is not, and that is checked against the
        # package below -- silently dropping a changed value would let the proposal claim a change it did not make.
        known = CANVAS_NODE_FIELDS + READ_ONLY_NODE_FIELDS
        unknown = sorted({str(key) for node in nodes for key in node if key not in known})
        if unknown:
            # The owner reviews and saves on the canvas; a field it cannot show would be changed unseen.
            return {"refused": "unknown_fields", "error": "the builder cannot save these node fields: " + ", ".join(unknown[:10])}
        try:
            package = files.candidate_package(self._all_packages(), package_id)
        except files.MasterFileRefused as exc:
            return {"refused": exc.reason, "error": str(exc)}
        refused = self._other_projects_flow(package)
        if refused is not None:
            return refused
        # A gate's checklist and a step's write fence belong to the package. A proposal may echo back what it was
        # shown; it may not rewrite it, and it may not invent one for a step the package has never seen.
        by_id = {str(node.get("id", "") or ""): node for node in package.nodes}
        for node in nodes:
            node_id = str(node.get("id", "") or "")
            existing = by_id.get(node_id)
            for field in READ_ONLY_NODE_FIELDS:
                if field not in node:
                    continue
                # A text field is one value, not a list of characters; a number there is compared as its text, never
                # passed to list -- list(3) raised and took the whole tool call down. A step's tier is
                # no longer among these fields: the proposal card shows and applies it (CANVAS_NODE_FIELDS).
                if _read_only_values(node.get(field)) != _read_only_values((existing or {}).get(field)):
                    # 4.7 A04: said to the owner in plain words too -- a changed write fence is a step allowed to write
                    # where it could not before, which is exactly what a proposal may never slip through.
                    step_name = str(node.get("name", "") or "") or node_id or "?"
                    return {
                        "refused": "read_only_fields",
                        "error": f"`{field}` on step `{node_id or '?'}` belongs to the package and is shown but never edited; "
                        "propose it in the package, not on the canvas",
                        "owner_note": (
                            f"「{step_name}」这一步的可写位置由流程包本身决定，提案不能改它，也不能让这一步去写以前不能写的地方；要改请在流程包里改。"
                            if field == "writes" else
                            # 4.7 A02: a branch's condition is shown on the canvas since, and read-only like the rest.
                            f"「{step_name}」这一步的分支条件由流程包本身决定，提案只能照原样带回，不能改；要改请在流程包里改。"
                            if field == "branch_when" else
                            f"「{step_name}」这一步的验收标准由流程包本身决定，提案只能照原样带回，不能改；要改请在流程包里改。"
                        ),
                    }
        from ampuly.plugins.office_ui.browser_connector import connector_params

        for node in nodes:
            proposed = node.get("connector")
            if not isinstance(proposed, dict):
                continue
            existing = by_id.get(str(node.get("id", "") or "")) or {}
            now = str(dict(dict(connector_params(existing) or {}).get("verify", {}) or {}).get("mode", "operation") or "operation")
            wants = str(dict(proposed.get("verify", {}) or {}).get("mode", "operation") or "operation") if isinstance(proposed.get("verify", {}), dict) else "operation"
            # Bulk P4: generalised from lookup to every mode that is not `operation`. Both lookup and batch
            # loosen what counts as done, so moving a step OFF the strict default is the owner's, whichever mode
            # it moves to. Moving one back to `operation` is a tightening and master control may still propose it.
            if wants != now and wants != "operation":
                # Bulk P1 (owner ruling): only the owner turns a step into a lookup -- it changes what counts as done.
                return {
                    "refused": "lookup_mode_is_the_owners",
                    "error": f"step `{node.get('id', '?')}`: only the owner sets a browser step to lookup mode (it decides what counts "
                    "as done); propose the other changes and tell the owner, who can switch it on the builder",
                    "owner_note": (
                        f"「{str(node.get('name', '') or '') or node.get('id', '?')}」这一步改成查询或批量方式会放宽「怎样算做完」，"
                        "只能由你本人在搭建台上切换；安安只提其余的改动。"
                    ),
                }
        if str(package.status) not in {"draft", "testing"}:
            # The builder edits draft and testing packages (a draft with no steps yet included, since 4.7 Q04-B), so
            # those are the kinds a proposal can be reviewed and saved on; a published one needs a draft first.
            return {
                "refused": "not_on_builder",
                "error": f"workflow {package.id!r} is {package.status}; only a draft or testing package is edited on the builder canvas",
            }
        change_note = str(args.get("change_note", "") or "").strip()
        try:
            preview = preview_package_nodes(
                package,
                [dict(node) for node in nodes],
                actor=_ws_actor_login(self._handler, self._ws) or "master control",
                expected_version=expected_version,
                new_version=new_version,
                change_note=change_note or "proposed by master control",
            )
        except ValueError as exc:
            return {"refused": "invalid_proposal", "error": str(exc)[:800]}
        from ampuly.layer2_organization.node_contract import employee_bindings_unchanged

        if not employee_bindings_unchanged(package.nodes, preview.nodes):
            # Compare the final merged table, not partial edit fields: omitting
            # a field preserves it, but deleting its node also removes a binding.
            return {
                "refused": "employee_binding_requires_owner",
                "error": "AI 员工绑定不能由普通节点提案增加、更换或移除。",
                "owner_note": "这次改动会改变 AI 员工的责任绑定，需要你本人确认绑定方案；安安可以继续提议其他改动。",
            }
        candidates, unknown, paid = self._outside_candidates(package, preview)
        from ampuly.plugins.office_ui.workflow_packages import copies_library_skills, uses_shared_skill_library

        if candidates and not uses_shared_skill_library(package) and not copies_library_skills(package):
            # A package cut into the runtime home carries its own skills folder: nothing from outside (the internal
            # library included) is declared into it, so a candidate there could never be saved.
            return {
                "refused": "own_skills_package",
                "error": f"workflow {package.id!r} carries its own skills folder; skills from the internal library, the catalog "
                "or the market cannot be added to it: " + ", ".join(item["id"] for item in candidates[:10]),
            }
        if unknown:
            return {
                "refused": "unknown_components",
                "error": "these skills are not in the internal library, the local catalog or a search result this turn: "
                + ", ".join(unknown[:10]) + ". Search with master_search_components and use what it returns; for more of the online market, ask the owner to search it on the Components page.",
            }
        if paid:
            return {"refused": "paid_components", "error": "paid skills are not proposed (no budget or payment channel): " + ", ".join(paid[:10])}
        before = {str(node.get("id", "") or ""): node_payload(node) for node in package.nodes}
        after = [node_payload(node) for node in preview.nodes]
        after_ids = {node["id"] for node in after}

        def _content(node: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in node.items() if key != "canvas"}

        counts = {
            "added": sum(1 for node in after if node["id"] not in before),
            "removed": sum(1 for node_id in before if node_id not in after_ids),
            "changed": sum(1 for node in after if node["id"] in before and _content(node) != _content(before[node["id"]])),
        }
        self.node_proposal = {
            "package_id": str(package.id),
            "package_name": str(package.name),
            "base_version": expected_version,
            "new_version": new_version,
            "change_note": change_note,
            # The table the proposal was checked against. A save that keeps the version number would still match
            # base_version, so the canvas compares this table too before it lets an old proposal be applied.
            "base_nodes": list(before.values()),
            "nodes": after,
            "counts": counts,
            # Outside the internal library: the owner tries and admits each before a draft naming it saves.
            "candidates": candidates,
        }
        return {
            "ok": True,
            "proposed": True,
            "saved": False,
            "package_id": str(package.id),
            "base_version": expected_version,
            "new_version": new_version,
            "counts": counts,
            "candidates": [{"id": item["id"], "source": item["source"]} for item in candidates],
            "note": "nothing was saved; the owner reviews this change on the builder canvas, applies it and saves it there"
            + ("; the outside candidates are tried and admitted by the owner before that draft can be saved" if candidates else ""),
        }

    async def _read_files(self, name: str, args: dict[str, Any]) -> Any:
        """The two disk reads, confined to what the owner granted.

        A refusal comes back as a dict with `refused`, which the registry
        wrapper reports as such and the model can act on; it is never an
        exception, so a wrong path costs the loop one iteration, not the turn.
        """
        from ampuly.plugins.office_ui import master_files as files
        from ampuly.plugins.office_ui.ws_handler import artifact_customer_denied

        package_id = str(args.get("package_id", "") or "").strip()
        task_id = str(args.get("task_id", "") or "").strip()
        path = str(args.get("path", "") or "").strip()
        offset = int(args.get("offset", 0) or 0)
        max_chars = int(args.get("max_chars", files.DEFAULT_MAX_CHARS) or files.DEFAULT_MAX_CHARS)
        try:
            if package_id and task_id:
                return {"refused": "ambiguous_scope", "error": "give package_id or task_id, not both"}
            if package_id:
                package = files.candidate_package(self._all_packages(), package_id)
                refused = self._other_projects_flow(package)
                if refused is not None:
                    return refused
                if name == "master_list_files":
                    return files.list_candidate_files(package)
                return files.read_candidate_file(package, path, offset=offset, max_chars=max_chars)
            if task_id:
                folder_id, order_id = await self._order_folder_for(task_id)
                # The socket that started this turn may be bound to customers;
                # an order it may not see through the console it may not see
                # through the model either.
                _root, prefix = files.order_root(self._engine, self._project_id, folder_id)
                # The folder itself: "<prefix>/." is not a canonical path, and a bound account was refused every order.
                denied, _customer = artifact_customer_denied(self._handler, self._ws, self._project_id, prefix)
                if denied:
                    return {"refused": "customer_scope", "error": "this order is outside the account's customer scope"}
                if name == "master_list_files":
                    return files.list_order_files(self._engine, self._project_id, folder_id, task_id=task_id, order_id=order_id)
                return files.read_order_file(self._engine, self._project_id, folder_id, path, offset=offset,
                                             max_chars=max_chars, task_id=task_id, order_id=order_id)
            if name == "master_read_file":
                return {"refused": "scope_required", "error": "say which candidate package (package_id) or order (task_id) the file belongs to"}
            # Another customer's flow is not on offer, so it is not listed either.
            return {"candidates": files.candidate_packages(self._own_packages()), "note": "pass package_id or task_id to list files"}
        except files.MasterFileRefused as exc:
            return {"refused": exc.reason, "error": str(exc)}
        except ValueError as exc:
            return {"refused": "bad_path", "error": str(exc)}

    async def _order_folder_for(self, named: str) -> tuple[str, str]:
        """The folder an order's files are in, and its business order id (data phase 1).

        What the model names may be the business order (``wo-…``), the task of its run, or one of that run's work
        items; each resolves to ``orders/<wo-…>``. Anything else is taken as the folder of an order made before
        data phase 1 (``orders/<task id>``), with no business order -- for an account bound to customers such a
        folder fails closed, because no order can be found for it.
        """
        from ampuly.core.order_layout import order_segment_ok
        from ampuly.plugins.office_ui.ws_handler import WSHandler

        order = None
        try:
            order = self._handler._console_api().business.get_order(named, project_id=self._project_id)
        except Exception:  # noqa: BLE001 - not an order number
            order = None
        if order is None:
            store = getattr(self._engine, "store", None)
            task = None
            if store is not None:
                try:
                    task = await store.get_task(named)
                except Exception:  # noqa: BLE001
                    task = None
            if task is not None and str(getattr(task, "project_id", "") or self._project_id) == self._project_id:
                order = await WSHandler._order_of_work_item(self._handler, task)
        order_id = str(getattr(order, "order_id", "") or "").strip() if order is not None else ""
        if order_id and order_segment_ok(order_id):
            return order_id, order_id
        return named, ""

    # ── 总控施工模式 (policy, B1) ──

    def _build_runner(self) -> Any:
        """This console's build runner (master_build.MasterBuildRunner), or None where the console has none."""
        getter = getattr(self._handler, "_master_build_runner", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 - no runner is "no build mode here", said as a refusal
            logger.opt(exception=True).warning("master loop: the build runner is unavailable")
            return None

    def _build_action(self, runner: Any, proposed: dict[str, Any]) -> dict[str, Any] | None:
        """The build write the live records would honour, read again now; None when they would not.

        A start names the owner's goal and, to change a line, a line this project may build on. A continue names a job
        of this project that has stopped or handed over (a job that is still working is steered from its card).
        """
        from ampuly.plugins.office_ui.master_build import CONTINUABLE_STATES, MAX_GOAL_CHARS, list_jobs

        kind = str(proposed.get("kind", "") or "")
        if kind == "start_build":
            goal = str(proposed.get("goal", "") or "").strip()
            line_id = str(proposed.get("line_id", "") or "").strip()
            if not goal or len(goal) > MAX_GOAL_CHARS:
                return None
            if line_id and line_id not in {row["line_id"] for row in runner.lines(self._project_id)}:
                return None
            return {
                "kind": "start_build", "goal": goal, "line_id": line_id,
                "name": str(proposed.get("name", "") or "").strip()[:160],
                "team": str(proposed.get("team", "") or "").strip()[:80],
            }
        if kind == "continue_build":
            job_id = str(proposed.get("job_id", "") or "").strip()
            instruction = str(proposed.get("instruction", "") or "").strip()
            if not instruction or len(instruction) > MAX_GOAL_CHARS:
                return None
            jobs = {job.job_id: job for job in list_jobs(runner.host.ampuly_home(), self._project_id)}
            job = jobs.get(job_id)
            # A job nothing here holds is not working, whatever its record says: one a stopped console left marked
            # live is settled as interrupted by the runner before it goes on (MasterBuildRunner.recover).
            if job is None or runner.holds(job_id):
                return None
            if job.state not in CONTINUABLE_STATES and job.state not in {"running", "waiting_owner"}:
                return None
            return {"kind": "continue_build", "job_id": job_id, "instruction": instruction}
        return None

    async def _build_write(self, tool: Any, method: Any, proposed: dict[str, Any]) -> Any:
        """A build started or continued through its console handler, once per message, against the records read now."""
        from ampuly.plugins.office_ui.ws_handler import _ws_is_owner

        kind = str(proposed.get("kind", "") or "")
        if self._build_taken:
            return {"ok": False, "refused": "one build started or continued per message; this turn already did"}
        runner = self._build_runner()
        if runner is None:
            return {"ok": False, "refused": "this console has no build mode; nothing was started"}
        try:
            action = await asyncio.to_thread(self._build_action, runner, proposed)
        except Exception as exc:  # noqa: BLE001 - records that cannot be read are a refusal, not a guess
            return {"ok": False, "refused": f"the build records could not be read ({type(exc).__name__}); nothing was started"}
        if action is None:
            return {"ok": False, "refused": _why_refused(proposed)}
        # Reading the records can outlive the socket's authority; check again right before the handler.
        if tool.needs_owner and not _ws_is_owner(self._handler, self._ws):
            return {"ok": False, "refused": "owner_required"}
        # Before the call, never after: a build started and then cancelled here still started.
        self._build_taken = True
        self._attempted.append(kind)
        self._snapshot = None
        answered = await method(action, ws=self._ws, project_id=self._project_id, message_id=self._message_id)
        ok, note = answered
        if note:
            self.receipts.append(note)
        detail = getattr(answered, "detail", None)
        if ok and isinstance(detail, dict) and detail.get("job_id"):
            self.build_jobs.append(str(detail["job_id"]))
        return {
            "ok": bool(ok), "receipt": note, **({} if ok else {"refused": note}),
            **({"detail": detail} if detail else {}),
        }

    async def write(self, name: str, arguments: dict[str, Any]) -> Any:
        from ampuly.plugins.office_ui.master_control import actions_from_model
        from ampuly.plugins.office_ui.master_tools import by_name, write_handlers
        from ampuly.plugins.office_ui.ws_handler import _ws_is_owner

        args = dict(arguments or {})
        self.calls.append({"name": name, "arguments": args, "writes": True})

        tool = by_name(name)
        if tool is None or not tool.writes:
            return {"ok": False, "refused": f"{name} is not a write this conversation has"}
        if tool.needs_owner and not _ws_is_owner(self._handler, self._ws):
            return {"ok": False, "refused": "owner_required"}

        handler_name = str(write_handlers().get(name, "") or "")
        method = getattr(self._handler, handler_name, None)
        if not callable(method):
            # The declaration names the only legitimate road. If that road is
            # gone, refusing is right: reaching the service another way would
            # be the second path.
            logger.error(f"master loop: {name} names handler {handler_name!r}, which this console does not have")
            return {"ok": False, "refused": f"{name} cannot run: its handler {handler_name} is missing"}

        if name == "master_create_order" and args.get("domains"):
            # policy (M1): a draft carries no websites. The order runs its flow's own browser steps, so the
            # sites are the flow's; a site those steps do not allow is a change to the step, made on the builder.
            return {
                "ok": False, "refused": "domains_not_taken",
                "error": "master_create_order takes no domains: an order works only on the sites its flow's steps allow "
                "(master_list_order_flows lists them); another site means changing that step on the builder "
                "(propose it with master_propose_candidate_nodes). Nothing was drafted.",
                "owner_note": "工单不带网站：它只在业务流步骤允许的网站上操作；要换网站，得先在搭建台改那一步。",
            }

        proposed = _proposed_action(name, args)
        if proposed is None:
            return {"ok": False, "refused": f"{name} produced no action"}

        kind = str(proposed.get("kind", "") or "")
        if kind in _BUILD_KINDS:
            return await self._build_write(tool, method, proposed)
        if kind == "create_order" and self._order_taken:
            return {"ok": False, "refused": "one order per message; this turn already drafted one"}
        if kind != "create_order" and self._control_taken:
            return {"ok": False, "refused": "one control action per message; this turn already used it"}

        # Read the world again before acting on it. A checkpoint that was
        # pending when the loop started may have been answered since, and the
        # loop itself may have changed what is open.
        context = await self._context(refresh=True)
        # The model/context lookup can outlive the socket's authority. Check
        # again immediately before entering the existing command funnel.
        if tool.needs_owner and not _ws_is_owner(self._handler, self._ws):
            return {"ok": False, "refused": "owner_required"}
        validated = actions_from_model([proposed], context)
        if not validated:
            return {"ok": False, "refused": _why_refused(proposed)}
        action = validated[0]

        if kind == "create_order":
            # A draft that cannot be placed (no such flow, not this project's, not taking orders, an unknown or a
            # missing start field) is refused here, before it is attempted: nothing was written, so the model may
            # correct it and try again within the turn.
            refused = await asyncio.to_thread(self._draft_refusal, action)
            if refused is not None:
                return refused
            if tool.needs_owner and not _ws_is_owner(self._handler, self._ws):
                return {"ok": False, "refused": "owner_required"}
            # Before the call, never after. Between "await" and "returned" the
            # order exists; a cancellation delivered in that window leaves the
            # effect done and this function never resuming. Marking it after
            # would tell the fallback nothing was written and let it draft
            # a second one.
            self._order_taken = True
            self._attempted.append("create_order")
            self._snapshot = None
            drafted = await method(action, engine=self._engine, project_id=self._project_id, ws=self._ws)
            note, order_id = drafted
            if note:
                self.receipts.append(note)
            if order_id and not self.drafted_order_id:
                self.drafted_order_id = order_id
            # ``detail`` (ws_handler.MasterAnswer) is for the model only: it never joins the receipts.
            detail = getattr(drafted, "detail", None)
            return {
                "ok": bool(order_id), "order_id": order_id, "receipt": note,
                **({} if order_id else {"refused": note}),
                **({"detail": detail} if detail else {}),
            }

        # Same rule as above: the attempt is the durable fact, not the reply.
        self._control_taken = True
        self._attempted.append(kind)
        self._snapshot = None
        if kind in {
            "run_offline_trial", "snooze_blocker", "generate_connector_candidate",
            "create_improvement_proposal", "create_improvement_from_acceptance", "delegate_browser",
        }:
            # These handlers say whether the thing happened; a refusal (not a
            # candidate, trial not admitted, invalid contract) is a result the
            # model can act on, not a receipt that reads like success.
            answered = await method(
                action, ws=self._ws, project_id=self._project_id, message_id=self._message_id,
            )
            ok, note = answered
            if note:
                # The owner's sentence, in the owner's words. What only the model needs -- ids, fingerprints, the
                # engine's own wording of a refusal -- is the answer's ``detail`` and goes back beside it.
                self.receipts.append(note)
            detail = getattr(answered, "detail", None)
            if ok and kind == "generate_connector_candidate" and isinstance(detail, dict) and detail.get("candidate_id"):
                self.connector_candidates.append(str(detail["candidate_id"]))
            return {
                "ok": bool(ok), "receipt": note, **({} if ok else {"refused": note}),
                **({"detail": detail} if detail else {}),
            }
        note = await method(
            action, ws=self._ws, project_id=self._project_id, message_id=self._message_id,
        )
        if note:
            self.receipts.append(note)
        # The receipt is built from the engine's acknowledgement, so "ok" here
        # means the verb reached the handler -- what the engine decided is in
        # the receipt the owner reads. In Chinese the engine's own code and the
        # ids are its ``detail`` (ws_handler.MasterReceipt), beside it.
        detail = getattr(note, "detail", None)
        return {"ok": True, "receipt": note, **({"detail": detail} if detail else {})}

def _read_only_values(value):
    """A read-only node field as comparable text: a list stays a list of strings, one value becomes a one-item list."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]
