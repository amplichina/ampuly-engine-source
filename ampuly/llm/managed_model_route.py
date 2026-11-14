"""Run-bound model capability, reusing the business budget and runtime journal.

No listener starts here. HTTP/SDK wiring must mint grants from authenticated
engine facts, not from a request body. Credentials are never placed in events.
The route supports the bridge's text/function subset, not all Responses tools.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ampuly.layer3_agent.harness_sessions import HarnessRequestDuplicate, HarnessSessionBusy
from ampuly.llm.budget import ModelBudgetStop, RouteRefused, model_budget_scope
from ampuly.llm.responses_bridge import BridgedResponse, ProtocolBridgeRefused, ResponsesChatBridge, chat_request, normalize_wire_defaults, client_metadata_receipt

SCOPE_FIELDS = ("project_id", "customer_id", "environment_id", "run_id", "order_id", "task_id")


class ModelRouteRefused(RuntimeError):
    """Public error code only; never attach raw transport exception details."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _Grant:
    session_id: str
    turn_id: str
    scope: dict[str, str]
    task: Any = field(repr=False)
    bridge: ResponsesChatBridge = field(repr=False)
    expires_at: float
    inflight: bool = False


class ManagedModelRoute:
    """Bounded per-process grants; deduplication is durable across instances.

    A token grants one concrete turn, not permission to start/resume another.
    Revocation prevents new dispatch/results; it cannot undo a model request
    already sent. A crash invalidates all tokens, while claims remain in DB.
    """
    def __init__(self, registry: Any, *, max_grants: int = 32, clock=time.monotonic):
        if type(max_grants) is not int or not 1 <= max_grants <= 128:
            raise ValueError("bounded grant capacity required")
        self.registry = registry
        self.max_grants = max_grants
        self.clock = clock
        self._grants: dict[str, _Grant] = {}
        self._prepared: dict[str, tuple[float, bool]] = {}
        self._closed = False

    @staticmethod
    def _key(token: str) -> str:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ModelRouteRefused("model_route_unauthorized")
        return hashlib.sha256(token.encode()).hexdigest()

    def _active(self, key: str, grant: _Grant) -> None:
        if self._closed or self._grants.get(key) is not grant or self.clock() >= grant.expires_at:
            raise ModelRouteRefused("model_route_expired_or_revoked")

    async def _check_scope(self, grant: _Grant) -> None:
        record = await asyncio.to_thread(self.registry.session, grant.session_id)
        if record is None or record.state != "active" or any(getattr(record, k) != v for k, v in grant.scope.items()):
            raise ModelRouteRefused("model_route_scope_changed")
        turn = await asyncio.to_thread(self.registry.pending_turn_id, grant.session_id)
        if turn != grant.turn_id:
            raise ModelRouteRefused("model_route_turn_changed")

    async def require_binding(self, token: str, *, session_id: str, turn_id: str,
                              budget_id: str, ampuly_home: str, model: str) -> None:
        """Pre-dispatch authority check; not a request-body supplied binding."""
        key = self._key(token)
        grant = self._grants.get(key)
        if grant is None:
            raise ModelRouteRefused("model_route_unauthorized")
        self._active(key, grant)
        if (grant.session_id != session_id or grant.turn_id != turn_id
                or not budget_id or grant.task.metadata.get("budget_id") != budget_id
                or Path(grant.task.metadata["ampuly_home"]).resolve() != Path(ampuly_home).resolve()
                or grant.bridge.model != model):
            raise ModelRouteRefused("model_route_binding_mismatch")
        await self._check_scope(grant)
        self._active(key, grant)

    async def bind(self, *, session_id: str, turn_id: str, task: Any,
                   bridge: ResponsesChatBridge, ttl_seconds: float = 900,
                   prepared_token: str | None = None) -> str:
        """Engine-only operation. Requires an explicit existing budget/root."""
        # Claim before any await. Retain the identity while validating scope,
        # so revoke/close during the database wait cannot resurrect authority.
        preparation = None
        if prepared_token is not None:
            key = self._key(prepared_token)
            old = self._prepared.get(key)
            if old is None or old[1] or self.clock() >= old[0]:
                raise ModelRouteRefused("model_route_preparation_unavailable")
            preparation = (old[0], True)
            self._prepared[key] = preparation
        try:
            return await self._bind(session_id=session_id, turn_id=turn_id, task=task,
                                    bridge=bridge, ttl_seconds=ttl_seconds,
                                    prepared_token=prepared_token, preparation=preparation)
        finally:
            if prepared_token is not None and self._prepared.get(key) is preparation:
                self._prepared.pop(key, None)

    async def _bind(self, *, session_id: str, turn_id: str, task: Any,
                    bridge: ResponsesChatBridge, ttl_seconds: float,
                    prepared_token: str | None, preparation: tuple[float, bool] | None) -> str:
        if type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 3600 or not turn_id:
            raise ModelRouteRefused("model_route_invalid_lifetime")
        task = copy.deepcopy(task)
        metadata = dict(getattr(task, "metadata", {}) or {})
        if not metadata.get("budget_id") or not Path(str(metadata.get("ampuly_home", ""))).is_absolute():
            raise ModelRouteRefused("model_route_budget_binding_required")
        scope = {k: str(metadata.get(k, "") or "") for k in SCOPE_FIELDS}
        scope["project_id"] = str(task.project_id)
        scope["task_id"] = str(task.id)
        if not scope["task_id"] or not scope["project_id"]:
            raise ModelRouteRefused("model_route_scope_required")
        grant = _Grant(session_id, turn_id, scope, task, bridge, self.clock() + ttl_seconds)
        await self._check_scope(grant)
        if self._closed:
            raise ModelRouteRefused("model_route_closed")
        self._expire_idle()
        if preparation is not None:
            if (self.clock() >= preparation[0]
                    or self._prepared.get(self._key(prepared_token)) is not preparation):
                raise ModelRouteRefused("model_route_preparation_unavailable")
        # No await between capacity check and insertion. Never evict active work.
        reserved_slot = 1 if preparation is not None else 0
        if len(self._grants) + len(self._prepared) - reserved_slot >= self.max_grants:
            raise ModelRouteRefused("model_route_capacity")
        token = prepared_token if prepared_token is not None else secrets.token_urlsafe(32)
        self._grants[self._key(token)] = grant
        return token

    def _expire_idle(self) -> None:
        now = self.clock()
        for key, old in list(self._grants.items()):
            if not old.inflight and now >= old.expires_at:
                del self._grants[key]
        for key, (deadline, _) in list(self._prepared.items()):
            if now >= deadline:
                del self._prepared[key]

    def prepare(self, *, ttl_seconds: float = 60) -> str:
        """Reserve a runtime credential with NO model authority until bind()."""
        if self._closed:
            raise ModelRouteRefused("model_route_closed")
        if type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 300:
            raise ModelRouteRefused("model_route_invalid_lifetime")
        self._expire_idle()
        if len(self._grants) + len(self._prepared) >= self.max_grants:
            raise ModelRouteRefused("model_route_capacity")
        token = secrets.token_urlsafe(32)
        self._prepared[self._key(token)] = (self.clock() + ttl_seconds, False)
        return token

    def revoke(self, token: str) -> None:
        key = self._key(token)
        self._grants.pop(key, None)
        self._prepared.pop(key, None)

    def revoke_turn(self, session_id: str, turn_id: str) -> None:
        """Exact lifecycle revocation; never treat an empty turn as a wildcard."""
        if not session_id or not turn_id:
            raise ValueError("session and turn are required for revocation")
        for key, grant in list(self._grants.items()):
            if grant.session_id == session_id and grant.turn_id == turn_id:
                del self._grants[key]

    def revoke_session(self, session_id: str) -> None:
        """Stop one session without shutting down other customers' grants."""
        if not session_id:
            raise ValueError("session is required for revocation")
        for key, grant in list(self._grants.items()):
            if grant.session_id == session_id:
                del self._grants[key]

    def close(self) -> None:
        self._closed = True
        self._grants.clear()
        self._prepared.clear()

    def authorize(self, token: str) -> None:
        """Cheap pre-body check; complete() still rechecks scope and dispatch."""
        key = self._key(token)
        grant = self._grants.get(key)
        if grant is None:
            raise ModelRouteRefused("model_route_unauthorized")
        self._active(key, grant)

    async def _event(self, grant: _Grant, event_type: str, **data) -> None:
        await asyncio.to_thread(self.registry.record_event, grant.session_id,
                                event_type=event_type, turn_id=grant.turn_id, data=data)

    async def complete(self, token: str, request: dict[str, Any]) -> BridgedResponse:
        key = self._key(token)
        grant = self._grants.get(key)
        if grant is None:
            raise ModelRouteRefused("model_route_unauthorized")
        self._active(key, grant)
        if grant.inflight:
            raise ModelRouteRefused("model_route_busy")
        try:
            request = normalize_wire_defaults(request)
            chat_request(request, model=grant.bridge.model)
        except (ProtocolBridgeRefused, TypeError, ValueError):
            raise ModelRouteRefused("model_route_unsupported_request") from None
        # The route the grant was bound to must still be the provider's route.
        # Checked before the claim: a model switched mid-turn is a refusal the
        # caller can act on, not a claim that then waits for verification.
        selector = getattr(getattr(grant.bridge, "provider", None), "_select_model", None)
        if callable(selector) and selector(None) != grant.bridge.model:
            raise ModelRouteRefused("model_route_changed")
        # Presentation-only defaults cannot turn a retransmit into a fresh call.
        canonical = {**request, "model": grant.bridge.model, "stream": False, "store": False}
        # Cache routing is a performance hint, not a new model operation.
        canonical.pop("prompt_cache_key", None)
        canonical.pop("client_metadata", None)
        fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, allow_nan=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        self._active(key, grant)
        grant.inflight = True
        claimed = False
        try:
            await self._check_scope(grant)
            self._active(key, grant)
            try:
                await self._event(grant, "model_request_claimed", fingerprint=fingerprint,
                                  **client_metadata_receipt(request))
            except HarnessRequestDuplicate:
                raise ModelRouteRefused("model_request_already_claimed") from None
            except HarnessSessionBusy:
                raise ModelRouteRefused("model_route_turn_changed") from None
            claimed = True
            self._active(key, grant)
            # HTTP request tasks do not inherit a worker's ContextVar. Restore
            # only the server-owned task snapshot; body fields cannot set scope.
            async def before_dispatch():
                await self._check_scope(grant)
                self._active(key, grant)
            from ampuly.llm.provider import model_credential_scope
            from ampuly.llm.task_model_scope import plan_scope_for
            with model_budget_scope(grant.task), plan_scope_for(grant.task), model_credential_scope(
                grant.scope['customer_id'], grant.scope['environment_id'],
            ):
                completion_options = {"before_dispatch": before_dispatch}
                if request.get("prompt_cache_key") is not None:
                    completion_options["cache_namespace"] = hashlib.sha256(json.dumps(
                        {"version": 1, "scope": grant.scope,
                         "home": str(Path(grant.task.metadata['ampuly_home']).resolve())},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode()).hexdigest()
                result = await grant.bridge.complete(request, **completion_options)
            await self._check_scope(grant)
            self._active(key, grant)
            await self._event(grant, "model_request_result", fingerprint=fingerprint,
                              status=result.response["status"], accounting=result.accounting)
            self._active(key, grant)
            return result
        except BaseException as exc:
            if claimed:
                try:
                    # The binary only ever sees a bare 503; the operator gets the
                    # failure's class (never its text, which may quote the model)
                    # so "the model answered in a shape the bridge refused" can be
                    # told apart from "the provider went away after the claim".
                    await self._event(grant, "model_request_unreturned", fingerprint=fingerprint,
                                      reason="cancelled" if isinstance(exc, asyncio.CancelledError) else "verification_required",
                                      error_type=type(exc).__name__)
                except Exception:
                    pass  # The durable claim still blocks automatic replay.
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, ModelRouteRefused):
                raise
            if isinstance(exc, RouteRefused):
                # Refused before the request left: nothing to verify, nothing was sent. The route's own refusal (a
                # revoked scope, a changed turn) keeps its code; anything else (a key) is named as such.
                if isinstance(exc.__cause__, ModelRouteRefused):
                    raise exc.__cause__ from None
                raise ModelRouteRefused("model_route_refused_before_dispatch") from None
            if isinstance(exc, ModelBudgetStop):
                raise ModelRouteRefused("model_budget_stopped_verify_before_retry") from None
            raise ModelRouteRefused("model_result_unavailable_verify_before_retry") from None
        finally:
            grant.inflight = False
