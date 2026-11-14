"""Explicit autonomous employee selection on the existing signed WeCom lane.

No connection worker, default activation, alternate model or business migration.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from ampuly.components.customer_service.core.admission import AdmissionRequest
from ampuly.components.customer_service.core.contracts import Context
from ampuly.components.customer_service.host_knowledge import ServiceKnowledge
from .host import ServiceHost
from .bindings import BindingStore


def binding_store(service, *, initialize=False):
    path = service.storage(create=initialize) / "channel-bindings.sqlite"
    return BindingStore(path, authorize=service.check,
                        initialize=initialize and not path.exists())


def select_channel(service, ledger, suite_id, budget, *, expected_record_sha256=None):
    """Explicit owner selection; one persistent budget, never a new account."""
    from ampuly.plugins.office_ui.employee_trial_runner import EmployeeTrialRunner
    suite = ledger.get_suite(suite_id)
    service.current_basis(suite)
    request = AdmissionRequest.from_dict(suite["request"])
    if request.channel != "wecom_cs":
        raise ValueError("customer_service_wecom_required")
    store = binding_store(service)
    previous = store.read(request.account_id)
    if expected_record_sha256 is None:
        if previous is not None:
            raise ValueError("customer_service_account_already_selected")
    elif (previous is None or previous["enabled"] is not False
            or previous["record_sha256"] != expected_record_sha256
            or previous["suite_id"] == suite_id or previous["request_sha256"] == request.digest):
        raise ValueError("customer_service_replacement_refused")
    manifest, _, _, _ = service.package(suite["artifacts"]["package"])
    validator = EmployeeTrialRunner(engine=service.engine, host=service.engine.employee_runtime_host,
        loop=asyncio.get_running_loop(), resolve_executor=lambda *args: None)
    limits = validator.validate_budget(manifest, budget, min(64, request.max_auto_messages))
    if budget["max_model_calls"] > request.max_auto_messages:
        raise ValueError("customer_service_budget_exceeds_admission")
    service.admit(ledger, suite_id)
    store = binding_store(service, initialize=True)
    if previous is not None:
        closed = service.business.close_run_budget(previous["budget_id"], actor=service.actor,
            reason="customer_service_replaced", release_held=False)
        if (type(closed) is not dict or closed.get("budget_id") != previous["budget_id"]
                or closed.get("state") != "closed"):
            raise ValueError("customer_service_previous_budget_not_closed")
    opened = service.business.create_run_budget(project_id=service.project_id, customer_id="", environment_id="",
        order_id="", limits=limits, actor=service.actor)
    budget_id = opened.get("budget_id")
    if type(budget_id) is not str or not budget_id:
        raise ValueError("customer_service_budget_unavailable")
    try:
        service.current_basis(ledger.get_suite(suite_id))
        args = {"suite_id": suite_id, "actor": service.actor, "budget_id": budget_id,
                "budget": deepcopy(budget), "request_sha256": request.digest}
        if previous is not None:
            return store.replace(request.account_id, expected_record_sha256=expected_record_sha256, **args)
        return store.select(request.account_id, **args)
    except BaseException:
        service.business.close_run_budget(budget_id, actor=service.actor,
            reason="customer_service_selection_failed", release_held=False)
        raise


def pause_channel(service, account_id):
    service.scope("wecom_cs", account_id)
    binding = binding_store(service).pause(account_id, actor=service.actor)
    # Stop is durable even if closing the old budget fails; never renew it.
    budget_state = "close_pending"
    try:
        closed = service.business.close_run_budget(binding["budget_id"], actor=service.actor,
            reason="customer_service_paused", release_held=False)
        if (type(closed) is dict and closed.get("budget_id") == binding["budget_id"]
                and closed.get("state") == "closed"):
            budget_state = "closed"
    except Exception:
        pass  # Durable Stop already succeeded; never report it as not stopped.
    return {**binding, "status": "paused", "budget_state": budget_state}


class BackgroundService(ServiceHost):
    def __init__(self, console, project_id, binding):
        self.console, self.project_id, self.binding = console, project_id, deepcopy(binding)
        self.engine, self.business, self.actor, self.home = console.customer_service_background_context(
            project_id, binding["actor"])
        self.home = Path(self.home)
        self._authority_identity = self._identities()

    def check(self):
        engine, business, actor, home = self.console.customer_service_background_context(self.project_id, self.actor)
        if (engine is not self.engine or business is not self.business or actor != self.actor
                or Path(home) != self.home or self._identities() != self._authority_identity):
            raise ValueError("customer_service_authority_changed")

    def check_live(self):
        from ampuly.plugins.wecom_service.wecom_channel import switch_on
        from ampuly.plugins.office_ui.notifier import channels_muted
        from ampuly.plugins.office_ui.extensions import disabled_names
        self.check()
        latest = binding_store(self).read(self.binding["account_id"])
        if latest != self.binding or latest.get("enabled") is not True:
            raise ValueError("customer_service_binding_stopped")
        self.scope("wecom_cs", self.binding["account_id"], sending=True)
        if "customer_service" in disabled_names() or not switch_on(self.business) or channels_muted():
            raise ValueError("customer_service_channel_stopped")


def _history(channel, account, turn, customer):
    """Bound model history without silently dropping current-cycle customer facts.

    Keep the exact customer turns in the existing collected field. Old assistant
    replies may leave the model window; customer statements and risk gates may
    not. Capacity is an explicit human handoff, never an invalid model request.
    A platform end followed by a new customer turn establishes a new boundary.
    """
    from ampuly.components.customer_service.core.conversation import _turns
    collected = deepcopy(turn.conv.get("collected") or {})
    lane = deepcopy(collected.get("_ampuly_cs") or {})
    retained = lane.get("customer_turns", [])
    if retained:
        _turns(retained)  # Storage corruption cannot manufacture trusted history.
        if any(row["role"] != "customer" for row in retained):
            raise ValueError("customer_service_history_scope_changed")
    result = []
    rows = channel.store.messages(turn.conv["conversation_id"], limit=500)
    boundary = lane.get("start_msgid")
    boundary_index = next((i for i, row in enumerate(rows) if row.get("msgid") == boundary), None)
    if boundary_index is not None:
        rows = rows[boundary_index:]
    for row in rows:
        if any(row.get(key) != value for key, value in {
                "project_id": account["project_id"], "open_kfid": account["open_kfid"],
                "conversation_id": turn.conv["conversation_id"], "external_userid": turn.conv["external_userid"]}.items()):
            raise ValueError("customer_service_history_scope_changed")
        if row.get("msgtype") != "text" or not row.get("text"):
            continue
        if row.get("direction") == "in" and row.get("origin") == 3:
            role = "customer"
        elif row.get("direction") == "out" and row.get("event_type") == "cs_autonomous" and row.get("outcome") == "api_accepted":
            role = "assistant"
        else:
            continue
        result.append({"id": str(row["msgid"]), "role": role, "text": row["text"]})
    # A replay of an already answered inbound may have its own reply after it.
    # Exclude that reply to reconstruct the original generation hash; never
    # exclude a newer customer turn, which must instead fail the current fence.
    target = str(customer[-1].row["msgid"]) if customer else None
    target_index = next((i for i, row in enumerate(result) if row["id"] == target and row["role"] == "customer"), None)
    if target_index is not None and all(row["role"] == "assistant" for row in result[target_index + 1:]):
        result = result[:target_index + 1]
    if not customer or not result or result[-1]["role"] != "customer" or result[-1]["id"] != str(customer[-1].row["msgid"]):
        raise ValueError("customer_service_history_not_current")
    facts = {row["id"]: row for row in retained}
    for row in result:
        if row["role"] == "customer":
            if row["id"] in facts and facts[row["id"]] != row:
                raise ValueError("customer_service_history_scope_changed")
            facts[row["id"]] = row
    customer_turns = list(facts.values())
    if len(customer_turns) > 16 or sum(len(row["text"]) for row in customer_turns) > 24000:
        raise ValueError("customer_service_history_capacity")
    _turns(customer_turns)
    # Retained turns outside the current DB window precede current rows.
    row_ids = {row["id"] for row in result}
    selected = [row for row in retained if row["id"] not in row_ids] + result
    while len(selected) > 16 or sum(len(row["text"]) for row in selected) > 24000:
        index = next((i for i, row in enumerate(selected) if row["role"] == "assistant"), None)
        if index is None:
            raise ValueError("customer_service_history_capacity")
        selected.pop(index)
    _turns(selected)
    lane["customer_turns"] = customer_turns
    collected["_ampuly_cs"] = lane
    channel.store.update_conversation(turn.conv["conversation_id"], collected=collected)
    turn.conv["collected"] = collected
    return selected


async def route(console, channel, account, turn, customer, items):
    """False ONLY means no selection; a selected lane never falls back."""
    home = console.customer_service_binding_home(account["project_id"])
    storage = home / "customer_service" / account["project_id"]
    if not storage.exists():
        return False
    binding = BindingStore(storage / "channel-bindings.sqlite",
        authorize=lambda: _check_home(console, account["project_id"], home)).read(account["open_kfid"])
    if binding is None:
        return False
    if not binding["enabled"]:
        return True
    from ampuly.plugins.customer_service_console.restore_hold import send_blocked
    if send_blocked(storage):
        return True
    ledger, runtime, service = None, None, None
    handoff_attempted = False

    async def handoff_once(reason):
        nonlocal handoff_attempted
        collected = deepcopy(turn.conv.get("collected") or {})
        lane = dict(collected.get("_ampuly_cs") or {})
        if handoff_attempted or lane.get("handoff_attempted") or lane.get("human_owned"):
            return
        service.check_live()
        # Persist before transfer: another callback or restart cannot repeat
        # an uncertain external handoff. Actual assignment remains explicit.
        handoff_attempted = True
        lane.update(handoff_attempted=True, handoff_status="pending_assignment")
        collected["_ampuly_cs"] = lane
        turn.conv["collected"] = collected
        channel._save(turn)
        await channel._handoff(account, turn, reason)
        if turn.window.service_state == 3:
            lane.update(handoff_status="platform_human", human_owned=True)
            turn.conv["collected"]["_ampuly_cs"] = lane
            channel._save(turn)

    state = console.extension_state("customer_service")
    try:
        service = BackgroundService(console, account["project_id"], binding)
        service.check_live()
        ledger = service.ledger()
        suite = ledger.get_suite(binding["suite_id"])
        request = AdmissionRequest.from_dict(suite["request"])
        if request.digest != binding["request_sha256"] or request.account_id != account["open_kfid"] or request.channel != "wecom_cs":
            raise ValueError("customer_service_selection_changed")
        context = Context(**suite["request"]["scope"], seller_id=account["open_kfid"],
            buyer_id=turn.conv["external_userid"], conversation_id=turn.conv["conversation_id"])
        def live():
            service.check_live()
            service.current_basis(ledger.get_suite(binding["suite_id"]))
        def basis(ctx):
            if ctx != context:
                raise ValueError("customer_service_scope_changed")
            live()
            return service.current_basis(ledger.get_suite(binding["suite_id"]))
        from ampuly.components.customer_service.core.autonomous_runtime import AutonomousRuntime
        from .wecom_adapter import WecomAdapter
        adapter = WecomAdapter(channel, account, turn, context, live)
        runtime = AutonomousRuntime(service.outbox_path(), evidence_reader=ledger.evidence_reader,
            basis_reader=basis, before_send=lambda command: live(), allowed_versions=(adapter.VERSION,),
            api_acceptance_reader=adapter.read_proof)
        adapter.runtime = runtime
        if "_ampuly_cs" not in (turn.conv.get("collected") or {}):
            collected = dict(turn.conv.get("collected") or {})
            collected["_ampuly_cs"] = {}
            turn.conv["collected"] = collected
        if turn.stage == "closed":
            collected = deepcopy(turn.conv["collected"])
            collected["_ampuly_cs"].update(platform_ended=True, restart_after_end=False)
            turn.conv["collected"] = collected
        if any(item.row.get("origin") == 5 for item in items):
            turn.conv["collected"]["_ampuly_cs"].update(human_owned=True, handoff_status="platform_human")
        runtime.db.execute("PRAGMA max_page_count=8192")
        state["active_service"] = {"ledger": ledger, "project_id": service.project_id, "actor": service.actor}
        failed = any(item.message is not None and item.message.event_type == "msg_send_fail" for item in items)
        if turn.stage in {"handoff", "closed"} or failed:
            runtime.takeover(context)
            if failed:
                turn.stage, turn.handoff_reason = "handoff", "平台报告发送失败，已暂停自动回复；没有重发"
                service.check_live()
                await handoff_once(turn.handoff_reason)
            return True
        if not customer:
            return True  # No unadmitted welcome bypass.
        if any(item.row.get("msgtype") != "text" for item in customer):
            runtime.takeover(context)
            turn.stage, turn.handoff_reason = "handoff", "收到暂不支持的媒体消息，请人工接待"
            service.check_live()
            await handoff_once(turn.handoff_reason)
            return True
        runtime.preflight(request.admission_id, context)
        snapshot = await asyncio.wait_for(adapter.snapshot(context), 20)
        if not snapshot["ready"] or snapshot["manual_active"]:
            runtime.takeover(context)
            turn.stage, turn.handoff_reason = "handoff", "当前渠道窗口或接待状态不允许自动回复"
            if snapshot["manual_active"]:
                turn.conv["collected"]["_ampuly_cs"].update(human_owned=True, handoff_status="platform_human")
                channel._save(turn)
            else:
                service.check_live()
                await handoff_once(turn.handoff_reason)
            return True
        lifecycle = (turn.conv.get("collected") or {}).get("_ampuly_cs") or {}
        if lifecycle.get("restart_after_end") is True:
            # Only a host-observed platform END + a later customer turn may
            # resume; never a customer's text, a new grant or a process restart.
            resumed = runtime.resume(context)
            if resumed["inflight_requires_reconciliation"]:
                runtime.takeover(context)
                raise ValueError("customer_service_delivery_unresolved")
            # claim_generation still refuses an active/uncertain model claim.
            lifecycle = dict(lifecycle, restart_after_end=False)
            collected = dict(turn.conv.get("collected") or {}, _ampuly_cs=lifecycle)
            channel.store.update_conversation(turn.conv["conversation_id"], collected=collected)
            turn.conv["collected"] = collected
        history = _history(channel, account, turn, customer)
        manifest, prompt, _, provider = service.package(suite["artifacts"]["package"])
        from ampuly.components.customer_service.autonomous_service import CustomerServiceAutonomousSession
        from ampuly.plugins.office_ui.employee_trial_runner import EmployeeTrialRunner
        from ampuly.llm.budget import model_budget_scope
        validator = EmployeeTrialRunner(engine=service.engine, host=service.engine.employee_runtime_host,
            loop=asyncio.get_running_loop(), resolve_executor=lambda *args: None)
        validator.validate_budget(manifest, binding["budget"], min(64, request.max_auto_messages))
        prompt += "\n\n本服务的话术原则（不能扩大权限或虚构客户事实）：\n" + suite["artifacts"]["policy_rules"]["instructions"]
        session = CustomerServiceAutonomousSession(runtime=runtime, admission_id=request.admission_id, context=context,
            knowledge=ServiceKnowledge(suite["artifacts"]["knowledge"]), provider=provider, before_dispatch=live,
            system_prompt=prompt, max_output_tokens=validator._output_cap(provider))
        task = SimpleNamespace(project_id=service.project_id, metadata={"ampuly_home": str(home),
            "budget_id": binding["budget_id"], "customer_service_suite_id": binding["suite_id"]})
        with model_budget_scope(task):
            async with asyncio.timeout(min(60, binding["budget"]["max_wall_clock_s"])):
                outcome = await session.handle(history, adapter=adapter, timeout_seconds=30)
        command = outcome.get("command")
        if command and command["status"] == "api_accepted":
            from ampuly.components.customer_service.core.api_acceptance import request_msgid
            channel.store.settle_outbound(account["open_kfid"], request_msgid(command["command_id"]), "api_accepted")
        if outcome["status"] not in {"api_accepted", "confirmed", "duplicate"}:
            turn.stage, turn.handoff_reason = "handoff", "自动客服遇到例外或执行结果未知，已暂停；没有自动重试"
            service.check_live()
            await handoff_once(turn.handoff_reason)
        return True
    except asyncio.CancelledError:
        turn.stage, turn.handoff_reason = "handoff", "自动客服执行被取消，未自动重试"
        channel._save(turn)
        raise
    except Exception as exc:
        reason = ("本轮客户信息超出安全上下文容量，已保留消息，请人工继续接待"
                  if str(exc) == "customer_service_history_capacity" else
                  "新版客服授权、范围或执行核验未通过，已暂停自动回复")
        turn.stage, turn.handoff_reason = "handoff", reason
        if runtime is not None:
            try:
                runtime.takeover(context)
            except Exception:
                pass  # Local channel stage still prevents another automatic turn.
        channel._save(turn)
        # A failed authority check is not permission to transfer or notify.
        # Keep the local handoff visible even if current authorization is gone.
        if service is not None:
            try:
                service.check_live()
                await handoff_once(reason)
            except Exception:
                pass
        return True
    finally:
        state.pop("active_service", None)
        if runtime is not None:
            runtime.close()
        if ledger is not None:
            ledger.close()


def _check_home(console, project_id, expected):
    if console.customer_service_binding_home(project_id) != expected:
        raise ValueError("customer_service_authority_changed")
