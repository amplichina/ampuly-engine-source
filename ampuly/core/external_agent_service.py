"""Host-authorized external goals through existing Native Harness and Effects.

No transport creates authority: the host reloads the grant, supplies an already
ready Engine/BusinessStore and a registered channel provider. Delivery requires
the existing Effect approval. A receipt is not recipient readback.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import asyncio
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ampuly.channels.external_agent_ingress import ExternalAgentEnvelope, ExternalAgentIngress, resolve_secret
from ampuly.core.direct_task_source import direct_task_source
from ampuly.core.models import Task, TaskResult, TaskStatus
from ampuly.core.external_agent_readback import (
    ExternalAgentReadbackError, ReadbackAdapter, ReadbackRequest,
    read_recipient_batch, require_readback_adapter,
)
from ampuly.database.external_agent_tasks import (
    PROTOCOL, SOURCE, STATE, ExternalAgentTaskRefused, ExternalAgentTaskStore, task_id_for,
)
from ampuly.effects.channel_message import ChannelMessageEffectProvider, ENTERPRISE_MESSAGE_SEND
from ampuly.effects.models import ApprovalType, EffectState, ProviderOutcome, ProviderResult, VerificationStatus
from ampuly.project_id import is_valid_project_id


class ExternalAgentRefused(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExternalAgentBinding:
    principal_id: str
    project_id: str
    workspace_root: str
    budget_id: str
    provider_name: str
    recipient_id: str
    delivery_session_id: str
    grant_id: str
    authorized_by: str
    customer_id: str = ""
    environment_id: str = ""

    def __post_init__(self):
        for name, value in asdict(self).items():
            if (type(value) is not str or value != value.strip()
                    or any(ord(c) < 32 for c in value)
                    or (not value and name not in {"customer_id", "environment_id"})):
                raise ExternalAgentRefused("invalid_host_binding")
        if not is_valid_project_id(self.project_id) or not Path(self.workspace_root).is_absolute():
            raise ExternalAgentRefused("invalid_host_binding")


class ExternalAgentDeliveryProvider(ChannelMessageEffectProvider):
    """Dedicated provider identity: no host installation means no execution."""

    def __init__(self, base, *, name, guard, cancelled):
        super().__init__(base.channel)
        self.name = name
        self.base = base
        self.guard = guard
        self.cancelled = cancelled

    def _credential_refusal(self, effect):
        # Account checks remain owned by the registered host channel adapter.
        return self.base._credential_refusal(effect) or self.guard(effect)

    async def apply(self, effect, *, idempotency_key):
        refusal = self._credential_refusal(effect)
        if refusal:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=refusal)
        try:
            result = await self.base.apply(effect, idempotency_key=idempotency_key)
        except asyncio.CancelledError as cancellation:
            try:
                self.cancelled(effect)
            except Exception:
                cancellation.add_note("external_agent_cancel_cleanup_unavailable")
            raise
        if result.outcome == ProviderOutcome.APPLIED:
            result = replace(result, receipt={**dict(result.receipt or {}), "provider": self.name})
        return result


class ExternalAgentService:
    def __init__(self, *, engine, business_store, binding_loader: Callable[[], ExternalAgentBinding],
                 secret: str | bytes, readback: ReadbackAdapter | None = None):
        try:
            if readback is not None:
                require_readback_adapter(readback)
        except ExternalAgentReadbackError as exc:
            raise ExternalAgentRefused(exc.code) from None
        from ampuly.engine import AmpulyEngine
        if not isinstance(engine, AmpulyEngine) or engine.store is None or getattr(engine, "harness_registry", None) is None:
            raise ExternalAgentRefused("ready_native_engine_required")
        if engine.effect_gateway is None or engine.effect_approval_service is None:
            raise ExternalAgentRefused("effect_approval_unavailable")
        self.engine = engine
        self.business = business_store
        if Path(self.business.path).resolve(strict=True) != (Path(engine.ampuly_home) / "business.db").resolve(strict=True):
            raise ExternalAgentRefused("host_budget_store_mismatch")
        self.binding_loader = binding_loader
        self.binding = binding_loader()
        if not isinstance(self.binding, ExternalAgentBinding):
            raise ExternalAgentRefused("invalid_host_binding")
        self.binding_sha256 = _digest(asdict(self.binding))
        self.secret = resolve_secret(secret)
        self.readback = readback
        self.gateway = engine.effect_gateway
        self._host()
        self.tasks = ExternalAgentTaskStore(
            engine.store, principal_id=self.binding.principal_id, project_id=self.binding.project_id,
            binding_sha256=self.binding_sha256,
        )
        base = next((p for p in self.gateway.providers()
                              if p.name == self.binding.provider_name), None)
        if not isinstance(base, ChannelMessageEffectProvider):
            raise ExternalAgentRefused("registered_channel_provider_required")
        self.delivery_provider_name = "external-agent:" + self.binding_sha256
        installed = next((p for p in self.gateway.providers() if p.name == self.delivery_provider_name), None)
        if installed is not None:
            if not isinstance(installed, ExternalAgentDeliveryProvider) or installed.base is not base:
                raise ExternalAgentRefused("external_agent_provider_conflict")
            installed.guard = self._send_guard
            installed.cancelled = self._cancel_delivery_claim
            self.provider = installed
        else:
            self.provider = ExternalAgentDeliveryProvider(
                base, name=self.delivery_provider_name, guard=self._send_guard, cancelled=self._cancel_delivery_claim,
            )
            self.gateway.register(self.provider)

    def _host(self):
        current = self.binding_loader()
        if not isinstance(current, ExternalAgentBinding) or current != self.binding:
            raise ExternalAgentRefused("host_binding_changed")
        if (self.engine.project_id != current.project_id
                or self.engine.store.project_id != current.project_id):
            raise ExternalAgentRefused("host_project_mismatch")
        root = Path(current.workspace_root)
        if not root.is_dir() or str(root.resolve(strict=True)) != str(root):
            raise ExternalAgentRefused("host_workspace_unavailable")
        return current

    def _budget(self, *, require_available=False):
        binding = self._host()
        budget = self.business.run_budget(binding.budget_id)
        if (not budget or budget.get("project_id") != binding.project_id
                or budget.get("customer_id", "") != binding.customer_id
                or budget.get("environment_id", "") != binding.environment_id
                or budget.get("order_id") or budget.get("state") != "open"):
            raise ExternalAgentRefused("host_budget_unavailable")
        limits = budget.get("limits") or {}
        # Both call count and money are bounded. Actual per-call reservation,
        # tariff checks and settlement remain in the native model/tool guard.
        for kind in ("model_call", "cost_cny"):
            value = limits.get(kind)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ExternalAgentRefused("finite_host_budget_required")
            if require_available and (budget.get("available") or {}).get(kind, 0) <= 0:
                raise ExternalAgentRefused("host_budget_exhausted")
        return budget

    def _send_guard(self, effect):
        """Synchronous final provider gate; also covers the owner consumer."""
        try:
            binding = self._host()
            self._budget()
            if bool(getattr(self.engine, "_shutting_down", False)):
                return "external_agent_stopped"
            marker = dict(effect.payload.get("metadata") or {}).get("external_agent") or {}
            if (effect.project_id != binding.project_id
                    or effect.provider != self.delivery_provider_name
                    or effect.kind != ENTERPRISE_MESSAGE_SEND
                    or effect.run_id != effect.payload.get("task_ref")
                    or effect.customer_id != binding.customer_id
                    or effect.environment_id != binding.environment_id
                    or effect.payload.get("channel") != self.provider.channel.name
                    or marker.get("protocol") != PROTOCOL
                    or marker.get("project_id") != binding.project_id
                    or marker.get("principal_id") != binding.principal_id
                    or marker.get("binding_sha256") != self.binding_sha256
                    or marker.get("effect_id") != effect.effect_id
                    or effect.payload.get("user_id") != binding.recipient_id
                    or effect.payload.get("session_id") != binding.delivery_session_id):
                return "external_agent_delivery_scope"
            # Read only, no schema setup. The final send sees a stop committed
            # after approval, instead of trusting a stale in-memory Task.
            task = self.tasks.read_task(effect.payload.get("task_ref"))
            if task.status != TaskStatus.DONE or task.metadata.get("last_stop_reason") == "user_stop":
                return "external_agent_stopped"
            metadata = task.metadata
            source = metadata.get(SOURCE) or {}
            receipt = metadata.get(STATE) or {}
            content = effect.payload.get("content")
            if (source.get("binding_sha256") != self.binding_sha256
                    or source.get("principal_id") != binding.principal_id
                    or receipt.get("effect_id") != effect.effect_id
                    or (receipt.get("artifact") or {}).get("sha256") != marker.get("artifact_sha256")
                    or type(content) is not str
                    or hashlib.sha256(content.encode("utf-8")).hexdigest() != marker.get("artifact_sha256")):
                return "external_agent_delivery_source"
            return ""
        except Exception:
            return "external_agent_host_unavailable"

    async def submit(self, envelope: ExternalAgentEnvelope):
        binding = self._host()
        admission = ExternalAgentIngress(secret=self.secret, allowed_projects={binding.project_id}).verify(envelope)
        if (type(envelope.message_id) is not str or envelope.message_id != admission.message_id
                or len(admission.message_id.encode("utf-8")) > 256):
            raise ExternalAgentRefused("invalid_message_id")
        tid = task_id_for(binding.principal_id, binding.project_id, admission.message_id)
        scope = direct_task_source(
            kind="direct_task", project_id=binding.project_id, workspace_root=binding.workspace_root,
            budget_id=binding.budget_id, customer_id=binding.customer_id,
            environment_id=binding.environment_id,
        ).to_metadata()
        scope.update(ampuly_home=str(self.engine.ampuly_home), run_id=tid)
        source = {
            "protocol": PROTOCOL, "principal_id": binding.principal_id, "project_id": binding.project_id,
            "message_id": admission.message_id, "sequence": admission.sequence,
            "request_sha256": hashlib.sha256(envelope.canonical_payload()).hexdigest(),
            "goal_sha256": hashlib.sha256(admission.goal.encode("utf-8")).hexdigest(),
            "binding_sha256": self.binding_sha256, "scope": scope,
        }
        # An exact retry stays queryable even when its budget is now exhausted.
        try:
            old = await self.tasks.get(admission.message_id)
        except ExternalAgentTaskRefused as exc:
            if exc.code != "external_agent_task_unavailable":
                raise
        else:
            if old.metadata[SOURCE] != source:
                raise ExternalAgentTaskRefused("external_agent_request_conflict")
            return await self.status(admission.message_id)
        self._budget(require_available=True)
        task = Task(id=tid, session_id="session-" + tid, title="External Agent goal",
                    description=admission.goal, project_id=binding.project_id, max_retries=0,
                    metadata={**scope, SOURCE: source, STATE: {}, "content_trust": "untrusted_external"})
        await self.tasks.create_or_get(task)
        return await self.status(admission.message_id)

    async def status(self, message_id: str):
        self._host()
        task = await self.tasks.get(message_id)
        receipt = task.metadata.get(STATE) or {}
        status = "accepted"
        if receipt.get("claim_id"):
            status = "running_or_unsettled"
        if receipt.get("finished"):
            status = "body_recorded" if receipt.get("artifact") else "execution_stopped"
        effect_id = receipt.get("effect_id", "")
        if effect_id:
            effect = self.gateway.journal.get(effect_id)
            self._effect_identity(task, effect)
            status = ("awaiting_delivery_approval" if effect.state == EffectState.AWAITING_APPROVAL
                      else "delivery_" + effect.state.value)
        if receipt.get("readback"):
            status = "delivery_verified"
        if (task.status == TaskStatus.CANCELLED or task.metadata.get("last_stop_reason") == "user_stop") and not receipt.get("readback"):
            status = "execution_stopped"
        return {
            "task_id": task.id, "project_id": task.project_id, "message_id": message_id,
            "status": status, "task_status": task.status.value,
            "harness_session_id": str(receipt.get("harness_session_id") or task.metadata.get("harness_session_id") or ""),
            "harness_turn_id": str(receipt.get("harness_turn_id") or ""),
            "artifact": dict(receipt.get("artifact") or {}), "handoff_id": receipt.get("handoff_id", ""),
            "effect_id": effect_id, "readback_verified": bool(receipt.get("readback")),
            "business_acceptance": "pending",
        }

    async def execute(self, message_id: str):
        self._host()
        existing = await self.tasks.get(message_id)
        if (existing.metadata.get(STATE) or {}).get("claim_id"):
            receipt = existing.metadata.get(STATE) or {}
            if receipt.get("artifact") and existing.status == TaskStatus.DONE:
                await self._ensure_publication(message_id, existing)
            return await self.status(message_id)
        self._budget(require_available=True)
        if bool(getattr(self.engine, "_shutting_down", False)):
            raise ExternalAgentRefused("external_agent_stopped")
        task, claim = await self.tasks.claim_once(message_id)
        if not claim:
            return await self.status(message_id)
        registry = self.engine._active_task_run_registry
        attempt = registry.register(task.project_id, task.id, owner_task=asyncio.current_task())
        try:
            # The real Engine constructs NativeAgent and NativeHarnessProvider.
            # No injected executor and no provider-without-registry fallback.
            from ampuly.computer_use.desktop_runtime_binding import desktop_execution_scope
            async with desktop_execution_scope(self.engine, task, attempt):
                result = await self.engine._run_native_agent(task)
            session_id, turn_id = self._native_turn(task, result)
            refs = list(task.metadata.get("direct_output_receipts") or [])
            artifact = refs[0] if len(refs) == 1 else None
            handoff = str(task.metadata.get("governed_handoff_id") or "")
            if result.status == TaskStatus.DONE and (not artifact or not handoff):
                result = TaskResult(status=TaskStatus.BLOCKED, content="",
                                    artifacts={"direct_delivery_state": "outcome_unknown"})
            finished = await self.tasks.finish(message_id, claim, result, artifact=artifact, handoff_id=handoff,
                                               harness_session_id=session_id, harness_turn_id=turn_id)
        except BaseException:
            # A crash/cancel is never a license to launch a substitute turn.
            # The permanent claim makes the uncertain attempt visible.
            raise
        finally:
            registry.unregister(task.project_id, task.id, attempt)
        if finished.status == TaskStatus.DONE:
            await self._prepare_delivery(message_id)
        return await self.status(message_id)

    def _native_turn(self, task, result):
        session_id = str(task.metadata.get("harness_session_id") or "")
        registry = self.engine.harness_registry
        session = registry.session(session_id)
        starts = registry.events_after(session_id, limit=2, event_type="turn_started")
        finishes = registry.events_after(session_id, limit=2, event_type="turn_finished")
        if (session is None or session.task_id != task.id or session.project_id != task.project_id
                or session.run_id != task.id or session.provider != "native"
                or len(starts) != 1 or len(finishes) != 1
                or starts[0].data.get("task_id") != task.id
                or not finishes[0].turn_id or starts[0].turn_id != finishes[0].turn_id
                or finishes[0].data.get("status") != result.status.value):
            raise ExternalAgentRefused("native_turn_evidence_unavailable")
        return session_id, finishes[0].turn_id

    async def _body(self, task):
        receipt = task.metadata.get(STATE) or {}
        ref = receipt.get("artifact") or {}
        artifact = await self.engine.store.get_artifact(task.project_id, ref.get("artifact_id", ""))
        handoff = await self.engine.store.get_handoff_record(receipt.get("handoff_id", ""))
        if (artifact is None or artifact.task_id != task.id or handoff is None
                or handoff.task_id != task.id or handoff.project_id != task.project_id
                or ref not in list(handoff.payload.get("artifacts") or [])):
            raise ExternalAgentRefused("governed_delivery_unavailable")
        content = artifact.details.get("content")
        if (type(content) is not str or hashlib.sha256(content.encode("utf-8")).hexdigest() != ref.get("sha256")
                or artifact.details.get("content_sha256") != ref.get("sha256")):
            raise ExternalAgentRefused("governed_delivery_changed")
        return content, ref

    async def _prepare_delivery(self, message_id):
        binding = self._host()
        task = await self.tasks.get(message_id)
        content, ref = await self._body(task)
        effect_id = task.id + "-delivery"
        payload = {
            "channel": self.provider.channel.name, "user_id": binding.recipient_id,
            "session_id": binding.delivery_session_id, "content": content, "task_ref": task.id,
            "metadata": {"external_agent": {
                "protocol": PROTOCOL, "effect_id": effect_id, "principal_id": binding.principal_id,
                "project_id": binding.project_id, "artifact_sha256": ref["sha256"],
                "binding_sha256": self.binding_sha256,
            }},
        }
        args = dict(provider=self.delivery_provider_name, kind=ENTERPRISE_MESSAGE_SEND, payload=payload,
                    idempotency_key=effect_id, project_id=binding.project_id, run_id=task.id,
                    customer_id=binding.customer_id, environment_id=binding.environment_id,
                    correlation_id=task.id, causation_id=message_id, approval_type=ApprovalType.BUSINESS)
        # Journal already supports a caller-chosen Effect ID. Reserve it before
        # publication so the recipient can bind its bytes to that exact Effect.
        effect = self.gateway.journal.propose(
            **args, effect_id=effect_id,
            provider_supports_idempotency=self.provider.supports_idempotency, claim_before_apply=True,
        )
        if effect.effect_id != effect_id:
            raise ExternalAgentRefused("external_agent_effect_conflict")
        await self.tasks.bind_effect(message_id, effect_id)
        await self.engine.effect_approval_service.propose_for_approval(
            **args, prompt="Approve delivery of this external Agent task result?",
            task_id=task.id, session_id=task.session_id,
            ownership={"authorized_by": binding.authorized_by},
        )

    async def _ensure_publication(self, message_id, task):
        effect_id = (task.metadata.get(STATE) or {}).get("effect_id")
        if not effect_id:
            await self._prepare_delivery(message_id)
            return
        effect = self.gateway.journal.get(effect_id)
        self._effect_identity(task, effect)
        if effect.state in {EffectState.PROPOSED, EffectState.AWAITING_APPROVAL}:
            # Same Effect key and checkpoint identity repair either side of
            # the publication crash window without replaying the Native turn.
            await self._prepare_delivery(message_id)

    def _effect_identity(self, task, effect):
        marker = dict(effect.payload.get("metadata") or {}).get("external_agent") or {}
        if (effect.project_id != task.project_id or effect.run_id != task.id
                or effect.provider != self.delivery_provider_name or effect.payload.get("task_ref") != task.id
                or marker.get("binding_sha256") != self.binding_sha256
                or marker.get("principal_id") != self.binding.principal_id
                or marker.get("effect_id") != effect.effect_id):
            raise ExternalAgentRefused("external_agent_effect_scope")

    def _cancel_delivery_claim(self, owned):
        """Only this exact attempt can turn a cancelled send into unknown."""
        if (not owned.claim_token or not owned.claimed_by
                or owned.provider != self.delivery_provider_name
                or owned.project_id != self.binding.project_id):
            return
        current = self.gateway.journal.get(owned.effect_id)
        if current.claim_token != owned.claim_token or current.claimed_by != owned.claimed_by:
            return
        if current.state == EffectState.CLAIMED:
            current = self.gateway.journal.mark_outcome_unknown(
                owned.effect_id, claim_token=owned.claim_token,
                error="external_agent_delivery_cancelled_outcome_unknown",
            )
        if current.state == EffectState.OUTCOME_UNKNOWN:
            self.business.mark_budget_pending_verification(
                self.binding.budget_id, reason="external_agent_delivery_outcome_unknown",
                effect_id=owned.effect_id,
            )

    async def _apply_delivery_once(self, effect_id):
        # Drain this one journal claim through cancellation, using its existing
        # SQLite lock timeout (not an I/O deadline). Never retry the claim or
        # leave an unobserved to_thread claim running behind the request.
        attempt = "external-agent:" + uuid.uuid4().hex
        worker = asyncio.create_task(asyncio.to_thread(
            self.gateway.claim_authorized, effect_id, claimed_by=attempt,
        ))
        cancelled = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        try:
            claim = worker.result()
        except Exception:
            if cancelled is not None:
                raise cancelled
            raise
        if cancelled is not None:
            if claim.acquired:
                try:
                    self._cancel_delivery_claim(claim.record)
                except Exception:
                    cancelled.add_note("external_agent_cancel_cleanup_unavailable")
            raise cancelled
        if not claim.acquired:
            return
        try:
            await self.gateway.apply_claimed(effect_id, claim_token=claim.record.claim_token)
        except asyncio.CancelledError as cancellation:
            # The provider covers cancellation during send (including owner
            # consumers). This also covers cancellation around receipt commit.
            try:
                self._cancel_delivery_claim(claim.record)
            except Exception:
                cancellation.add_note("external_agent_cancel_cleanup_unavailable")
            raise

    async def deliver(self, message_id: str):
        self._host()
        task = await self.tasks.get(message_id)
        receipt = task.metadata.get(STATE) or {}
        if task.status != TaskStatus.CANCELLED:
            await self._ensure_publication(message_id, task)
            task = await self.tasks.get(message_id)
            receipt = task.metadata[STATE]
        if not receipt.get("effect_id"):
            return await self.status(message_id)
        effect = self.gateway.journal.get(receipt["effect_id"])
        self._effect_identity(task, effect)
        if effect.state == EffectState.AWAITING_APPROVAL and effect.authorization_id:
            refusal = self._send_guard(effect)
            if refusal:
                raise ExternalAgentRefused(refusal)
            await self._apply_delivery_once(effect.effect_id)
            effect = self.gateway.journal.get(effect.effect_id)
        if effect.state == EffectState.OUTCOME_UNKNOWN:
            self.business.mark_budget_pending_verification(
                self.binding.budget_id, reason="external_agent_delivery_outcome_unknown",
                effect_id=effect.effect_id,
            )
        # APPLIED and OUTCOME_UNKNOWN are read-only here. Never use recovery to
        # resend an uncertain channel message. Missing adapter remains unverified.
        if effect.state in {EffectState.APPLIED, EffectState.OUTCOME_UNKNOWN} and self.readback is not None:
            content, ref = await self._body(task)
            try:
                adapter = require_readback_adapter(self.readback)
                request = ReadbackRequest(
                    effect_id=effect.effect_id, principal_id=self.binding.principal_id,
                    project_id=task.project_id, channel=self.provider.channel.name,
                    user_id=self.binding.recipient_id, session_id=self.binding.delivery_session_id,
                    task_ref=task.id, artifact_sha256=ref["sha256"], binding_sha256=self.binding_sha256,
                    protocol=PROTOCOL, policy=adapter.policy,
                    deadline_monotonic=time.monotonic() + adapter.policy.deadline_s,
                )
                batch = await read_recipient_batch(adapter, request)
            except ExternalAgentReadbackError as exc:
                raise ExternalAgentRefused(exc.code) from None
            # The reader receives detached scope, never authority. An expired or
            # replaced host grant cannot be revived by a late recipient result.
            self._host()
            effect = self.gateway.journal.get(effect.effect_id)
            self._effect_identity(task, effect)
            if effect.state not in {EffectState.APPLIED, EffectState.OUTCOME_UNKNOWN}:
                raise ExternalAgentRefused("external_agent_effect_scope")
            matches = []
            for row in batch.rows:
                marker = dict(row.get("metadata") or {}).get("external_agent") or {}
                if (row.get("channel") == self.provider.channel.name
                        and row.get("user_id") == self.binding.recipient_id
                        and row.get("session_id") == self.binding.delivery_session_id
                        and row.get("task_ref") == task.id and row.get("content") == content
                        and marker == effect.payload["metadata"]["external_agent"]
                        and hashlib.sha256(row["content"].encode("utf-8")).hexdigest() == ref["sha256"]):
                    matches.append(row)
            if len(matches) == 1:
                proof = {"effect_id": effect.effect_id, "principal_id": self.binding.principal_id,
                         "project_id": task.project_id, "session_id": self.binding.delivery_session_id,
                         "artifact_sha256": ref["sha256"], "recipient_readback": True}
                if effect.state == EffectState.OUTCOME_UNKNOWN:
                    self.gateway.journal.record_look_back(effect.effect_id, status=VerificationStatus.COMPLETE, details=proof)
                else:
                    self.gateway.journal.record_verification(effect.effect_id, status=VerificationStatus.COMPLETE, details=proof)
                await self.tasks.record_readback(message_id, effect.effect_id, proof)
                self.business.release_budget_hold_for_effect(
                    effect.effect_id, reason="external_agent_recipient_readback_verified",
                )
        return await self.status(message_id)

