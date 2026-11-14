"""The desktop connector (桌面应用连接器, 4.7-A06): a flow step that works in an app on this computer.

It sits beside the browser universal connector (browser_connector.py) and follows its rules: placed while a flow is
built -- never swapped in at run time after another channel's outcome was unknown -- named apps only, a stated goal,
done only when code finds the desktop showing the done-when conditions, and the result written into one of the
step's own deliverables. On a step it is one instance in ``nodes[].components``::

    components:
      - instance: desktop
        kind: desktop
        ref: desktop_universal
        version: 1.0.0
        params:
          allowed_apps: [notepad.exe]
          goal: 在记事本里把订单摘要填好并保存
          postconditions: [{kind: window_title_contains, value: 已保存}]
          output: {path: out/desktop-result.md}

The builder edits the same one ``connector`` field it edits for the browser, marked ``kind: desktop``; the save turns
it into this instance (``apply_any_connector_draft``). A step carries one channel: a desktop connector replaces a
browser one on the same step and the other way round.

What this module owns: the parameters and their checks (Chinese findings), the summary the running step reads
(``workflow_node.desktop``), the three tools the step's role is granted at projection, what the step is told, and the
code-written result file. What it does not: the Gateway, the helper and the channel switch (ampuly/computer_use/
desktop_bridge.py). The desktop channel is closed by default: until the owner opens it (配置 · 服务与连接's 「本机桌面通道」 row,
``connector_switch_set``, name ``desktop_channel``, explicit only), a desktop step can be saved and cannot run -- the
kernel stops it before its turn and asks a person (connector_done_wait.desktop_channel_hold), and its tools stop the
turn too. No real desktop helper is bound in this version: a step with the channel open still stops and says the
desktop helper is not connected. A turn that ends DONE without this turn's desktop check standing waits for the
owner (connector_done_wait.as_pause), whatever tool wrote its deliverable.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from ampuly.computer_use import desktop_bridge

DESKTOP_COMPONENT_KIND = "desktop"
DESKTOP_REF = "desktop_universal"
DESKTOP_VERSION = "1.0.0"
DESKTOP_NAME_ZH = "桌面应用连接器"
# The marker the builder puts inside its one `connector` field to say which channel it means.
DRAFT_KIND = "desktop"
# The owner's switch (business_store connector switches). On only when somebody explicitly switched it on.
CHANNEL_SWITCH = "desktop_channel"
DESKTOP_TOOLS: tuple[str, ...] = ("desktop_observe", "desktop_act", "desktop_finish")
POSTCONDITION_KINDS = desktop_bridge.POSTCONDITION_KINDS
POSTCONDITION_LABELS_ZH = {"window_title_contains": "窗口标题包含"}
_APP = re.compile(r'^[^\\/:*?"<>|\r\n\t]{1,80}$')
# What a flow step never drives, whatever the step lists: a shell, a script host, the registry, system settings, an
# installer, the file manager. The helper's vocabulary has no shell, but typing into one would be a shell. A denylist
# has gaps by nature: the step's own allow-list is what the runtime holds the desktop to, this only keeps the obvious
# hosts off it.
NEVER_APPS = frozenset({
    "cmd", "command", "powershell", "powershell_ise", "pwsh", "wt", "windowsterminal", "openconsole", "conhost",
    "bash", "sh", "zsh", "git-bash", "mintty", "wsl", "wslhost", "ssh",
    "python", "python3", "pythonw", "py", "pyw", "node", "deno", "bun", "java", "javaw", "perl", "ruby", "php",
    "wscript", "cscript", "mshta", "cmstp", "forfiles", "pcalua", "msbuild", "installutil",
    "regedit", "regedt32", "reg", "regsvr32", "rundll32", "mmc", "taskmgr", "control", "systemsettings", "msconfig",
    "gpedit", "secpol", "services", "compmgmt", "devmgmt", "diskmgmt", "eventvwr", "resmon", "perfmon",
    "certutil", "bitsadmin", "schtasks", "sc", "netsh", "wmic", "runas",
    "msiexec", "setup", "install", "installer", "winget", "choco",
    "explorer",
})
# Kinds of file that are a script, a console or a system panel whatever they are named -- never a program to work in.
NEVER_EXTENSIONS = frozenset({
    "bat", "cmd", "ps1", "psm1", "psd1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "msc", "cpl", "scr", "lnk",
    "msi", "msp", "reg", "inf", "py", "pyw", "sh", "jar",
})


def never_app(app: Any) -> bool:
    """A shell, script host, system panel or installer: by its program name (``cmd.com`` is ``cmd``) or its kind of file."""
    key = desktop_bridge.app_key(app)
    return key in NEVER_APPS or (key.rsplit(".", 1)[-1] in NEVER_EXTENSIONS if "." in key else False)


def is_desktop_connector(item: Any) -> bool:
    return isinstance(item, dict) and item.get("kind") == DESKTOP_COMPONENT_KIND and item.get("ref") == DESKTOP_REF


def desktop_connector_of(node: dict[str, Any]) -> dict[str, Any] | None:
    """The step's desktop connector instance, or None."""
    for item in list(node.get("components", []) or []):
        if is_desktop_connector(item):
            return item
    return None


def clean_app(raw: Any) -> str:
    """An app as a person names it -- the program's own name, e.g. notepad.exe -- or '' if it is not one."""
    text = str(raw or "").strip()
    if not text or not _APP.match(text) or not desktop_bridge.app_key(text):
        return ""
    return text


def desktop_findings(node: dict[str, Any]) -> list[tuple[str, str, str]]:
    """What is wrong with a step's desktop connector, as (field, code, message), in the owner's words. Empty without one.

    The codes start with ``connector_``: the canvas sends the cursor to the step's connector section for them, and
    the refused save lists them under 「的连接器」.
    """
    items = [item for item in list(node.get("components", []) or [])
             if isinstance(item, dict) and item.get("kind") == DESKTOP_COMPONENT_KIND]
    if not items:
        return []
    found: list[tuple[str, str, str]] = []
    if len(items) > 1:
        found.append(("components", "connector_desktop_twice", "一步只能带一个桌面应用连接器；另一个应用的活请拆成单独一步"))
    item = items[0]
    if item.get("ref") != DESKTOP_REF:
        return [*found, ("components", "connector_desktop_unknown",
                         "这一步的桌面连接器不是本控制台有的（可能是别的版本带来的）；请把连接器类别重新选一次「本机桌面应用」")]
    version = str(item.get("version", "") or "")
    if version and version != DESKTOP_VERSION:
        found.append(("components", "connector_desktop_version",
                      f"桌面应用连接器的版本 {version} 本控制台没有（这里是 {DESKTOP_VERSION}）"))
    if any(isinstance(other, dict) and other.get("kind") == "connector" for other in list(node.get("components", []) or [])):
        found.append(("components", "connector_desktop_with_browser",
                      "一步只走一个通道：这一步已经带了浏览器连接器，桌面应用的活请拆成单独一步"))
    if str(node.get("type", "") or "") != "ai_do":
        found.append(("components", "connector_desktop_not_ai_step", "桌面应用连接器只能放在 AI 步骤上；人做的步骤用自己的电脑"))
    params = dict(item.get("params", {}) or {})
    apps = params.get("allowed_apps")
    if not isinstance(apps, list) or not apps:
        found.append(("components", "connector_desktop_apps_missing", "写明这一步只能操作哪些本机应用（允许的应用，如 notepad.exe）"))
    else:
        bad = [str(app) for app in apps if not clean_app(app)]
        if bad:
            found.append(("components", "connector_desktop_app_invalid",
                          f"允许的应用要写程序名（如 notepad.exe），不能写路径：{'、'.join(bad[:3])}"))
        never = [str(app) for app in apps if clean_app(app) and never_app(app)]
        if never:
            found.append(("components", "connector_desktop_app_not_allowed",
                          f"命令行、脚本、注册表、系统设置、安装程序和文件管理器不能交给流程操作：{'、'.join(never[:3])}"))
    if not str(params.get("goal", "") or "").strip():
        found.append(("components", "connector_desktop_goal_missing", "写明这一步要在应用里做成什么（要做的事）"))
    posts = params.get("postconditions")
    if not isinstance(posts, list) or not posts:
        found.append(("components", "connector_desktop_postconditions_missing",
                      "至少写一条完成条件，由代码在桌面上核对（它自己说做完了不算）"))
    else:
        for index, post in enumerate(posts, 1):
            if (not isinstance(post, dict) or post.get("kind") not in POSTCONDITION_KINDS or set(post) - {"kind", "value"}
                    or not str(post.get("value", "") or "").strip()):
                found.append(("components", "connector_desktop_postcondition_invalid",
                              f"第 {index} 条完成条件要写成「窗口标题包含 某某字」"))
    output = params.get("output")
    deliverables = [str(x) for x in list(node.get("deliverables", []) or [])]
    if not isinstance(output, dict) or not str(output.get("path", "") or "").strip():
        found.append(("components", "connector_desktop_output_missing", "写明结果写到这一步的哪一项交付（输出文件）"))
    elif str(output["path"]).strip() not in deliverables:
        found.append(("components", "connector_desktop_output_not_deliverable",
                      f"输出文件「{output['path']}」不在这一步的交付物里"))
    return found


def desktop_summary(node: dict[str, Any]) -> dict[str, Any] | None:
    """What the running step reads (``workflow_node.desktop``): the connector's parameters, normalised. None without one."""
    item = desktop_connector_of(node)
    if item is None:
        return None
    params = dict(item.get("params", {}) or {})
    apps: list[str] = []
    seen: set[str] = set()
    for raw in list(params.get("allowed_apps", []) or []):
        app = clean_app(raw)
        key = desktop_bridge.app_key(app)
        if app and key not in seen and not never_app(app):
            seen.add(key)
            apps.append(app)
    output = dict(params.get("output", {}) or {}) if isinstance(params.get("output"), dict) else {}
    return {
        "ref": DESKTOP_REF,
        "version": str(item.get("version", "") or DESKTOP_VERSION),
        "allowed_apps": apps,
        "goal": str(params.get("goal", "") or "").strip(),
        "postconditions": [{"kind": str(p.get("kind", "")), "value": str(p.get("value", "")).strip()}
                           for p in list(params.get("postconditions", []) or []) if isinstance(p, dict)],
        "output": {"path": str(output.get("path", "") or "").strip()},
    }


def desktop_fields(node: dict[str, Any]) -> dict[str, Any]:
    """The projected node's desktop key: ``{"desktop": summary}``, or nothing for a step without one."""
    summary = desktop_summary(node)
    return {"desktop": summary} if summary is not None else {}


def desktop_tools(node: dict[str, Any]) -> list[str]:
    """The tools this step's role gets for it (merged into the role at projection, never into node.tools)."""
    return list(DESKTOP_TOOLS) if desktop_connector_of(node) is not None else []


def desktop_params(node: dict[str, Any]) -> dict[str, Any] | None:
    """What the builder shows and edits: the parameters marked ``kind: desktop``, or None without one."""
    item = desktop_connector_of(node)
    return {"kind": DRAFT_KIND, **dict(item.get("params", {}) or {})} if item is not None else None


def apply_any_connector_draft(draft: dict[str, Any], merged: dict[str, Any]) -> None:
    """The builder's one ``connector`` field, for either channel, turned into the step's instance.

    ``kind: desktop`` makes it the desktop connector (a browser connector on the step goes); anything else is the
    browser connector's own rule (browser_connector.apply_connector_draft), after which a desktop connector the
    step had goes too -- a step carries one channel, and the canvas edits only one. A draft without the field leaves
    both as stored.
    """
    if "connector" not in draft:
        return
    value = draft.get("connector")
    if isinstance(value, dict) and str(value.get("kind", "") or "") == DRAFT_KIND:
        from ampuly.plugins.office_ui.node_components import derived_components, normalize_components

        current = merged.get("components")
        base = list(current) if isinstance(current, list) else derived_components(merged)
        before = next((item for item in base if isinstance(item, dict) and item.get("kind") == DESKTOP_COMPONENT_KIND), None)
        kept = [item for item in base
                if not (isinstance(item, dict) and item.get("kind") in (DESKTOP_COMPONENT_KIND, "connector"))]
        kept.append({"instance": str((before or {}).get("instance", "") or "desktop"), "kind": DESKTOP_COMPONENT_KIND,
                     "ref": DESKTOP_REF, "version": str((before or {}).get("version", "") or ""),
                     "params": {key: val for key, val in value.items() if key != "kind"}})
        merged["components"] = normalize_components(str(merged.get("id", "") or ""), kept)
        return
    from ampuly.plugins.office_ui.browser_connector import apply_connector_draft

    apply_connector_draft(draft, merged)
    current = merged.get("components")
    if isinstance(current, list) and any(isinstance(item, dict) and item.get("kind") == DESKTOP_COMPONENT_KIND for item in current):
        merged["components"] = [item for item in current
                                if not (isinstance(item, dict) and item.get("kind") == DESKTOP_COMPONENT_KIND)]


def desktop_prompt(node: dict[str, Any]) -> str:
    """What a step carrying the desktop connector is told. Empty for every other step."""
    summary = desktop_summary(node)
    if summary is None:
        return ""
    apps = "、".join(summary["allowed_apps"]) or "（未指定）"
    posts = "；".join(f"{POSTCONDITION_LABELS_ZH.get(p['kind'], p['kind'])}「{p['value']}」" for p in summary["postconditions"])
    output = summary["output"]["path"] or "指定的交付"
    parts = [
        f"这一步在本机桌面应用里做，只能操作这些程序：{apps}；要做成：{summary['goal'] or '（未写明）'}",
        "先用 desktop_observe 看清前台是哪个程序、窗口标题是什么，再用 desktop_act 一次做一个动作"
        "（click / type / scroll / wait，坐标按画面 0 到 1000 的比例写，如 click(start_box='(500,500)')）",
        "不能启动程序、不能用剪贴板和文件对话框；回车和快捷键这类会提交的按键会被拦下，需要提交就交给人",
        "工具说停下来问人（桌面通道没打开、执行程序没接上、上一次操作结果不明）时就停下，不要换别的办法去做",
        "上一次操作结果不明时，只有人在问题卡片上回复「我看过了，可以接着做」之后，才调用 desktop_observe(person_checked=true)，"
        "然后再接着做；没有人的回复，这样调用也只会再问一次人",
        f"做完调用 desktop_finish 由代码核对完成条件（{posts or '未写明'}）：done_verified 才算完成，结果由代码写到 {output}，"
        "不要自己写这个文件；done_unverified 就写明原因并交给人，不要自称完成",
        "核对只算这一回合在桌面上做的操作；完成条件要在这一回合动手之后才成立，之前就已成立的不算",
        "没有这一回合核对通过的 desktop_finish，用别的工具写了交付物也不算完成：这一步会停下来交给负责人决定",
    ]
    return "；".join(parts)


# ── the running step ─────────────────────────────────────────────────────────

# The option a person picks to say they looked at an input whose outcome was unknown (the only answer that settles it).
PERSON_LOOKED = "person_looked"
_HEADER = "桌面步骤"

_STOP_TEXT: dict[str, dict[str, Any]] = {
    desktop_bridge.STOP_CHANNEL_CLOSED: {
        "reason": "这一步要操作本机桌面应用，桌面通道还没打开。",
        "question": "这一步要操作本机桌面应用，桌面通道还没打开。怎么处理？",
        # The switch is the 「本机桌面通道」 row of 配置 · 服务与连接 (CapabilityCenterPage.tsx is that tab's body, the
        # 渠道连接 group; S3-7 retired the 控制面 page). The card says where it is; no option claims the channel was opened.
        "options": [{"id": "skip_step", "label": "先不做这一步"}, {"id": "resume_when_open", "label": "桌面通道打开以后再接着做"}],
        "note": "桌面通道默认关闭，要负责人明确打开后，流程里的桌面步骤才会运行。打开的地方：配置里「服务与连接」页签的「本机桌面通道」"
                "那一行（在「渠道连接」那一组），点「打开」，再点「确认打开」。"
                "通道没打开时选「接着做」，这一步会再问一次。这一步还没有做任何桌面操作。",
    },
    desktop_bridge.STOP_NO_HELPER: {
        "reason": "这一步要操作本机桌面应用，但本机的桌面执行程序还没接上。",
        "question": "本机的桌面执行程序还没接上，这一步做不了。怎么处理？",
        "options": ["接上以后接着做", "先不做这一步"],
        "note": "这一版只把桌面步骤接进了流程，还没有接真正操作电脑的执行程序。这一步没有做任何桌面操作。",
    },
    desktop_bridge.STOP_UNKNOWN_OUTCOME: {
        "reason": "上一次在本机桌面上的操作结果不明（执行程序失联，或控制台重启了），系统没有重做。",
        "question": "请到电脑上看一眼这一步做到哪了，再决定怎么处理。",
        "options": [{"id": PERSON_LOOKED, "label": "我看过了，可以接着做"}, {"id": "skip_step", "label": "先不做这一步"}],
        "note": "结果不明的操作不会自动再做一遍，以免同一件事在电脑上做两次。您看过之后，它才会接着往下做。",
    },
    desktop_bridge.STOP_PERSON_ASKED: {
        "reason": "这一步在本机桌面上需要人来操作一下。",
        "question": "这一步请人来操作一下，弄好了告诉它。",
        "options": ["我弄好了，接着做", "先不做这一步"],
        "note": "",
    },
    "desktop_frame_approval_required": {
        "reason": "要把当前窗口截图发给视觉模型，需要负责人先批准这一次截图共享。",
        "question": "截图共享还在效果审批里，这一步先停着。批准之后再看一眼才会把画面交给模型。",
        "options": [{"id": "wait_for_frame_approval", "label": "我去效果审批里处理"}],
        "note": "截图还没有发送，桌面也还没有被操作。效果审批通过之前，不能把画面交给模型。",
    },
}


def stop_request(code: str, *, pending: str = "") -> dict[str, Any]:
    """A desktop tool's stop as the ``task_user_input`` pause the runtime parks the turn on (request_user_input).

    ``pending`` (an unknown input's question): the mark of the inputs asked about. The pause is what the task keeps as
    ``requested_user_input`` when the person answers, so the answer can later be matched to exactly these inputs.

    The closed-channel card carries the kernel's channel mark (connector_done_wait.CHANNEL_MARK, LP-B P-h 2026-09-27):
    the hold before the step's next turn (connector_done_wait.desktop_channel_hold) reads only an answer given on a
    card with that mark, so without it the owner who answered this card from a tool's stop was asked the same thing
    again by the hold."""
    from ampuly.layer2_organization.connector_done_wait import CHANNEL_MARK
    from ampuly.layer4_tools.user_input import normalize_user_input_request

    text = _STOP_TEXT.get(code) or _STOP_TEXT[desktop_bridge.STOP_CHANNEL_CLOSED]
    request = normalize_user_input_request(
        reason=text["reason"],
        questions=[{"id": "desktop_step", "header": _HEADER, "question": text["question"],
                    "options": [option if isinstance(option, dict) else {"label": option} for option in text["options"]]}],
        context_note=text["note"],
    )
    return {**request, "stopped": code, **({"desktop_pending": pending} if pending else {}),
            **({CHANNEL_MARK: code} if code == desktop_bridge.STOP_CHANNEL_CLOSED else {})}


def looked_answer(task: Any) -> str:
    """The pending mark of the unknown inputs a person answered 「我看过了，可以接着做」 about, or '' when no such
    answer is on record. Read off what the runtime hands a resumed step: the question it paused on
    (``requested_user_input``, this module's own card) and the person's reply (``user_supplied_input``) -- the console
    card sends ``- 桌面步骤: <label>``, the approval centre the option id, a channel what was typed."""
    snapshot = dict(getattr(task, "context_snapshot", None) or {})
    asked = snapshot.get("requested_user_input")
    if not isinstance(asked, dict) or asked.get("stopped") != desktop_bridge.STOP_UNKNOWN_OUTCOME:
        return ""
    said = " ".join(str(snapshot.get("user_supplied_input", "") or "").lower().split())
    structured = re.search(r"selected_option_id['\"]?\s*[:=]\s*['\"]?([a-z_]+)", said)
    if structured:
        said = structured.group(1)
    else:
        said = said.lstrip("-").strip()
        for colon in (":", "："):
            if said.startswith(_HEADER + colon):
                said = said[len(_HEADER) + 1:].strip()
        # A note the person typed beside the option rides after 「; 」; the option is what counts.
        said = said.split(";", 1)[0].split("；", 1)[0].strip(" 。.!！")
    labels = {PERSON_LOOKED, *(str(option.get("label", "")).lower() for option in _STOP_TEXT[desktop_bridge.STOP_UNKNOWN_OUTCOME]["options"]
                               if isinstance(option, dict) and option.get("id") == PERSON_LOOKED)}
    return str(asked.get("desktop_pending", "") or "") if said in labels else ""


_REFUSAL_ZH = {
    "desktop_not_in_this_step": "这一步没有声明桌面应用连接器，不能操作本机桌面",
    "desktop_unreadable": "读不到本机桌面现在的样子，没有操作",
    "app_not_allowed": "前台的程序不在这一步允许的应用里，没有操作；请先让允许的程序在前台",
    "one_action_at_a_time": "一次只写一个动作",
    "finished_is_not_an_action": "做完了请调用 desktop_finish，由代码核对",
    "not_a_desktop_action": "桌面步骤只能 click / type / scroll / wait；快捷键不做",
    "external_effect_without_independent_verification": "回车、提交这类按键会被拦下，需要提交就交给人",
    "external_effect_not_authorized": "回车、提交这类按键会被拦下，需要提交就交给人",
    "geometry_changed": "窗口在决定动作之后动过了，没有操作；请重新看一眼",
    "coordinate_expired": "坐标过期了，没有操作；请重新看一眼",
    "desktop_frame_expired": "截图许可过期了，没有操作；请负责人重新确认",
    "window_identity_changed": "窗口被重新打开过，原来的坐标作废，没有操作",
    "agent_not_in_control": "桌面正由人操作，没有输入",
    "desktop_fresh_look_required": "交还之后需要重新看一眼，原来的画面不能再用",
    "desktop_not_in_human_control": "桌面现在不是人在操作，不能交还",
    "desktop_takeover_unavailable": "当前不能把桌面交给人",
    "desktop_frame_not_delivered": "当前画面还没有经批准送进视觉模型，没有操作",
    "desktop_frame_observation_changed": "画面在送出之后变了，没有操作；请重新看一眼",
    "desktop_frame_approval_changed": "截图许可已变化，没有操作",
    "desktop_frame_owner_approval_required": "截图共享还没有负责人批准，没有操作",
    "desktop_frame_runtime_source_missing": "当前运行没有绑定视觉模型，没有发送画面",
}


def _step(task: Any) -> tuple[dict[str, Any], str] | None:
    """(the step's desktop summary, its node id) for an execute turn of a step that carries one; else None."""
    from ampuly.layer2_organization.node_contract import is_execute_turn

    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    node = metadata.get("workflow_node")
    summary = node.get("desktop") if isinstance(node, dict) else None
    if isinstance(summary, dict):
        return (summary, str(node.get("id", "") or "")) if is_execute_turn(task) else None
    # A direct task has no workflow node. The package/recipe may still declare
    # the same desktop contract explicitly; it is server-side metadata, never a
    # client permission.
    direct = metadata.get("desktop_direct")
    if (isinstance(direct, dict) and metadata.get("execution_source") in {
            "employee_delegate", "talent_run", "direct_task"}):
        return direct, "direct"
    return None


def _answer(result: dict[str, Any]) -> dict[str, Any]:
    """A bridge result as the model reads it: a stop becomes the pause, a refusal gets its words."""
    stop = str(result.get("stop", "") or "")
    if stop:
        # The pause's own `reason` is what the person reads; the action's outcome rides beside it, under other keys.
        # Frame disclosure also carries the effect checkpoint. The pause does not approve it.
        answered = {**stop_request(stop, pending=str(result.get("pending", "") or "")),
                **{key: result[key] for key in ("kind", "outcome") if key in result},
                **({"why": result["reason"]} if result.get("reason") else {})}
        approval = result.get("approval")
        if isinstance(approval, dict):
            answered["approval"] = approval
        return answered
    refused = str(result.get("refused", "") or "")
    if refused:
        return {**result, "note": _REFUSAL_ZH.get(refused, "没有操作")}
    return result


async def desktop_observe(person_checked: bool = False, task: Any = None) -> dict[str, Any]:
    step = _step(task)
    if step is None:
        return {"ok": False, "refused": "desktop_not_in_this_step", "note": _REFUSAL_ZH["desktop_not_in_this_step"]}
    summary, node_id = step
    # "A person looked" counts only with that person's recorded answer to this step's question (looked_answer).
    result = await desktop_bridge.observe(task, summary, node_id, person_checked=bool(person_checked),
                                          answered=looked_answer(task) if person_checked else "")
    binding = desktop_bridge._project_gate(task)
    frames = getattr(binding, "frames", None) if binding is not None else None
    if frames is not None and result.get("ok"):
        from ampuly.computer_use.desktop_frames import FrameApprovalPending, FrameRefused
        try:
            view = await desktop_bridge._view()
            helper, _broker, _texts = desktop_bridge._helper_objects()
            if view is not None and helper is not None:
                await frames.prepare(task, view, helper)
        except FrameApprovalPending as exc:
            result.update(stop="desktop_frame_approval_required", approval=exc.result)
        except FrameRefused as exc:
            result["frame_refused"] = exc.reason
    return _answer(result)


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "refused": reason, "note": _REFUSAL_ZH.get(reason, "没有操作"), **extra}


async def desktop_act(action: str, task: Any = None) -> dict[str, Any]:
    step = _step(task)
    if step is None:
        return {"ok": False, "refused": "desktop_not_in_this_step", "note": _REFUSAL_ZH["desktop_not_in_this_step"]}
    summary, node_id = step
    blocked = desktop_bridge.input_block(task, node_id)
    binding = desktop_bridge._project_gate(task)
    frames = getattr(binding, "frames", None) if binding is not None else None
    if frames is not None and callable(getattr(frames, "action_view", None)):
        from ampuly.computer_use.desktop_frames import FrameRefused
        # A cleared frame after hand-back is refused before any capture or input.
        # While a person holds the desktop, do not recapture just to refuse.
        sent = getattr(getattr(frames, "vault", None), "last_sent", None)
        if blocked and sent is None:
            return _refused("desktop_frame_not_delivered")
        if blocked:
            return _refused(blocked)
        try:
            view = await desktop_bridge._view()
            identity = desktop_bridge.helper_refusal()
            if identity:
                return _refused(identity)
            helper, _broker, _texts = desktop_bridge._helper_objects()
            if view is None or helper is None:
                raise FrameRefused("desktop_frame_not_delivered")
            await frames.action_view(task, view, helper)
        except FrameRefused as exc:
            return {"ok": False, "refused": exc.reason,
                    "note": _REFUSAL_ZH.get(exc.reason, "截图许可无法核实，没有操作")}
    elif blocked:
        return _refused(blocked)
    result = await desktop_bridge.act(task, summary, node_id, str(action or ""))
    if result.get("ok") is False and not result.get("refused") and not result.get("stop"):
        reason = str(result.get("reason") or "")
        if reason in _REFUSAL_ZH:
            result = {**result, "refused": reason}
    return _answer(result)


def desktop_takeover(task: Any = None) -> dict[str, Any]:
    """Owner API for the later hand-off button. Not a model tool."""
    step = _step(task)
    if step is None:
        return _refused("desktop_not_in_this_step")
    result = desktop_bridge.request_takeover(task, step[1], source="api")
    if result.get("refused"):
        return {**result, "note": _REFUSAL_ZH.get(str(result["refused"]), "没有操作")}
    return result


def desktop_hand_back(task: Any = None) -> dict[str, Any]:
    """Owner API for the later hand-back button. Not a model tool."""
    step = _step(task)
    if step is None:
        return _refused("desktop_not_in_this_step")
    result = desktop_bridge.hand_back(task, step[1])
    if result.get("refused"):
        return {**result, "note": _REFUSAL_ZH.get(str(result["refused"]), "没有操作")}
    return result


def desktop_status(task: Any = None) -> dict[str, Any]:
    """Owner API for the hand-off button's current lease. Not a model tool."""
    step = _step(task)
    if step is None:
        return _refused("desktop_not_in_this_step")
    return desktop_bridge.control_view(task, step[1])


async def desktop_finish(summary: str = "", task: Any = None) -> dict[str, Any]:
    step = _step(task)
    if step is None:
        return {"ok": False, "refused": "desktop_not_in_this_step", "note": _REFUSAL_ZH["desktop_not_in_this_step"]}
    connector, node_id = step
    verdict = await desktop_bridge.finish(task, connector, node_id)
    if verdict.get("stop"):
        return _answer(verdict)
    if verdict.get("status") == "done_verified":
        output = write_result(task, connector, verdict, note=str(summary or ""))
        verdict = {**verdict, "output": output}
        if not output.get("written"):
            verdict["note"] = ("核对通过了，但结果文件没写成（这一步不在订单文件夹里运行，或写入位置不在这一步允许的范围里）；"
                               "不要自己补写，把情况交给人")
    elif verdict.get("status") == "done_unverified":
        # The reason code is for the record; the sentence beside it is what may reach a person (never the code alone).
        verdict = {**verdict, "note": unverified_note(str(verdict.get("reason", "") or ""))}
    return verdict


def unverified_note(reason: str) -> str:
    """A done check that did not pass, in the owner's words, with what the step does next."""
    from ampuly.layer2_organization.connector_done_wait import desktop_why_zh

    return f"代码核对没通过：{desktop_why_zh(reason)}。不要自称完成，也不要改用别的办法写交付物；写明原因交给人。"


_KIND_ZH = {"select_candidate": "点击", "type_draft": "输入文字", "scroll": "滚动", "wait": "等待", "submit": "提交"}
_OUTCOME_ZH = {
    "succeeded": "已核验成功", "outcome_unknown": "结果不明", "refused": "被拒绝，没有操作",
    "failed_before_effect": "没做成（还没碰到桌面）", "failed_after_effect": "做了但没达到预期",
    "cancelled_before_effect": "取消了，没有操作", "cancelled_unknown": "取消时结果不明", "awaiting_human": "等人处理",
}


def render_result(connector: dict[str, Any], verdict: dict[str, Any], *, note: str = "", at: str = "") -> str:
    """The step's result file, in the owner's words: the goal, the app, each condition, what was done. No typed text."""
    lines = ["# 桌面步骤结果", "", f"- 要做成：{connector.get('goal', '') or '（未写明）'}",
             f"- 程序：{verdict.get('app', '')}", f"- 窗口标题：{verdict.get('title', '')}", "- 完成条件（代码核对）："]
    for check in list(verdict.get("checks", []) or []):
        label = POSTCONDITION_LABELS_ZH.get(str(check.get("kind", "")), "其他条件")
        state = ("成立（这一回合动手前就已成立）" if check.get("held_before") else "成立") if check.get("holds") else "不成立"
        lines.append(f"  - {label}「{check.get('value', '')}」：{state}")
    lines.append("- 这一回合做过的操作：")
    for index, entry in enumerate(list(verdict.get("log", []) or []), 1):
        outcome = str(entry.get("outcome", "") or "")
        words = "输入已送达（未单独核验）" if (outcome == "outcome_unknown" and entry.get("reason") == "input_accepted_unverified") \
            else _OUTCOME_ZH.get(outcome, "结果不明")
        lines.append(f"  {index}. {_KIND_ZH.get(str(entry.get('kind', '')), '其他操作')}：{words}")
    if note.strip():
        lines.append(f"- 说明：{' '.join(note.split())[:500]}")
    lines.append(f"- 核对时间：{at or datetime.now().isoformat(timespec='seconds')}")
    return chr(10).join(lines) + chr(10)


def write_result(task: Any, connector: dict[str, Any], verdict: dict[str, Any], *, note: str = "") -> dict[str, Any]:
    """Write the done step's result into its declared deliverable, inside the order's own write fence.

    Code writes this file, not the model, as for a batch step (connector_batch_wait.write_delivery): the fence the
    file tools apply is re-checked here because this runs outside them. A step that works in no order folder, or a
    path the fence refuses, gets no file and says so -- never a file somewhere else.
    """
    relative = str(dict(connector.get("output", {}) or {}).get("path", "") or "").strip()
    if not relative:
        return {"path": "", "written": False, "why": "no_output"}
    try:
        from ampuly.core import item_ledger
        from ampuly.core.order_layout import order_area_of, order_write_decision
        from ampuly.layer2_organization.node_contract import task_write_roots

        area = order_area_of(getattr(task, "project_id", ""), dict(getattr(task, "metadata", {}) or {}))
        if area is None:
            return {"path": relative, "written": False, "why": "no_order_folder"}
        refusal = order_write_decision(area, [relative], task_write_roots(task))
        if refusal is not None:
            return {"path": relative, "written": False, "why": str(refusal)}
        item_ledger.publish(render_result(connector, verdict, note=note), area.output_root / relative)
        return {"path": relative, "written": True}
    except Exception as exc:  # noqa: BLE001 - a file that cannot be written is said, not raised into the turn
        return {"path": relative, "written": False, "why": type(exc).__name__}


# What each desktop tool does once the channel is open and a helper is bound -- declared as the browser's tools of the
# same kind are (ampuly/layer4_tools/browser.py): a look reads (this computer's screen, so local_read, where a page look
# is network_access), an action drives input into another program (external_side_effect, as browser_click / type /
# scroll), the done check reads the desktop and writes the step's deliverable (workspace_write, as a screenshot).
OPEN_EFFECTS: dict[str, tuple[str, ...]] = {
    "desktop_observe": ("local_read",),
    "desktop_act": ("local_read", "external_side_effect"),
    "desktop_finish": ("local_read", "workspace_write"),
}
# While the channel is closed or no helper is bound, a desktop tool does nothing but stop the turn with its own
# question (desktop_bridge.gate): that is all it can do, so that is what it declares -- and the owner is told the
# channel is closed, not first shown a permission card for something that cannot happen.
GATED_EFFECTS: tuple[str, ...] = ("runtime_internal",)


def effects_now(name: str) -> tuple[str, ...]:
    """What the desktop tool ``name`` can do right now, as the permission check reads it."""
    return GATED_EFFECTS if desktop_bridge.gate() else OPEN_EFFECTS.get(name, ("unknown",))


def _desktop_tool_class() -> type:
    from ampuly.layer4_tools.registry import ToolDefinition

    class DesktopTool(ToolDefinition):
        """A ToolDefinition whose permission effects are read at the moment the permission check asks: closed channel
        (or no helper), runtime_internal -- the tool will only stop and say so; open, the effects it really has, and
        the ordinary permission card for them."""

        @property  # type: ignore[override]
        def permission_effects(self) -> tuple[str, ...]:
            return effects_now(self.name)

        @permission_effects.setter
        def permission_effects(self, _value: Any) -> None:
            return None   # declared by OPEN_EFFECTS / GATED_EFFECTS, never by the constructor

    return DesktopTool


def create_desktop_tools() -> list[Any]:
    from ampuly.layer4_tools.registry import COMPANY_EFFECT_NO_LOCAL_FS, COMPANY_EFFECT_RUNTIME_INTERNAL

    ToolDefinition = _desktop_tool_class()
    return [
        ToolDefinition(
            name="desktop_observe",
            description="Look at this computer's desktop for a flow step that carries the desktop connector: which "
                        "program is in front, its window title and size. Set person_checked=true only after a person "
                        "replied that they looked at an earlier action whose outcome was unknown.",
            parameters={"type": "object", "properties": {
                "person_checked": {"type": "boolean", "description": "a person said they looked at the desktop"}}},
            func=desktop_observe, category="desktop", read_only=True,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS, permission_effects=OPEN_EFFECTS["desktop_observe"],
        ),
        ToolDefinition(
            name="desktop_act",
            description="One action in an allowed program on this computer, as click(start_box='(x,y)'), "
                        "type(content='...'), scroll(start_box='(x,y)', direction='down') or wait(); coordinates on a "
                        "0-1000 scale. Keys that submit are refused; the desktop is looked at before each action.",
            parameters={"type": "object", "properties": {"action": {"type": "string", "description": "one action"}},
                        "required": ["action"]},
            func=desktop_act, category="desktop", read_only=False,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS, permission_effects=OPEN_EFFECTS["desktop_act"],
        ),
        ToolDefinition(
            name="desktop_finish",
            description="Have code check the step's done-when conditions on the desktop. done_verified writes the "
                        "result into the step's deliverable; done_unverified says why.",
            parameters={"type": "object", "properties": {
                "summary": {"type": "string", "description": "what was done, in a sentence or two"}}},
            func=desktop_finish, category="desktop", read_only=False,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL, permission_effects=OPEN_EFFECTS["desktop_finish"],
        ),
    ]


def channel_switch_open(switch: dict[str, Any] | None) -> bool:
    """The owner's switch opens the channel only when somebody explicitly switched it on (a switch nobody touched is
    on for every other connector, and must be closed for this one)."""
    return bool(switch) and bool(switch.get("explicit")) and bool(switch.get("enabled"))


def bind_engine(engine: Any, read_switch: Callable[[str], dict[str, Any]] | None) -> None:
    """Give an engine the desktop tools, and this process the owner's switch. Bound on every engine the console
    wires; nothing here binds a helper."""
    registry = getattr(engine, "tool_registry", None)
    if registry is not None and callable(getattr(registry, "register", None)):
        for tool in create_desktop_tools():
            registry.register(tool)
    if read_switch is None:
        return

    def _open() -> bool:
        return channel_switch_open(read_switch(CHANNEL_SWITCH))

    desktop_bridge.bind_channel_check(_open)
