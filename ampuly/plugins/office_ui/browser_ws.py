"""Pure browser helpers lifted out of ws_handler (decomposition step 1).

Each takes an explicit registry / engine / binding / session and reaches only into
ampuly.computer_use / ampuly.layer4_tools -- never into ws_handler -- so ws_handler imports these back
without an import cycle. Behaviour is identical to when they lived in ws_handler.
"""
from __future__ import annotations

from typing import Any
from contextlib import contextmanager
import asyncio
import re
from weakref import WeakValueDictionary


def _browser_error_reason(exc: Exception) -> str:
    reason = str(getattr(exc, "reason", "") or "")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,80}", reason):
        return reason
    known = {"browser_session_not_found", "browser_control_changed", "browser_view_unavailable",
             "browser_view_not_started", "human_not_in_control", "url_required",
             "revocation_reason_required", "unsupported_browser_session_command", "no_hostnames_to_allow",
             "order_not_found_for_session"}
    return str(exc) if str(exc) in known else type(exc).__name__


def _browser_stream_lock(handler: Any, binding_key: str, resource_id: str) -> asyncio.Lock:
    if not hasattr(handler, "_browser_stream_locks"):
        handler._browser_stream_locks = WeakValueDictionary()
    return handler._browser_stream_locks.setdefault((binding_key, resource_id), asyncio.Lock())


@contextmanager
def _browser_dispatch_scope(registry: Any, binding: Any, resource_id: str, control_fence: str, *, human: bool = False):
    """Use the runtime's existing final dispatch gate with this request's identity."""
    from ampuly.layer4_tools.browser import _AGENT_DISPATCH_GUARD
    entry = registry.assert_control(binding, resource_id, control_fence, human=human)
    runtime = entry.runtime
    def allowed() -> bool:
        try:
            current = registry.assert_control(binding, resource_id, control_fence, human=human)
            return current is entry and current.runtime is runtime and not current.unavailable
        except Exception:
            return False
    token = _AGENT_DISPATCH_GUARD.set(allowed)
    try:
        yield
        if not allowed():
            raise ValueError("browser_control_changed")
    finally:
        _AGENT_DISPATCH_GUARD.reset(token)


def _browser_gateway_facts(registry: Any, binding_key: str) -> dict[str, Any]:
    """X05: the same session as the Gateway sees it -- in-flight, last action, proposals -- for the owner list."""
    try:
        from ampuly.computer_use import browser_bridge

        binding = registry.binding_for_key(binding_key)
        if binding is None:
            return {"available": False, "error": "browser_session_not_found"}
        return {"available": True, **browser_bridge.session_facts(binding)}
    except Exception:  # noqa: BLE001 - the list must render even when the Gateway has nothing to say
        return {"available": False, "error": "browser_gateway_unavailable"}


def _browser_combination_evidence(engine: Any) -> dict[str, Any]:
    """X11: what the durable ledger says about running a browser on this machine's executor.

    Read from the product home's copy; the acting model is the engine's
    default. No engine (tests, probes) means no view rather than a guess."""
    if engine is None:
        return {"available": False}
    try:
        from pathlib import Path

        from ampuly.computer_use.combination import CombinationEvidence, browser_admission_view, current_combination, default_ledger_path

        home = getattr(engine, "ampuly_home", None)
        path = Path(home) / "computer_use" / "combination-evidence.json" if home else default_ledger_path()
        llm = getattr(engine, "llm", None)
        model = str(getattr(getattr(llm, "config", None), "default_model", "") or getattr(llm, "default_model", "") or "")
        view = browser_admission_view(CombinationEvidence.load(path), current_combination(acting_model=model))
        return {"available": True, "ledger": str(path), **view}
    except Exception as exc:  # noqa: BLE001 - the list must render; a ledger that cannot be read is said so
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _cancel_browser_actions_for_binding(binding: Any, *, reason: str) -> int:
    try:
        from ampuly.computer_use import browser_bridge

        return len(browser_bridge.cancel_turn(binding, reason=reason))
    except Exception:  # noqa: BLE001 - the owner's control action stands whatever the Gateway bookkeeping did
        return 0


def cancel_order_browsers(project_id: str, task_ids: Any, reason: str) -> dict[str, int]:
    """Q05 B5: an order's cancel reaches its browsers -- the session of each of its work items (by task id, in its
    project): in-flight actions end cancelled_unknown, PULY (JEV) is asked nothing more, and the agent's control ends
    (revoked: a turn still running cannot look again and carry on). The page stays for a person to see. Counts only."""
    from ampuly.computer_use import browser_bridge
    from ampuly.layer4_tools import browser as browser_tools

    registry = browser_tools._browser_sessions
    wanted = {str(t) for t in list(task_ids or []) if str(t or "").strip()}
    reached = cancelled = 0
    for row in list(registry.operator_sessions() or []):
        if str(row.get("project_id", "") or "") != str(project_id or "") or str(row.get("task_id", "") or "") not in wanted:
            continue
        binding = registry.binding_for_key(str(row.get("binding_key", "") or ""))
        if binding is None:
            continue
        reached += 1
        cancelled += _cancel_browser_actions_for_binding(binding, reason=reason)
        try:
            browser_bridge.hold_decider(binding, "order_cancelled")
            registry.revoke(binding, reason=reason)
        except Exception:  # noqa: BLE001 - the turn's cancel above stands; the record says how far it got
            continue
    return {"sessions": reached, "actions_cancelled": cancelled}


def _sync_browser_gateway_lease(registry: Any, binding: Any) -> None:
    try:
        from ampuly.computer_use import browser_bridge

        browser_bridge.sync_lease(registry, binding)
    except Exception:  # noqa: BLE001
        return


def _cancel_browser_actions_for_session(session: Any, *, reason: str) -> int:
    """How many in-flight browser actions of this harness session's task were cancelled (0 when none, or no browser)."""
    try:
        from ampuly.computer_use import browser_bridge
        from ampuly.layer4_tools.browser_sessions import BrowserBinding

        binding = BrowserBinding(project_id=str(getattr(session, "project_id", "") or ""),
                                 customer_id=str(getattr(session, "customer_id", "") or ""),
                                 environment_id=str(getattr(session, "environment_id", "") or ""),
                                 task_id=str(getattr(session, "task_id", "") or ""))
        if not binding.is_bound:
            return 0
        return len(browser_bridge.cancel_turn(binding, reason=reason))
    except Exception:  # noqa: BLE001 - the interrupt is the owner's; a browser bookkeeping failure must not eat it
        return 0
