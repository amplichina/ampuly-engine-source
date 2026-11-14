"""Codex through its own Python SDK, when the SDK is installed.

The reuse survey found that Codex ships an official Python SDK (Apache-2.0,
``openai-codex``) whose dependencies are pydantic and its own CLI binary, and
which already covers thread lifecycle, resume, fork, streaming notifications,
approval modes and sandbox settings. Writing another JSON-RPC client against
the same protocol would be duplicated work.

Installing it needs the owner's approval, so this adapter is written behind an
optional import. Without the package it reports exactly that -- unavailable,
naming the dependency -- and refuses to run. What it must never do is answer
as if it had worked: an adapter that fakes a turn is worse than one that is
missing, because the missing one is visible.

The SDK and bundled app-server supply the mature execution loop and transport;
the session mapping, scope, model routing and business budget are SuperOnes'
responsibility. Product API and lifecycle verification is a separate V track;
it does not require a customer's personal Codex account.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import importlib.util
import inspect
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from ampuly.core.models import ExecutionCheckpoint, TaskResult, TaskStatus
from ampuly.layer3_agent.harness import (
    HARNESS_INVALID,
    HARNESS_NOT_FOUND,
    HARNESS_OUTCOME_UNKNOWN,
    HARNESS_SESSION_NOT_LIVE,
    HARNESS_UNSUPPORTED,
    HarnessCallbacks,
    HarnessEvent,
    HarnessOperationResult,
    HarnessProvider,
    HarnessScope,
    _invoke_callback,
)
from ampuly.layer3_agent.harness_sessions import HarnessSessionLost, HarnessSessionRegistry

PROVIDER_NAME = "codex_sdk"

# The notification methods this adapter understands, read from the SDK's own
# registry (``generated/notification_registry.py``) rather than guessed. What
# is not listed here is still recorded -- by its own name -- because an event
# nobody translated is not an event that did not happen.
STREAM_METHODS = {
    "turn/started": "turn_started",
    "item/agentMessage/delta": "assistant_delta",
    "item/started": "item_started",
    "item/completed": "item_completed",
    "turn/completed": "turn_completed",
    "item/commandExecution/outputDelta": "command_output",
    "item/reasoning/summaryTextDelta": "thinking_delta",
}
SDK_PACKAGE = "openai_codex"

# What a recorded action may say about itself. Never the output, never a diff, never the full command line: the record
# is read by people and kept for good, and a command line is where a token typed by an agent ends up.
_COMMAND_PREVIEW_CHARS = 160
_MAX_CHANGED_PATHS = 50
_SECRET_IN_COMMAND = re.compile(r"(?i)\b([\w-]*(?:token|secret|password|passwd|api[_-]?key|authorization|bearer)[\w-]*)(\s*[=:]\s*|\s+)(\S+)")


_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)[^'\"]+")
_KEY_SHAPED = re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[a-z])[-_][A-Za-z0-9_-]{6,}")


def _redacted(command: str) -> str:
    """A command line with what looks like a credential taken out. Best effort, which is why the full command is never
    kept: a header's whole value, anything named like a secret, anything shaped like a provider key."""
    text = _AUTH_HEADER.sub(r"\1<redacted>", command)
    text = _SECRET_IN_COMMAND.sub(r"\1\2<redacted>", text)
    return _KEY_SHAPED.sub("<redacted>", text)


def _plain(value: Any) -> str:
    """The SDK wraps some strings (a working directory, a path) in a root model; ``str()`` of one reads ``root='...'``."""
    return str(getattr(value, "root", value) or "")


def action_projection(item: Any) -> dict[str, Any]:
    """The bounded, secret-free description of one provider item, for the action ledger.

    Every read is defaulted: an item that carries none of these fields still gives a row, and nothing here may raise
    inside the stream loop. What is recorded is what the PROVIDER reported; nothing here was verified by us.
    """
    if item is None:
        return {}
    kind = str(getattr(item, "type", "") or "item")
    out: dict[str, Any] = {"item_id": str(getattr(item, "id", "") or ""), "item_type": kind}
    status = getattr(item, "status", None)
    status_text = str(getattr(status, "value", status) or "")
    if status_text:
        out["status"] = status_text[:40]
    if kind == "commandExecution":
        command = _plain(getattr(item, "command", ""))
        out["command_sha256"] = hashlib.sha256(command.encode("utf-8")).hexdigest() if command else ""
        out["command_preview"] = _redacted(command)[:_COMMAND_PREVIEW_CHARS]
        out["cwd"] = _plain(getattr(item, "cwd", ""))[:300]
        exit_code = getattr(item, "exit_code", None)
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            out["exit_code"] = exit_code
    elif kind == "fileChange":
        changes = list(getattr(item, "changes", []) or [])
        paths = []
        for change in changes[:_MAX_CHANGED_PATHS]:
            change_kind = getattr(change, "kind", "")
            change_kind = getattr(change_kind, "root", change_kind)
            paths.append({"path": _plain(getattr(change, "path", ""))[:300],
                          "kind": str(getattr(change_kind, "type", getattr(change_kind, "value", change_kind)) or "")[:40]})
        out["changes"] = paths
        out["changes_truncated"] = len(changes) > _MAX_CHANGED_PATHS
    elif kind == "mcpToolCall":
        out["server"] = str(getattr(item, "server", "") or "")[:120]
        out["tool"] = str(getattr(item, "tool", "") or "")[:120]
    return out
SDK_DISTRIBUTION = "openai-codex"
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}


# Codex's public approval surface has exactly two settings (read from the
# SDK's own ``_approval_mode.py``):
#
#   deny_all     an escalated permission request is refused;
#   auto_review  the model reviews its own escalation and may approve it.
#
# There is no "ask the operator". Our human gate therefore cannot be handed to
# Codex: when a decision belongs to a person, the only faithful mapping is to
# refuse the escalation and let our own gate ask. Mapping it to auto_review
# would quietly turn "a person must approve" into "the model approved itself",
# which is the exact substitution the gate exists to prevent.
CODEX_DENY_ALL = "deny_all"
CODEX_AUTO_REVIEW = "auto_review"
NO_OPERATOR_PROMPT = (
    "Codex has no ask-the-operator approval mode; an escalation is refused here "
    "and the decision stays with our own gate"
)


def _sdk_approval_mode(mode: str) -> Any:
    """Our string, in whatever shape this build of the SDK insists on.

    Current builds take an ``ApprovalMode`` member and reject a bare
    string before a thread exists -- which a fake client cannot show,
    because it accepts either. Older builds took the string. The value
    is unchanged either way: this decides nothing, it only translates.
    """
    try:
        from openai_codex import ApprovalMode  # noqa: PLC0415 - optional dependency
    except Exception:
        return mode
    try:
        return ApprovalMode(mode)
    except Exception:
        return mode


def codex_approval_mode(posture: str) -> tuple[str, str]:
    """Our security posture -> the closest faithful Codex setting, with a note.

    Anything unrecognised maps to ``deny_all``: an unknown posture is not a
    licence to let the model approve itself.
    """
    clean = str(posture or "").strip().lower()
    if clean == "dangerous":
        # The operator has explicitly accepted self-review for this run. Even
        # here it is worth saying who is doing the approving.
        return CODEX_AUTO_REVIEW, "the model reviews its own escalations; no person is asked"
    return CODEX_DENY_ALL, NO_OPERATOR_PROMPT


class ProviderDependencyMissing(RuntimeError):
    """The Provider's own library is not installed on this machine."""


def sdk_available() -> bool:
    """Whether the official SDK can be imported here. Never installs anything."""
    try:
        return importlib.util.find_spec(SDK_PACKAGE) is not None
    except (ImportError, ValueError):
        return False


def availability() -> dict[str, Any]:
    """What the control plane should say about this adapter today."""
    present = sdk_available()
    return {
        "provider": PROVIDER_NAME,
        "available": present,
        "reason": "" if present else f"dependency_missing:{SDK_DISTRIBUTION}",
        # Being importable is not being verified. Once the package is here
        # the reasons change but do not disappear, and an empty blocker list
        # beside "not production ready" reads as a bug rather than a state.
        "production_ready": False,
        "blockers": (
            [
                "the configured product model API route has not passed production verification",
                "no real turn has been run",
                "the conformance suite has not been run against it",
            ]
            if present
            else [f"install {SDK_DISTRIBUTION} (needs approval)"]
        ),
    }


class CodexSdkHarnessProvider(HarnessProvider):
    """Drives Codex through the official SDK; refuses clearly when it is absent."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        registry: HarnessSessionRegistry,
        client: Any | None = None,
        business: Any | None = None,
        interactions: Any | None = None,
        client_config: Any | None = None,
        model_route: Any | None = None,
        model_gateway: Any | None = None,
        business_tools: Any | None = None,
    ) -> None:
        self.registry = registry
        self._model_gateway = model_gateway
        self._gateway_token = ""
        self._gateway_config_identity = ""
        if model_gateway is not None:
            from ampuly.llm.model_route_http import ManagedModelGateway
            if not isinstance(model_gateway, ManagedModelGateway) or model_gateway.route.registry is not registry:
                raise ValueError("model gateway must use this provider's registry")
            if model_route is not None and model_route is not model_gateway.route:
                raise ValueError("model gateway and revocation route must agree")
            model_route = model_gateway.route
            self._gateway_token = self._validate_gateway_config(client_config, model_gateway)
            from ampuly.layer3_agent.managed_codex import routing_identity
            self._gateway_config_identity = routing_identity(client_config)
        if model_route is not None:
            from ampuly.llm.managed_model_route import ManagedModelRoute
            if not isinstance(model_route, ManagedModelRoute) or model_route.registry is not registry:
                raise ValueError("model route must use this provider's registry")
        self._model_route = model_route
        self._business_tools = business_tools
        if business_tools is not None:
            from ampuly.plugins.office_ui.master_sdk_tools import MasterSdkTools
            if not isinstance(business_tools, MasterSdkTools) or model_gateway is None:
                raise ValueError("business tools require the scoped managed gateway")
        # This adapter currently reserves turns, not measured API cost.
        # Billing comes from the product's configured route, not an assumed
        # personal Codex account. Per-call token/cost settlement remains work
        # for the model gateway; a turn allowance must not claim to cap money.
        self.business = business
        self.interactions = interactions
        self._client_config = client_config
        # A client may be injected for offline protocol tests. When it is not,
        # the SDK must actually be installed -- there is no fallback that
        # pretends to be Codex.
        self._client = client
        self._client_injected = client is not None
        if client is None and not sdk_available():
            raise ProviderDependencyMissing(
                f"{SDK_DISTRIBUTION} is not installed; install it (with approval) or inject a client"
            )
        self._threads: dict[str, Any] = {}
        # The handle of the turn currently running on each session. Steering
        # addresses a turn, not a thread, so a live handle is the only thing
        # that can carry a message into a turn already in flight.
        self._live_turns: dict[str, Any] = {}
        self._live_turn_ids: dict[str, str] = {}
        self._dispatch_ready: dict[str, asyncio.Event] = {}
        self._interrupted: set[str] = set()
        self.interrupt_poll_sec = 0.25
        # Whether this build let us take over approval answering at all.
        self._approvals_gated = self._install_approval_handler(client) if client is not None else False
        # Bound by the layer that owns the gate. Unbound means no.
        self.approval_gate: Any = None
        self._artifacts: dict[str, list[dict[str, Any]]] = {}
        # The SDK invokes server-request handlers on its sole stdout reader
        # thread.  The product coordinator lives on the turn's asyncio loop;
        # this is the explicit bridge between those two existing runtimes.
        self._session_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._pending_approval_calls: dict[str, set[concurrent.futures.Future[Any]]] = {}
        self._pending_approval_lock = threading.Lock()

    def control_transport(self) -> str:
        return "sdk"

    def tool_transport(self) -> str:
        return "provider-native"

    @staticmethod
    def _validate_gateway_config(config: Any, gateway: Any) -> str:
        """Only the exact product route compiler output enables request metering."""
        from types import SimpleNamespace
        from ampuly.layer3_agent.managed_codex import MODEL_KEY, TOOL_POLICIES, model_route

        overrides = tuple(getattr(config, "config_overrides", ()) or ())
        env = dict(getattr(config, "env", {}) or {})
        token = env.get(MODEL_KEY)
        if not token or not overrides or not overrides[0].startswith("model="):
            raise ValueError("gateway runtime configuration required")
        try:
            model = json.loads(overrides[0][len("model="):])
            expected, _ = model_route(SimpleNamespace(
                model=model, model_wire_api="responses", model_api_base=gateway.base_url,
                model_api_key_env=MODEL_KEY,
            ), {MODEL_KEY: token})
        except (ValueError, TypeError):
            raise ValueError("invalid gateway runtime configuration") from None
        # The route lines exactly, followed by exactly one named tool policy:
        # anything else is not a product-compiled runtime configuration.
        if overrides[:len(expected)] != expected or overrides[len(expected):] not in TOOL_POLICIES.values():
            raise ValueError("runtime must use only the managed gateway route")
        return token

    def _budget_reference(self, session_id: str) -> str:
        """Restore authority from the session journal, never an empty cache."""
        return self._budget_binding(session_id)[0]

    def _budget_binding(self, session_id: str) -> tuple[str, str]:
        """``(budget id, the order that budget answers to)`` as the session journal recorded them at creation.

        The second is empty for a session whose budget is its own order's. A child order's session spends from the
        allowance of the order above it and keeps its own order (cancelling and resuming find it by that), so the
        order its budget answers to is recorded beside the budget -- once, by the server, when the session is made.
        """
        from ampuly.llm.budget import ModelBudgetStop

        entries = self.registry.events_after(session_id, event_type="session_created", limit=2)
        if len(entries) != 1:
            raise ModelBudgetStop("SDK budget binding missing or ambiguous; explicit reconciliation required")
        binding = entries[0].data.get("budget_binding")
        if not isinstance(binding, dict):
            raise ModelBudgetStop("SDK legacy budget binding unknown; explicit reconciliation required")
        if binding == {"mode": "unbounded"}:
            return "", ""
        budget_id = binding.get("budget_id")
        answers_to = binding.get("budget_order_id", "")
        if (set(binding) not in ({"mode", "budget_id"}, {"mode", "budget_id", "budget_order_id"})
                or binding.get("mode") != "bounded"
                or not isinstance(budget_id, str) or not budget_id or len(budget_id) > 256
                or not isinstance(answers_to, str) or len(answers_to) > 256
                or ("budget_order_id" in binding and not answers_to)):
            raise ModelBudgetStop("SDK budget binding invalid; explicit reconciliation required")
        return budget_id, answers_to

    def _hold_a_turn(self, session_id: str, turn_id: str) -> Any | None:
        """Reserve one turn against the order's durable allowance binding."""
        budget_id, answers_to = self._budget_binding(session_id)
        if not budget_id:
            if self._model_gateway is not None:
                from ampuly.llm.budget import ModelBudgetStop
                raise ModelBudgetStop("model gateway requires a bounded session")
            return None
        from ampuly.layer4_tools.spend_guard import SpendGuard
        from ampuly.llm.budget import ModelBudgetStop

        if self.business is None:
            raise ModelBudgetStop("SDK budget store unavailable; turn not dispatched")
        budget = self.business.run_budget(budget_id)
        session = self.registry.session(session_id)
        if not budget or session is None:
            raise ModelBudgetStop("SDK budget or session missing; turn not dispatched")
        expected = {field: getattr(session, field) for field in ("project_id", "customer_id", "environment_id", "order_id")}
        if answers_to:
            expected["order_id"] = answers_to
        if any(str(budget.get(field, "") or "") != wanted for field, wanted in expected.items()):
            raise ModelBudgetStop("SDK budget scope mismatch; turn not dispatched")
        if "cost_cny" in dict(budget.get("limits", {}) or {}):
            raise ModelBudgetStop("SDK turn allowance cannot enforce a monetary ceiling; turn not dispatched")

        if self._model_gateway is not None:
            # The gateway reserves each actual API request. Merely supplying
            # a revocation route does not opt the direct path out of charging.
            return None

        reservation = self.business.reserve_budget(
            budget_id, kind="model_call", amount=1, actor=self.provider_name,
            idempotency_key=f"{session_id}:{turn_id}", note="codex turn",
        )
        return SpendGuard(self.business, budget_id, str(reservation["reservation_id"]), "model_call")

    async def _reserve_a_turn(self, session_id: str, turn_id: str) -> Any | None:
        from ampuly.llm.budget import _finish_thread, _finish_ledger

        guard, cancelled = await _finish_thread(self._hold_a_turn, session_id, turn_id)
        if cancelled:
            if guard is not None:
                await _finish_ledger(guard.release, reason="cancelled before SDK turn dispatch")
            raise asyncio.CancelledError
        return guard

    @staticmethod
    async def _finish_spend(guard: Any, operation: str, **kwargs: Any) -> None:
        from ampuly.llm.budget import _finish_ledger

        _, cancelled = await _finish_ledger(getattr(guard, operation), **kwargs)
        if cancelled:
            raise asyncio.CancelledError

    async def _stream_turn(
        self,
        session_id: str,
        turn_id: str,
        turn: Any,
        callbacks: HarnessCallbacks | None,
        guard: Any | None = None,
    ) -> TaskResult:
        """Follow the turn's own event stream and write each one down.

        A stream that ends without ``turn/completed`` is not a finished turn;
        reporting one as done would turn a dropped connection into a delivery.
        """
        text_parts: list[str] = []
        completed_status = ""
        saw_completion = False
        try:
            async for event in turn.stream():
                method = str(getattr(event, "method", "") or "")
                payload = getattr(event, "payload", None)
                kind = STREAM_METHODS.get(method, method or "event")
                data: dict[str, Any] = {"method": method}
                if method == "item/agentMessage/delta":
                    delta = str(getattr(payload, "delta", "") or "")
                    if delta:
                        text_parts.append(delta)
                        data["text"] = delta
                        await _invoke_callback(getattr(callbacks, "on_progress", None), delta)
                elif method == "item/started":
                    # The other half of the ledger: an action that was started and has no completion on record is
                    # what a person has to look at after a crash. Only the identity and a bounded description.
                    data.update(action_projection(getattr(getattr(payload, "item", None), "root", None)))
                elif method == "item/completed":
                    # What the turn produced, as the provider itself described
                    # it. Recorded verbatim: an item we do not recognise is
                    # still something that happened in this run.
                    item = getattr(getattr(payload, "item", None), "root", None)
                    entry = {
                        "turn_id": turn_id,
                        "item_id": str(getattr(item, "id", "") or ""),
                        "type": str(getattr(item, "type", "") or "item"),
                        "text": str(getattr(item, "text", "") or ""),
                    }
                    self._artifacts.setdefault(session_id, []).append(entry)
                    data["item_type"] = entry["type"]
                    data.update(action_projection(item))
                elif method == "error":
                    # Keep what it said. A recorded "error" with no message
                    # is the same as no record, except that it looks like one.
                    message = str(
                        getattr(payload, "message", "")
                        or getattr(payload, "error", "")
                        or getattr(payload, "text", "")
                        or ""
                    ).strip()
                    if not message:
                        message = str(payload)[:500] if payload is not None else ""
                    data["message"] = message[:500]
                    code = str(getattr(payload, "code", "") or "").strip()
                    if code:
                        data["code"] = code
                elif method == "turn/completed":
                    saw_completion = True
                    status = getattr(getattr(payload, "turn", None), "status", None)
                    completed_status = str(getattr(status, "value", status) or "")
                    data["status"] = completed_status
                self.registry.record_event(session_id, event_type=kind, turn_id=turn_id, data=data)
        except asyncio.CancelledError:
            self.registry.record_event(
                session_id, event_type="turn_cancelled", turn_id=turn_id,
                data={"rollback_claimed": False}, state_after="suspended",
                state_reason="turn cancelled; effect outcomes require their own verification",
                settle_turn=True,
            )
            if guard is not None:
                await self._finish_spend(guard, "settle")
            raise
        except Exception as exc:
            event_type = "turn_cancelled" if session_id in self._interrupted else "turn_failed"
            self.registry.record_event(
                session_id, event_type=event_type, turn_id=turn_id,
                data={"error_type": type(exc).__name__},
                state_after="suspended",
                state_reason=(
                    "turn interrupted; effect outcomes require their own verification"
                    if event_type == "turn_cancelled"
                    else f"stream failed: {type(exc).__name__}"
                ),
                settle_turn=True,
            )
            if guard is not None:
                if event_type == "turn_cancelled":
                    await self._finish_spend(guard, "settle")
                else:
                    await self._finish_spend(guard, "unknown", reason=f"{type(exc).__name__} mid-stream")
            raise

        text = "".join(text_parts)
        if not saw_completion:
            # The stream stopped mid-turn. Partial text is not a result.
            self.registry.record_event(
                session_id, event_type="turn_incomplete", turn_id=turn_id, data={"chars": len(text)},
            )
            self.registry.set_state(session_id, "suspended", reason="stream ended without a completion")
            if guard is not None:
                # The turn ran; only its ending is missing. Refunding here
                # would let a retry spend the same allowance twice.
                await self._finish_spend(guard, "unknown", reason="stream ended without turn/completed")
            raise HarnessSessionLost(
                f"session {session_id!r}: the stream ended without turn/completed; the turn is not finished"
            )

        interrupted = session_id in self._interrupted or completed_status.lower() in {
            "cancelled", "canceled", "interrupted",
        }
        failed = completed_status and completed_status.lower() not in {"completed", "success", "ok"}
        terminal_type = "turn_cancelled" if interrupted else "turn_failed" if failed else "turn_finished"
        self.registry.record_event(
            session_id, event_type=terminal_type, turn_id=turn_id,
            data={"chars": len(text), "status": completed_status, "rollback_claimed": False},
            state_after="suspended" if interrupted or failed else "",
            state_reason=(
                "turn interrupted; effect outcomes require their own verification"
                if interrupted
                else f"turn ended {completed_status}" if failed else ""
            ),
            settle_turn=True,
        )
        if guard is not None:
            await self._finish_spend(guard, "settle")
        return TaskResult(
            status=TaskStatus.CANCELLED if interrupted else TaskStatus.FAILED if failed else TaskStatus.DONE,
            content=text,
            artifacts={
                "turn_id": turn_id,
                "session_id": session_id,
                "provider_session_id": str(
                    getattr(self._threads.get(session_id), "id", "") or ""
                ),
                "provider_status": completed_status,
            },
        )

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from ampuly.layer3_agent.managed_codex import managed_client

            self._client = managed_client(self._client_config)
            self._approvals_gated = self._install_approval_handler(self._client)
        return self._client

    async def _start_sdk_thread(
        self, client: Any, scope: HarnessScope, *, model: str = "", security_posture: str = ""
    ) -> tuple[Any, str, str]:
        """Start an official thread with the narrowest truthful approval mode.

        The high-level SDK deliberately offers only deny-all or model
        auto-review.  Its public typed low-level client also exposes the
        exact app-server fields, so human review uses that existing path:
        on-request plus no approvals reviewer.  No JSON-RPC is reimplemented.
        """
        if self._business_tools is not None:
            return await self._start_business_thread(client, scope, model=model)
        if self.interactions is None:
            approval_mode, note = codex_approval_mode(security_posture)
            kwargs: dict[str, Any] = {
                "model": model or None,
                "approval_mode": _sdk_approval_mode(approval_mode),
            }
            # The real SDK accepts both; injected protocol doubles from older
            # tests may expose only the original two arguments.
            try:
                from openai_codex import Sandbox  # noqa: PLC0415

                kwargs.update(cwd=scope.workspace_path, sandbox=Sandbox.workspace_write)
            except Exception:
                if not self._client_injected:
                    raise
            try:
                thread = await client.thread_start(**kwargs)
            except TypeError:
                if not self._client_injected:
                    # A production TypeError may follow dispatch; retrying
                    # without cwd/sandbox would weaken authority and may
                    # duplicate the request. Compatibility is for doubles only.
                    raise
                thread = await client.thread_start(
                    model=kwargs["model"], approval_mode=kwargs["approval_mode"]
                )
            return thread, approval_mode, note

        if not self._approvals_gated:
            raise RuntimeError(
                "the official SDK approval handler is not controllable; refusing human-review mode"
            )
        ensure = getattr(client, "_ensure_initialized", None)
        transport = getattr(client, "_client", None)
        start = getattr(transport, "thread_start", None)
        if not callable(ensure) or not callable(start):
            raise RuntimeError(
                "this SDK build hides its typed thread/start transport; refusing to substitute auto-review"
            )
        from openai_codex.api import AsyncThread  # noqa: PLC0415
        from openai_codex.generated.v2_all import (  # noqa: PLC0415
            AskForApproval,
            AskForApprovalValue,
            SandboxMode,
            ThreadStartParams,
        )

        await ensure()
        params = ThreadStartParams(
            approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
            approvals_reviewer=None,
            cwd=scope.workspace_path,
            model=model or None,
            sandbox=SandboxMode.workspace_write,
        )
        started = await start(params)
        return (
            AsyncThread(client, str(started.thread.id)),
            "operator_review",
            "app-server asks the SuperOnes owner checkpoint; no model reviewer is configured",
        )

    async def _start_business_thread(self, client: Any, scope: HarnessScope, *, model: str) -> Any:
        from openai_codex.api import AsyncThread
        from openai_codex.generated.v2_all import AskForApproval, AskForApprovalValue, SandboxMode, ThreadStartParams
        if not self._approvals_gated or not getattr(self._client_config, 'experimental_api', False):
            raise RuntimeError('SDK dynamic tool routing unavailable')
        await client._ensure_initialized()
        policy = AskForApprovalValue.on_request if self.interactions is not None else AskForApprovalValue.never
        payload = ThreadStartParams(cwd=scope.workspace_path, model=model or None,
            approval_policy=AskForApproval(root=policy), approvals_reviewer=None,
            developer_instructions=self._business_tools.instructions,
            sandbox=SandboxMode.workspace_write).model_dump(mode='json', by_alias=True, exclude_none=True)
        payload['approvalsReviewer'] = None
        # The official transport accepts JSON alongside its public typed model.
        # Use that extension point; never silently drop the experimental field.
        payload['dynamicTools'] = self._business_tools.specs()
        started = await client._client.thread_start(payload)
        return AsyncThread(client, str(started.thread.id)), 'operator_review' if self.interactions else CODEX_DENY_ALL, 'business tools use SuperOnes handlers'

    async def _resume_sdk_thread(self, client: Any, session_id: str, provider_thread_id: str) -> Any:
        """Resume with the same human-review and workspace boundaries."""
        if self._business_tools is not None:
            raise RuntimeError('owned business-tool attempts cannot resume with a stale capability set')
        created = self.registry.events_after(session_id, 0, limit=2, event_type="session_created")
        workspace = str(created[0].data.get("workspace", "") or "") if len(created) == 1 else ""
        if not workspace or not Path(workspace).is_absolute():
            raise HarnessSessionLost("durable workspace scope is missing; refusing an unscoped resume")
        if self.interactions is None:
            if created[0].data.get("approval_mode") == "operator_review":
                raise RuntimeError("operator-review session requires its approval coordinator")
            from openai_codex import Sandbox

            kwargs = {"cwd": workspace, "sandbox": Sandbox.workspace_write,
                      "approval_mode": _sdk_approval_mode(CODEX_DENY_ALL)}
            # Only legacy offline doubles may omit parameters, established
            # before dispatch. A TypeError from inside resume is never retried.
            if self._client_injected:
                try:
                    inspect.signature(client.thread_resume).bind(provider_thread_id, **kwargs)
                except TypeError:
                    return await client.thread_resume(provider_thread_id)
            return await client.thread_resume(provider_thread_id, **kwargs)
        if not self._approvals_gated:
            raise RuntimeError("the SDK approval handler is not controllable")
        ensure = getattr(client, "_ensure_initialized", None)
        transport = getattr(client, "_client", None)
        resume = getattr(transport, "thread_resume", None)
        if not callable(ensure) or not callable(resume):
            raise RuntimeError("this SDK build hides its typed thread/resume transport")
        from openai_codex.api import AsyncThread  # noqa: PLC0415
        from openai_codex.generated.v2_all import (  # noqa: PLC0415
            AskForApproval,
            AskForApprovalValue,
            SandboxMode,
            ThreadResumeParams,
        )

        await ensure()
        params = ThreadResumeParams(
            thread_id=provider_thread_id,
            approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
            approvals_reviewer=None,
            cwd=workspace,
            sandbox=SandboxMode.workspace_write,
        )
        resumed = await resume(provider_thread_id, params)
        return AsyncThread(client, str(resumed.thread.id))

    def _install_approval_handler(self, client: Any) -> bool:
        """Take over the SDK's approval answering, or admit we could not.

        The SDK's own default accepts command execution and file changes.
        Left in place it means a library signs for work this product is
        supposed to gate, with no record of having done so. The handler
        is a constructor argument on the inner sync client, which the
        async facade does not forward, so it is installed here -- and if
        the attribute is not there, this says False rather than pretend
        the approvals are ours.
        """
        for holder in (getattr(getattr(client, "_client", None), "_sync", None), getattr(client, "_client", None), client):
            if holder is not None and hasattr(holder, "_approval_handler"):
                holder._approval_handler = self._answer_approval
                return True
        return False

    def _answer_approval(self, method: str, params: Any = None) -> dict[str, Any]:
        """What this product says when Codex asks permission.

        Called from the SDK's reader thread.  When the durable interaction
        coordinator is bound, that reader is deliberately held while the
        owner decides; the asyncio control plane remains live on its own
        thread.  This is the SDK's own request/response pause, not a second
        JSON-RPC implementation.
        """
        if method == 'item/tool/call':
            return self._answer_business_tool(params)
        request = dict(params or {})
        method = str(method or "")
        if method not in _APPROVAL_METHODS:
            return {}
        # App-server approval requests carry their authoritative threadId.
        # Resolve that provider id back through our session mapping.  A single
        # mutable "active session" races as soon as two customers run turns
        # concurrently and can let one session answer another one's prompt.
        provider_thread_id = str(
            request.get("threadId", "") or request.get("thread_id", "") or ""
        ).strip()
        session_id = ""
        if provider_thread_id:
            matches = [
                candidate
                for candidate, thread in list(self._threads.items())
                if str(getattr(thread, "id", "") or "") == provider_thread_id
            ]
            if len(matches) == 1:
                session_id = matches[0]
        gate = self.approval_gate
        approved = False
        reason = "approval request did not map to exactly one product session"
        response_decision = "decline"
        coordinator = self.interactions
        loop = self._session_loops.get(session_id) if session_id else None
        if session_id and coordinator is not None and loop is not None and loop.is_running():
            call = asyncio.run_coroutine_threadsafe(
                self._await_owner_approval(session_id, method, request), loop
            )
            with self._pending_approval_lock:
                self._pending_approval_calls.setdefault(session_id, set()).add(call)
            try:
                response_decision = str(call.result() or "decline")
                approved = response_decision in {"accept", "acceptForSession"}
                reason = "answered through the durable owner checkpoint"
            except concurrent.futures.CancelledError:
                reason = "the approval wait was cancelled with the turn"
                response_decision = "cancel"
            except Exception as exc:
                reason = f"the durable gate could not answer: {type(exc).__name__}"
                response_decision = "decline"
            finally:
                with self._pending_approval_lock:
                    calls = self._pending_approval_calls.get(session_id)
                    if calls is not None:
                        calls.discard(call)
                        if not calls:
                            self._pending_approval_calls.pop(session_id, None)
        elif session_id and coordinator is not None:
            reason = "no live product event loop can present this approval"
        elif session_id and callable(gate):
            try:
                approved = bool(gate(session_id, method, request))
                response_decision = "accept" if approved else "decline"
                reason = "allowed by the bound gate" if approved else "refused by the bound gate"
            except Exception as exc:
                approved = False
                reason = f"the gate could not answer: {type(exc).__name__}"
        elif session_id:
            reason = "no approval gate is bound; a provider does not approve itself"
        if session_id:
            try:
                self.registry.record_event(
                    session_id, event_type="approval_answered",
                    data={"method": method, "approved": approved, "decision": response_decision, "reason": reason},
                )
            except Exception:
                pass
        return {"decision": response_decision}

    def _answer_business_tool(self, params: Any) -> dict[str, Any]:
        from ampuly.plugins.office_ui.master_sdk_tools import MasterSdkTools
        refused = MasterSdkTools.refused
        if self._business_tools is None or not isinstance(params, dict):
            return refused('business_tool_unavailable')
        matches = [sid for sid, thread in list(self._threads.items())
                   if str(getattr(thread, 'id', '')) == params.get('threadId')]
        if len(matches) != 1:
            return refused('business_tool_session_mismatch')
        sid = matches[0]
        loop = self._session_loops.get(sid)
        if loop is None or not loop.is_running():
            return refused('business_tool_session_not_live')
        try:
            if asyncio.get_running_loop() is loop:
                return refused('business_tool_requires_sdk_reader')
        except RuntimeError:
            pass
        future = asyncio.run_coroutine_threadsafe(self._execute_business_tool(sid, dict(params)), loop)
        with self._pending_approval_lock:
            self._pending_approval_calls.setdefault(sid, set()).add(future)
        try:
            return future.result(timeout=120)
        except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
            future.cancel()
            self._business_tools.blocked = True
            self._model_route.revoke(self._gateway_token)
            return refused('business_tool_interrupted_verify_before_retry')
        except Exception:
            return refused('business_tool_refused')
        finally:
            with self._pending_approval_lock:
                calls = self._pending_approval_calls.get(sid)
                if calls is not None:
                    calls.discard(future)
                    if not calls:
                        self._pending_approval_calls.pop(sid, None)

    async def _execute_business_tool(self, sid: str, request: dict[str, Any]) -> dict[str, Any]:
        tools = self._business_tools
        if tools is None or tools.blocked:
            raise ValueError('business tool unavailable')
        name, arguments, call_id = request.get('tool'), request.get('arguments'), request.get('callId')
        if (set(request) - {'threadId', 'turnId', 'callId', 'tool', 'arguments', 'namespace'}
                or request.get('namespace') not in (None, '') or name not in tools.names
                or not isinstance(arguments, dict) or {'task', 'on_progress'} & arguments.keys()
                or not isinstance(call_id, str) or not 1 <= len(call_id) <= 256):
            raise ValueError('business tool request invalid')
        encoded = json.dumps({'name': name, 'arguments': arguments}, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(encoded) > 65536:
            raise ValueError('business tool request too large')
        ready = self._dispatch_ready.get(sid)
        if ready is not None and not ready.is_set():
            await asyncio.wait_for(ready.wait(), timeout=5)
        tid = self._live_turn_ids.get(sid, '')
        handle = self._live_turns.get(sid)
        if not tid or not getattr(handle, 'id', '') or request.get('turnId') != handle.id:
            raise ValueError('business tool turn mismatch')
        record = await asyncio.to_thread(self.registry.session, sid)
        task = tools.task
        if (record is None or task.id != record.task_id or task.project_id != record.project_id
                or any(str(task.metadata.get(k, '') or '') != getattr(record, k)
                       for k in ('customer_id', 'environment_id', 'run_id', 'order_id'))):
            raise ValueError('business tool scope mismatch')

        async def check_binding():
            await self._model_route.require_binding(self._gateway_token, session_id=sid, turn_id=tid,
                budget_id=str(task.metadata.get('budget_id', '')),
                ampuly_home=str(task.metadata.get('ampuly_home', '')),
                model=json.loads(self._client_config.config_overrides[0][len('model='):]))

        await check_binding()
        await asyncio.to_thread(self.registry.record_event, sid, event_type='dynamic_tool_claimed',
            turn_id=tid, data={'call_id': call_id, 'tool': name,
                              'fingerprint': hashlib.sha256(encoded).hexdigest()})
        try:
            await check_binding()
            response = await tools.call(name, arguments)
            await asyncio.to_thread(self.registry.record_event, sid, event_type='dynamic_tool_returned',
                turn_id=tid, data={'call_id': call_id, 'tool': name, 'success': response['success']})
            return response
        except BaseException:
            tools.blocked = True
            self._model_route.revoke(self._gateway_token)
            # The claim intentionally remains. No assertion that a write was
            # rolled back, and no automatic invocation with a fresh call id.
            raise

    @staticmethod
    def _approval_identity(session_id: str, method: str, request: dict[str, Any]) -> tuple[str, str]:
        """Stable identity for one provider callback, without trusting its scope."""
        parts = [
            session_id,
            method,
            str(request.get("threadId", "") or request.get("thread_id", "")),
            str(request.get("turnId", "") or request.get("turn_id", "")),
            str(request.get("itemId", "") or request.get("item_id", "")),
            str(request.get("approvalId", "") or request.get("approval_id", "")),
        ]
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        return f"codex-approval-{digest[:32]}", f"codex_sdk_approval:{digest}"

    @staticmethod
    def _approval_prompt(method: str, request: dict[str, Any]) -> str:
        reason = str(request.get("reason", "") or "").strip()
        if method == "item/commandExecution/requestApproval":
            command = str(request.get("command", "") or request.get("cmd", "") or "").strip()
            cwd = str(request.get("cwd", "") or "").strip()
            details = ["Codex 请求执行一条命令。"]
            if command:
                details.append(f"命令：{command}")
            if cwd:
                details.append(f"工作目录：{cwd}")
        else:
            grant_root = str(request.get("grantRoot", "") or request.get("grant_root", "") or "").strip()
            details = ["Codex 请求修改文件。"]
            if grant_root:
                details.append(f"请求写入范围：{grant_root}")
        if reason:
            details.append(f"原因：{reason}")
        details.append("批准只作用于本次请求；拒绝后当前回合可以继续寻找其他方案。")
        return "\n".join(details)

    async def _await_owner_approval(
        self, session_id: str, method: str, request: dict[str, Any]
    ) -> str:
        """Publish one SDK request through the product's only human-decision path."""
        coordinator = self.interactions
        record = self.registry.session(session_id)
        if coordinator is None or record is None:
            return "decline"
        if str(getattr(coordinator, "project_id", "") or "") != str(record.project_id or ""):
            return "decline"
        checkpoint_id, domain_key = self._approval_identity(session_id, method, request)
        options = [
            {"id": "approve_once", "label": "仅批准本次"},
            {"id": "deny", "label": "拒绝"},
        ]
        prompt = self._approval_prompt(method, request)
        provider_ids = {
            "thread_id": str(request.get("threadId", "") or request.get("thread_id", "")),
            "turn_id": str(request.get("turnId", "") or request.get("turn_id", "")),
            "item_id": str(request.get("itemId", "") or request.get("item_id", "")),
            "approval_id": str(request.get("approvalId", "") or request.get("approval_id", "")),
        }
        checkpoint = ExecutionCheckpoint(
            checkpoint_id=checkpoint_id,
            project_id=str(record.project_id or "default"),
            session_id=session_id,
            checkpoint_type="tool_permission",
            task_id=str(record.task_id or "") or None,
            payload={
                "schema_version": 2,
                "interaction": {
                    "kind": "tool_permission",
                    "domain_key": domain_key,
                    "prompt": prompt,
                    "options": options,
                },
                "tool_call": {
                    "provider": self.provider_name,
                    "kind": "command" if method.startswith("item/command") else "file_change",
                    **provider_ids,
                },
                "approval": {"provider_method": method},
            },
        )
        existing = await coordinator.store.get_execution_checkpoint(
            checkpoint_id,
            project_id=str(record.project_id or "default"),
            checkpoint_type="tool_permission",
        )
        if existing is not None and str(existing.status or "") not in {"pending"}:
            prior = dict((existing.payload or {}).get("provider_response", {}) or {})
            prior_decision = str(prior.get("decision", "") or "")
            if prior_decision in {"accept", "decline", "cancel"}:
                return prior_decision
            return "decline"
        lease = None
        try:
            lease = await coordinator.open_and_wait(
                checkpoint,
                prompt=prompt,
                options=options,
                consumer_id=f"codex-sdk:{session_id}",
                lease_seconds=300.0,
            )
            option_id = str(lease.decision.get("option_id", "") or "")
            decision = "accept" if option_id == "approve_once" else "decline"
            finished = await coordinator.finish(
                lease,
                final_status="resolved",
                payload_patch={"provider_response": {"decision": decision}},
            )
            if not finished.applied:
                return "decline"
            return decision
        except asyncio.CancelledError:
            if lease is not None:
                await asyncio.shield(
                    coordinator.finish(
                        lease,
                        final_status="cancelled",
                        payload_patch={"provider_response": {"decision": "cancel"}},
                    )
                )
            else:
                await asyncio.shield(
                    coordinator.close_pending_owner_checkpoint(
                        checkpoint_id,
                        checkpoint_type="tool_permission",
                        status="cancelled",
                        payload_patch={
                            "administrative_reason": "the Codex turn was cancelled while awaiting approval"
                        },
                    )
                )
            raise

    def _cancel_pending_approval_calls(self, session_id: str) -> None:
        with self._pending_approval_lock:
            calls = list(self._pending_approval_calls.get(session_id, set()))
        for call in calls:
            call.cancel()

    # ── sessions ──

    async def create_session(
        self,
        scope: HarnessScope,
        profile: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        scope.validate()
        client = await self._ensure_client()
        record = self.registry.open_session(
            provider=self.provider_name,
            project_id=scope.project_id,
            customer_id=scope.customer_id,
            environment_id=scope.environment_id,
            run_id=scope.run_id,
            order_id=str((context or {}).get("order_id", "") or ""),
            task_id=str((context or {}).get("task_id", "") or ""),
        )
        try:
            thread, approval_mode, approval_note = await self._start_sdk_thread(
                client,
                scope,
                model=str((context or {}).get("model", "") or ""),
                security_posture=str((context or {}).get("security_posture", "") or ""),
            )
        except BaseException as exc:
            self.registry.record_event(
                record.session_id,
                event_type="session_create_failed",
                data={"error_type": type(exc).__name__},
            )
            self.registry.set_state(
                record.session_id,
                "lost",
                reason=f"provider thread was not created: {type(exc).__name__}",
            )
            raise
        self._threads[record.session_id] = thread
        budget_id = str((context or {}).get("budget_id", "") or "")
        answers_to = str((context or {}).get("budget_order_id", "") or "").strip()
        binding: dict[str, str] = {"mode": "bounded", "budget_id": budget_id} if budget_id else {"mode": "unbounded"}
        if budget_id and answers_to and answers_to != record.order_id:
            binding["budget_order_id"] = answers_to
        # The Provider's own id is theirs; ours stays ours, and the mapping
        # between them is the only place the two meet.
        self.registry.bind_provider_session(record.session_id, str(getattr(thread, "id", "") or ""))
        self.registry.record_event(
            record.session_id, event_type="session_created",
            data={
                "profile": profile,
                "workspace": scope.workspace_path,
                "approval_mode": approval_mode,
                "approval_note": approval_note,
                "sandbox": "workspace-write",
                "budget_binding": binding,
            },
        )
        return record.session_id

    async def resume_session(self, session_id: str) -> bool:
        record = self.registry.session(session_id)
        if record is None or not record.provider_session_id:
            return False
        self._budget_reference(session_id)
        client = await self._ensure_client()
        try:
            thread = await self._resume_sdk_thread(client, session_id, record.provider_session_id)
        except Exception as exc:
            self.registry.record_event(
                session_id,
                event_type="session_resume_failed",
                data={"error_type": type(exc).__name__},
            )
            try:
                self.registry.set_state(
                    session_id, "suspended", reason=f"resume failed: {type(exc).__name__}"
                )
            except HarnessSessionLost:
                pass
            return False
        self._threads[session_id] = thread
        try:
            self.registry.set_state(session_id, "active", reason="resumed")
        except HarnessSessionLost:
            self._threads.pop(session_id, None)
            return False
        self.registry.record_event(session_id, event_type="session_resumed",
            data={"approval_mode": "operator_review" if self.interactions is not None else CODEX_DENY_ALL,
                  "sandbox": "workspace-write"})
        return True

    async def close_session(self, session_id: str) -> bool:
        if self._model_route is not None:
            self._model_route.revoke_session(session_id)
        self._cancel_pending_approval_calls(session_id)
        self._threads.pop(session_id, None)
        self._live_turns.pop(session_id, None)
        self._live_turn_ids.pop(session_id, None)
        self._session_loops.pop(session_id, None)
        self.registry.set_state(session_id, "closed", reason="closed by caller")
        return True

    async def detach_session_runtime(self, session_id: str, reason: str) -> bool:
        """Drop a dead per-task transport without claiming the thread is closed.

        Company-mode SDK Providers own one app-server per attempt.  If that
        attempt raises, the process must be released, while the durable thread
        mapping remains resumable by a later Provider instance.
        """
        record = self.registry.session(str(session_id or ""))
        if record is None:
            return False
        if self._model_route is not None:
            self._model_route.revoke_session(session_id)
        self._cancel_pending_approval_calls(session_id)
        self._threads.pop(session_id, None)
        self._live_turns.pop(session_id, None)
        self._live_turn_ids.pop(session_id, None)
        self._session_loops.pop(session_id, None)
        if record.state not in {"closed", "lost"}:
            self.registry.set_state(
                session_id,
                "suspended",
                reason=str(reason or "provider runtime detached")[:300],
            )
        self.registry.record_event(
            session_id,
            event_type="provider_runtime_detached",
            data={"reason": str(reason or "provider runtime detached")[:300]},
        )
        return True

    async def shutdown(self) -> bool:
        """Close the shared app-server only after every product session closed.

        Session close and Provider shutdown are intentionally different: one
        customer ending a thread must not kill other customers' live turns.
        """
        if self._threads or self._live_turns:
            return False
        with self._pending_approval_lock:
            if any(self._pending_approval_calls.values()):
                return False
        client = self._client
        close = getattr(client, "close", None)
        if callable(close):
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome
        self._client = None
        self._approvals_gated = False
        return True

    async def steer_turn(
        self, session_id: str, text: str, *, expected_turn_id: str,
    ) -> HarnessOperationResult:
        if not expected_turn_id:
            return HarnessOperationResult(ok=False, reason=HARNESS_INVALID, detail="expected_turn_id required")
        return await self.steer(session_id, text, expected_turn_id=expected_turn_id)

    async def steer(
        self, session_id: str, text: str, *, expected_turn_id: str = "",
    ) -> HarnessOperationResult:
        """Say something to the turn that is running, through the SDK.

        The first transport in the product that is not a stub. It works
        because the Codex SDK hands back a handle to the turn it started
        and that handle takes steering input; ``turn/steer`` is a
        first-class method of the app-server protocol the installed CLI
        generates a schema for, so none of this had to be guessed.

        Every refusal is written down with its reason, and returned with
        it. "No turn is running" and "this build of the SDK cannot steer"
        are different facts -- one is worth retrying and the other never
        will be -- and a bare False told them apart for nobody.
        """
        message = str(text or "").strip()
        if not message:
            return self._steer_refused(
                session_id, HARNESS_INVALID, "nothing to say"
            )
        if self.registry.session(str(session_id or "")) is None:
            # A wrong id, not a quiet turn. No retry helps, and the registry
            # would refuse to record an event against it anyway.
            return self._steer_refused(
                session_id, HARNESS_NOT_FOUND, "no such harness session"
            )
        handle = self._live_turns.get(session_id)
        if expected_turn_id and self._live_turn_ids.get(session_id) != expected_turn_id:
            return self._steer_refused(
                session_id, HARNESS_SESSION_NOT_LIVE, "the requested turn is no longer running here",
            )
        if handle is None:
            return self._steer_refused(
                session_id, HARNESS_SESSION_NOT_LIVE, "no turn is running"
            )
        steer = getattr(handle, "steer", None)
        if not callable(steer):
            return self._steer_refused(
                session_id,
                HARNESS_UNSUPPORTED,
                "this SDK build has no turn_steer",
                alternative="interrupt the turn and start the next one with the new instruction",
            )
        try:
            outcome = steer(message)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            # A transport exception does not prove the provider rejected it.
            # Do not leak exception text (which may contain provider secrets).
            return self._steer_refused(
                session_id, HARNESS_OUTCOME_UNKNOWN,
                f"{type(exc).__name__}: acknowledgement unavailable; do not resend",
            )
        try:
            self.registry.record_event(
                session_id, event_type="steered", turn_id=expected_turn_id,
                data={"chars": len(message)},
            )
        except Exception:
            return HarnessOperationResult(
                ok=False, reason=HARNESS_OUTCOME_UNKNOWN,
                detail="transport accepted the instruction but receipt persistence failed; do not resend",
            )
        return HarnessOperationResult(ok=True, detail="transport accepted; model consumption is not confirmed")

    def _steer_refused(
        self,
        session_id: str,
        reason: str,
        detail: str,
        *,
        alternative: str = "",
    ) -> HarnessOperationResult:
        """Record the refusal and hand back the same thing that was recorded.

        The log and the return value are one object, so they cannot become
        two authorings of one fact. `reason` is a token because a program
        branches on it; `detail` is the sentence it always had, where
        rewording breaks nothing.
        """
        outcome = HarnessOperationResult(
            ok=False, reason=reason, detail=detail, alternative=alternative
        )
        try:
            self.registry.record_event(
                session_id, event_type="steer_refused", data=outcome.to_payload()
            )
        except Exception:  # noqa: BLE001 - losing the note is not losing the refusal
            pass
        return outcome

    async def interrupt(self, session_id: str) -> bool:
        """Interrupt the active turn, without closing its reusable thread.

        Codex exposes ``turn/interrupt`` on ``AsyncTurnHandle``.  Looking for
        it on ``AsyncThread`` made a supported transport appear unsupported
        and blurred "cancel this turn" into "close this session".  The live
        handle is already the authority used by steer, so interruption uses
        that same source-backed boundary.
        """
        # Revoke even if the remote stop fails or its receipt was lost. This
        # does not claim a dispatched API request/effect has been undone.
        if self._model_route is not None:
            turn_id = self._live_turn_ids.get(session_id)
            if turn_id:
                self._model_route.revoke_turn(session_id, turn_id)
            else:
                self._model_route.revoke_session(session_id)
        self._cancel_pending_approval_calls(session_id)
        handle = self._live_turns.get(session_id)
        if handle is None:
            if self.registry.session(str(session_id or "")) is not None:
                self.registry.record_event(session_id, event_type="interrupt_not_live")
            return False
        interrupt = getattr(handle, "interrupt", None)
        if not callable(interrupt):
            # Say so rather than reporting a stop that did not happen.
            self.registry.record_event(session_id, event_type="interrupt_unsupported")
            return False
        try:
            outcome = interrupt()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            self.registry.record_event(
                session_id, event_type="interrupt_failed",
                data={"error_type": type(exc).__name__},
            )
            return False
        self._interrupted.add(session_id)
        self.registry.record_event(session_id, event_type="interrupted")
        self.registry.set_state(session_id, "suspended", reason="interrupted by a person")
        return True

    async def _watch_interrupts(
        self,
        session_id: str,
        turn_id: str,
        since_seq: int,
        running: asyncio.Task[Any],
    ) -> None:
        """Reuse the durable interrupt/lease protocol used by the native Harness.

        The console may live in another process, so an in-memory turn handle is
        not enough.  This watcher is deliberately transport-thin: the registry
        owns the request and lease facts; the official SDK handle performs the
        actual interruption.
        """
        while not running.done():
            try:
                await asyncio.to_thread(
                    self.registry.beat_turn,
                    session_id,
                    turn_id=turn_id,
                    period_sec=self.interrupt_poll_sec,
                )
            except Exception:
                pass
            await asyncio.sleep(self.interrupt_poll_sec)
            if running.done() or session_id in self._interrupted:
                return
            try:
                requested = await asyncio.to_thread(
                    self.registry.interrupt_requested_after,
                    session_id,
                    since_seq,
                    turn_id=turn_id,
                )
            except Exception:
                continue
            if requested:
                await self.interrupt(session_id)
                return

    # ── one turn ──

    async def start_turn(
        self,
        session_id: str,
        instruction: str,
        inputs: dict[str, Any] | None = None,
        callbacks: HarnessCallbacks | None = None,
    ) -> TaskResult:
        record = self.registry.session(session_id)
        if record is None:
            raise ValueError(f"unknown session {session_id!r}")
        if record.state == "lost":
            raise HarnessSessionLost(f"session {session_id!r} was lost; start a new one")
        thread = self._threads.get(session_id)
        if thread is None:
            raise HarnessSessionLost(f"session {session_id!r} has no live thread; resume it first")

        self._session_loops[session_id] = asyncio.get_running_loop()
        turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        guard = await self._reserve_a_turn(session_id, turn_id)
        try:
            started = self.registry.record_event(
                session_id, event_type="turn_started", turn_id=turn_id,
                data={"instruction": instruction}, exclusive_turn=True,
            )
        except Exception:
            if guard is not None:
                await self._finish_spend(guard, "release", reason="turn claim was refused before provider dispatch")
            raise
        try:
            await _invoke_callback(
                getattr(callbacks, "on_event", None),
                HarnessEvent(event_type="turn_started", session_id=session_id, turn_id=turn_id, data={}),
            )
            if self._model_gateway is not None:
                from ampuly.layer3_agent.managed_codex import routing_identity
                token = self._validate_gateway_config(self._client_config, self._model_gateway)
                if (token != self._gateway_token
                        or routing_identity(self._client_config) != self._gateway_config_identity):
                    raise ValueError("runtime gateway configuration changed; recreate the runtime")
                await self._model_route.require_binding(
                    token, session_id=session_id, turn_id=turn_id,
                    budget_id=self._budget_reference(session_id),
                    ampuly_home=str(self.business.path.parent),
                    model=json.loads(self._client_config.config_overrides[0][len("model="):]),
                )
        except BaseException as exc:
            # No turn request was sent. A UI/control callback failure must not
            # leave a running claim or spend the allowance as if work occurred.
            if self._model_gateway is not None:
                self._model_route.revoke(self._gateway_token)
            if self._model_route is not None:
                self._model_route.revoke_turn(session_id, turn_id)
            if guard is not None:
                await self._finish_spend(guard, "release", reason="turn callback stopped before provider dispatch")
            self.registry.record_event(
                session_id, event_type="turn_cancelled" if isinstance(exc, asyncio.CancelledError) else "turn_failed",
                turn_id=turn_id, data={"provider_dispatched": False, "error_type": type(exc).__name__},
                state_after="suspended", state_reason="turn stopped before provider dispatch", settle_turn=True,
            )
            raise

        ready = asyncio.Event()
        self._dispatch_ready[session_id] = ready
        try:
            turn = await thread.turn(instruction)
        except BaseException as exc:
            ready.set()
            self._dispatch_ready.pop(session_id, None)
            # A cancelled transport wait is not a cancelled provider turn. The
            # server may already be executing it, even without a returned id.
            # Retain the durable claim until an explicit reconciliation; a
            # terminal event here would allow resume + automatic duplicate work.
            if self._model_route is not None:
                self._model_route.revoke_turn(session_id, turn_id)
            try:
                if guard is not None:
                    await self._finish_spend(guard, "unknown", reason=f"{type(exc).__name__} while dispatching the turn")
            finally:
                self.registry.record_event(
                    session_id, event_type="turn_dispatch_unknown", turn_id=turn_id,
                    data={"error_type": type(exc).__name__, "provider_outcome_known": False,
                          "automatic_retry": False, "rollback_claimed": False},
                    state_after="suspended", state_reason="turn dispatch receipt unknown; verification required",
                )
            raise
        # Live only while the turn is: a handle kept past the end would accept
        # a steer that goes nowhere and report that it landed.
        self._live_turns[session_id] = turn
        self._live_turn_ids[session_id] = turn_id
        ready.set()
        running: asyncio.Task[TaskResult]
        streamer = getattr(turn, "stream", None)
        if callable(streamer):
            running = asyncio.create_task(
                self._stream_turn(session_id, turn_id, turn, callbacks, guard)
            )
        else:
            async def run_without_stream() -> TaskResult:
                try:
                    result = await turn.run()
                except asyncio.CancelledError:
                    self.registry.record_event(
                        session_id, event_type="turn_cancelled", turn_id=turn_id,
                        data={"rollback_claimed": False}, state_after="suspended",
                        state_reason="turn cancelled; effect outcomes require their own verification",
                        settle_turn=True,
                    )
                    if guard is not None:
                        await self._finish_spend(guard, "settle")
                    raise
                except Exception as exc:
                    event_type = "turn_cancelled" if session_id in self._interrupted else "turn_failed"
                    if guard is not None:
                        if event_type == "turn_cancelled":
                            await self._finish_spend(guard, "settle")
                        else:
                            await self._finish_spend(guard, "unknown", reason=f"{type(exc).__name__} after the turn was dispatched")
                    self.registry.record_event(
                        session_id, event_type=event_type, turn_id=turn_id,
                        data={"error_type": type(exc).__name__},
                        state_after="suspended",
                        state_reason=(
                            "turn interrupted; effect outcomes require their own verification"
                            if event_type == "turn_cancelled"
                            else f"turn failed: {type(exc).__name__}"
                        ), settle_turn=True,
                    )
                    raise

                if guard is not None:
                    await self._finish_spend(guard, "settle")
                text = str(getattr(result, "final_response", "") or getattr(result, "text", "") or "")
                if text:
                    self.registry.record_event(session_id, event_type="assistant_message", turn_id=turn_id, data={"text": text})
                    await _invoke_callback(getattr(callbacks, "on_progress", None), text)
                interrupted = session_id in self._interrupted
                self.registry.record_event(
                    session_id,
                    event_type="turn_cancelled" if interrupted else "turn_finished",
                    turn_id=turn_id,
                    data={"chars": len(text), "rollback_claimed": False},
                    state_after="suspended" if interrupted else "",
                    state_reason=(
                        "turn interrupted; effect outcomes require their own verification"
                        if interrupted else ""
                    ),
                    settle_turn=True,
                )
                return TaskResult(
                    status=TaskStatus.CANCELLED if interrupted else TaskStatus.DONE,
                    content=text,
                    artifacts={
                        "turn_id": turn_id,
                        "session_id": session_id,
                        "provider_session_id": str(getattr(thread, "id", "") or ""),
                    },
                )

            running = asyncio.create_task(run_without_stream())

        watcher = asyncio.create_task(
            self._watch_interrupts(session_id, turn_id, int(started.seq), running)
        )
        try:
            return await running
        finally:
            if self._model_route is not None:
                self._model_route.revoke_turn(session_id, turn_id)
            self._cancel_pending_approval_calls(session_id)
            self._dispatch_ready.pop(session_id, None)
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._live_turns.pop(session_id, None)
            self._live_turn_ids.pop(session_id, None)
            self._interrupted.discard(session_id)

    async def collect_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        """Recover provider items from thread/read when process memory is gone.

        The official thread history remains the authority.  The local list is
        only a hot cache for the live stream and is never required for restart
        recovery.
        """
        cached = list(self._artifacts.get(session_id, []))
        if cached:
            return cached
        record = self.registry.session(str(session_id or ""))
        if record is None or not record.provider_session_id:
            raise ValueError("unknown Codex SDK session")
        client = await self._ensure_client()
        thread = self._threads.get(session_id)
        read = getattr(thread, "read", None)
        try:
            if callable(read):
                history = await read(include_turns=True)
            elif hasattr(client, "_client"):
                # AsyncThread is the official high-level view over the same
                # typed client; constructing it does not resume or mutate the
                # provider thread.
                from openai_codex.api import AsyncThread  # noqa: PLC0415

                history = await AsyncThread(client, record.provider_session_id).read(
                    include_turns=True
                )
            else:
                # An injected protocol double with no read surface proves no
                # recovery capability.  Empty is still the truthful hot cache.
                return []
        except Exception as exc:
            self.registry.record_event(
                session_id,
                event_type="artifact_recovery_failed",
                data={"error_type": type(exc).__name__},
            )
            raise HarnessSessionLost(
                f"session {session_id!r}: official thread/read failed; artifact history is unknown"
            ) from exc
        recovered = self._artifacts_from_thread_read(history)
        self._artifacts[session_id] = recovered
        self.registry.record_event(
            session_id,
            event_type="artifacts_recovered",
            data={"count": len(recovered), "source": "thread/read"},
        )
        return list(recovered)

    @staticmethod
    def _artifacts_from_thread_read(history: Any) -> list[dict[str, Any]]:
        """Project official typed thread history into the Harness artifact shape."""
        thread = getattr(history, "thread", history)
        turns = list(getattr(thread, "turns", []) or [])
        recovered: list[dict[str, Any]] = []
        for turn in turns:
            view = getattr(turn, "items_view", None)
            view_value = str(getattr(view, "value", view) or "full")
            if view_value not in {"full", "TurnItemsView.full"}:
                raise HarnessSessionLost(
                    "thread/read returned a partial item page; artifact history is not complete"
                )
            turn_id = str(getattr(turn, "id", "") or "")
            for wrapped in list(getattr(turn, "items", []) or []):
                item = getattr(wrapped, "root", wrapped)
                kind = str(getattr(item, "type", "") or "item")
                entry: dict[str, Any] = {
                    "turn_id": turn_id,
                    "item_id": str(getattr(item, "id", "") or ""),
                    "type": kind,
                }
                text = str(getattr(item, "text", "") or "")
                if text:
                    entry["text"] = text
                if kind == "fileChange":
                    changes: list[Any] = []
                    for change in list(getattr(item, "changes", []) or []):
                        dump = getattr(change, "model_dump", None)
                        changes.append(
                            dump(mode="json", by_alias=True) if callable(dump) else str(change)
                        )
                    entry["changes"] = changes
                elif kind == "commandExecution":
                    entry.update({
                        "command": _plain(getattr(item, "command", "")),
                        "cwd": _plain(getattr(item, "cwd", "")),
                        "status": str(
                            getattr(getattr(item, "status", None), "value", getattr(item, "status", ""))
                            or ""
                        ),
                        "exit_code": getattr(item, "exit_code", None),
                    })
                recovered.append(entry)
        return recovered
