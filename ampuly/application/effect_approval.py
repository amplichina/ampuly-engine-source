"""Application service binding owner approval to the Effect Gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ampuly.core.models import ExecutionCheckpoint
from ampuly.effects import (
    ApprovalType,
    EffectExecutionResult,
    EffectGateway,
    EffectRecord,
    EffectState,
    SimulationStatus,
)
from ampuly.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)


EFFECT_APPROVAL_CHECKPOINT_TYPE = "action_permission"
EFFECT_APPROVAL_PROTOCOL = "superones.effect-approval/v1"
APPROVAL_TTL_SECONDS = {"browser.submit": 15 * 60.0}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class EffectApprovalPublication:
    effect: EffectRecord
    checkpoint: ExecutionCheckpoint
    checkpoint_created: bool


@dataclass(frozen=True)
class EffectApprovalConsumption:
    status: str
    effect: EffectRecord | None
    checkpoint: ExecutionCheckpoint | None
    provider_called: bool = False
    replay_noop: bool = False
    detail: str = ""


class EffectApprovalService:
    """Turns one authenticated owner decision into one durable effect attempt."""

    def __init__(
        self,
        *,
        gateway: EffectGateway,
        coordinator: InteractionCoordinator,
        on_outcome_unknown: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.gateway = gateway
        self.coordinator = coordinator
        # Called with (checkpoint, execution) when a provider call returned
        # without proof either way. This service has no budget store and
        # should not; whoever owns budgets binds this and places the hold.
        # A12: a lost receipt keeps the budget and stops further spending
        # until a person verifies -- which only works if somebody is told.
        self.on_outcome_unknown = on_outcome_unknown
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _deadline(self, checkpoint: ExecutionCheckpoint, effect: EffectRecord) -> tuple[datetime | None, str]:
        approval = dict((checkpoint.payload or {}).get("approval", {}) or {})
        expires_at = str(approval.get("expires_at", "") or "").strip()
        if not expires_at and effect.kind not in APPROVAL_TTL_SECONDS:
            return None, ""
        try:
            deadline = _utc(expires_at) if expires_at else _utc(effect.submitted_at) + timedelta(
                seconds=APPROVAL_TTL_SECONDS[effect.kind]
            )
            return deadline, deadline.isoformat()
        except Exception:
            # A malformed deadline on a time-bounded effect is unsafe to honor.
            return datetime.min.replace(tzinfo=timezone.utc), expires_at or "invalid_or_missing"

    async def expire_pending_checkpoint(self, checkpoint: ExecutionCheckpoint) -> EffectRecord | None:
        """Close a due approval at the decision door, before accepting an answer."""

        effect_id = str(dict((checkpoint.payload or {}).get("effect", {}) or {}).get("effect_id") or "").strip()
        if not effect_id or str(checkpoint.status or "") != "pending":
            return None
        effect = await asyncio.to_thread(self.gateway.journal.get, effect_id)
        self._assert_checkpoint_binds_effect(checkpoint, effect)
        deadline, expires_at = self._deadline(checkpoint, effect)
        if deadline is None or self.clock().astimezone(timezone.utc) < deadline:
            return None
        persisted, closed = await self.coordinator.close_pending_owner_checkpoint(
            checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            status="expired",
            payload_patch={
                "administrative_reason": "approval_expired",
                "administrative_closed_at": self.clock().astimezone(timezone.utc).isoformat(),
            },
        )
        if not closed:
            return None
        return await asyncio.to_thread(
            self.gateway.journal.expire_approval,
            effect.effect_id,
            expires_at=expires_at,
            expired_by="effect-approval-decision-door",
        )

    async def propose_for_approval(
        self,
        *,
        provider: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        prompt: str,
        preview: dict[str, Any] | None = None,
        approval_type: ApprovalType = ApprovalType.BUSINESS,
        project_id: str = "default",
        run_id: str = "",
        customer_id: str = "",
        environment_id: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        ownership: dict[str, Any] | None = None,
        approval_ttl_seconds: float | None = None,
        server_execution_scope: dict[str, Any] | None = None,
    ) -> EffectApprovalPublication:
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("effect approval prompt is required")
        effect = await asyncio.to_thread(
            self.gateway.propose,
            provider=provider,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
            approval_type=approval_type,
            project_id=project_id,
            run_id=run_id,
            customer_id=customer_id,
            environment_id=environment_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            server_execution_scope=server_execution_scope,
        )
        # Approval must be based on a recorded dry-run, not just prose supplied
        # by the caller.  Providers that cannot simulate may still return an
        # explicit UNSUPPORTED result, but an incomplete simulation is a hard
        # stop and must never reach the owner as an executable request.
        if effect.state == EffectState.PROPOSED:
            effect = await self.gateway.simulate(effect.effect_id)
        if effect.simulation_status == SimulationStatus.INCOMPLETE:
            raise ValueError(
                "effect simulation is incomplete and cannot be submitted for approval"
            )
        effect = await asyncio.to_thread(
            self.gateway.journal.submit_for_approval,
            effect.effect_id,
        )
        ttl = approval_ttl_seconds if approval_ttl_seconds is not None else APPROVAL_TTL_SECONDS.get(kind)
        approval: dict[str, Any] = {
            "approval_type": effect.approval_type.value,
            "requires_authenticated_owner": True,
            "scope": "once",
        }
        if ttl is not None:
            ttl = min(3600.0, max(60.0, float(ttl)))
            submitted = _utc(effect.submitted_at)
            approval["ttl_seconds"] = ttl
            approval["expires_at"] = (submitted + timedelta(seconds=ttl)).isoformat()
            clean_prompt += f"；本次批准有效至 {approval['expires_at']}，过期不会执行"
        checkpoint_id = self.checkpoint_id_for_effect(effect.effect_id)
        domain_key = self.domain_key_for_effect(effect.effect_id)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id=checkpoint_id,
            project_id=effect.project_id,
            session_id=session_id,
            checkpoint_type=EFFECT_APPROVAL_CHECKPOINT_TYPE,
            status="pending",
            task_id=task_id,
            payload={
                "schema_version": 1,
                "effect_approval": {"protocol": EFFECT_APPROVAL_PROTOCOL},
                "interaction": {
                    "kind": EFFECT_APPROVAL_CHECKPOINT_TYPE,
                    "domain_key": domain_key,
                    "prompt": clean_prompt,
                    "options": [
                        {"id": "approve_once", "label": "批准并执行"},
                        {"id": "deny", "label": "拒绝"},
                    ],
                    "ownership": dict(ownership or {}),
                },
                "effect": self._checkpoint_effect_identity(effect),
                "effect_preview": dict(preview or {}),
                "effect_simulation": {
                    "status": effect.simulation_status.value,
                    "details": dict(effect.simulation_details or {}),
                },
                "approval": approval,
            },
        )
        persisted, created = await self.coordinator.publish_owner_checkpoint(
            checkpoint,
            interaction_key=domain_key,
            supersede_pending_scope=False,
        )
        self._assert_checkpoint_binds_effect(persisted, effect)
        if not created:
            expired = await self.expire_pending_checkpoint(persisted)
            if expired is not None:
                refreshed = await self.coordinator.store.get_execution_checkpoint(
                    persisted.checkpoint_id,
                    project_id=persisted.project_id,
                    checkpoint_type=persisted.checkpoint_type,
                )
                return EffectApprovalPublication(expired, refreshed or persisted, False)
        return EffectApprovalPublication(effect, persisted, created)

    async def consume_answered(
        self,
        *,
        checkpoint_id: str,
        consumer_id: str,
        authorized_by: str,
        claimed_by: str,
        lease_seconds: float = 300.0,
        orphaned_after_seconds: float = 60.0,
    ) -> EffectApprovalConsumption:
        clean_authorized_by = str(authorized_by or "").strip()
        if not clean_authorized_by:
            raise ValueError("authenticated authorized_by is required")
        claim = await self.coordinator.claim_answered(
            checkpoint_id=checkpoint_id,
            checkpoint_type=EFFECT_APPROVAL_CHECKPOINT_TYPE,
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
        )
        if not claim.acquired or claim.checkpoint is None:
            checkpoint = claim.checkpoint
            if checkpoint is not None and checkpoint.status == "outcome_unknown":
                effect_id = str(
                    dict((checkpoint.payload or {}).get("effect", {}) or {}).get(
                        "effect_id"
                    )
                    or ""
                ).strip()
                effect = await asyncio.to_thread(
                    self.gateway.journal.get,
                    effect_id,
                )
                self._assert_checkpoint_binds_effect(checkpoint, effect)
                if effect.state == EffectState.CLAIMED:
                    effect = await asyncio.to_thread(
                        self.gateway.journal.mark_outcome_unknown,
                        effect.effect_id,
                        claim_token=effect.claim_token,
                        error=(
                            "checkpoint execution lease expired; provider outcome "
                            "cannot be proven"
                        ),
                    )
                return EffectApprovalConsumption(
                    status=effect.state.value,
                    effect=effect,
                    checkpoint=checkpoint,
                    detail=(
                        "checkpoint recovery terminalized after the effect fence; "
                        "the provider was not called again"
                    ),
                )
            return EffectApprovalConsumption(
                status=claim.outcome,
                effect=None,
                checkpoint=checkpoint,
            )

        checkpoint = claim.checkpoint
        decision = self._checkpoint_decision(checkpoint)
        lease = InteractionDecisionLease(
            checkpoint=checkpoint,
            decision=decision,
            consumer_id=consumer_id,
            claim_id=claim.claim_id,
        )
        return await self.consume_lease(
            lease,
            authorized_by=clean_authorized_by,
            claimed_by=claimed_by,
            orphaned_after_seconds=orphaned_after_seconds,
        )

    async def consume_lease(
        self,
        lease: InteractionDecisionLease,
        *,
        authorized_by: str | None = None,
        claimed_by: str,
        orphaned_after_seconds: float = 60.0,
    ) -> EffectApprovalConsumption:
        """Consume a lease already claimed by the engine's interaction loop."""

        checkpoint = lease.checkpoint
        decision = dict(lease.decision or {})
        clean_authorized_by = str(
            authorized_by or self.authorized_by_from_decision(decision)
        ).strip()
        if not clean_authorized_by:
            raise ValueError("effect approval has no authenticated decision actor")
        effect_id = str(
            dict((checkpoint.payload or {}).get("effect", {}) or {}).get("effect_id")
            or ""
        ).strip()
        effect_started = False
        prepared_claim_token = ""
        try:
            effect = await asyncio.to_thread(self.gateway.journal.get, effect_id)
            self._assert_checkpoint_binds_effect(checkpoint, effect)
            action = str(decision.get("option_id") or "").strip().lower()
            if action == "deny":
                effect = await asyncio.to_thread(
                    self.gateway.journal.reject,
                    effect.effect_id,
                    reason=str(decision.get("text") or "owner denied effect").strip(),
                    rejected_by=clean_authorized_by,
                )
                finished = await self.coordinator.finish(
                    lease,
                    final_status="resolved",
                    payload_patch={"effect_result": self._effect_result_payload(effect)},
                )
                return EffectApprovalConsumption(
                    status=EffectState.REJECTED.value,
                    effect=effect,
                    checkpoint=finished.checkpoint,
                )
            if action != "approve_once":
                raise ValueError(f"unsupported effect approval action: {action}")

            deadline, expires_at = self._deadline(checkpoint, effect)
            if effect.state == EffectState.AWAITING_APPROVAL and deadline is not None:
                if self.clock().astimezone(timezone.utc) >= deadline:
                    effect = await asyncio.to_thread(
                        self.gateway.journal.expire_approval,
                        effect.effect_id,
                        expires_at=expires_at or "invalid_or_missing",
                        expired_by="effect-approval-service",
                    )
                    finished = await self.coordinator.finish(
                        lease,
                        final_status="expired",
                        payload_patch={"effect_result": {"status": "approval_expired", "expires_at": expires_at,
                                                         "provider_called": False}},
                    )
                    return EffectApprovalConsumption(
                        status="approval_expired", effect=effect, checkpoint=finished.checkpoint,
                        provider_called=False, detail="approval expired before execution; re-observe and propose again",
                    )

            decision_record = dict(
                dict((checkpoint.payload or {}).get("interaction", {}) or {}).get(
                    "decision", {}
                )
                or {}
            )
            decision_hash = str(decision_record.get("decision_hash") or "").strip()
            if not decision_hash:
                raise ValueError("effect approval checkpoint has no durable decision hash")
            effect = await asyncio.to_thread(
                self.gateway.journal.authorize,
                effect.effect_id,
                authorization_id=self.authorization_id(
                    checkpoint.checkpoint_id,
                    decision_hash,
                ),
                authorized_by=clean_authorized_by,
            )
            if effect.state != EffectState.CLAIMED:
                prepared_claim = await asyncio.to_thread(
                    self.gateway.claim_authorized,
                    effect.effect_id,
                    claimed_by=claimed_by,
                )
                if not prepared_claim.acquired:
                    raise RuntimeError(
                        f"could not claim authorized effect: {prepared_claim.reason}"
                    )
                effect = prepared_claim.record
                prepared_claim_token = effect.claim_token
            effect_fence = await self.coordinator.begin_effect(lease)
            if not effect_fence.acquired:
                if prepared_claim_token:
                    await asyncio.to_thread(
                        self.gateway.journal.release_claim,
                        effect.effect_id,
                        claim_token=prepared_claim_token,
                        reason=(
                            "checkpoint effect fence was not acquired: "
                            f"{effect_fence.outcome}"
                        ),
                    )
                    prepared_claim_token = ""
                raise RuntimeError(
                    f"could not persist checkpoint effect fence: {effect_fence.outcome}"
                )
            effect_started = True

            if prepared_claim_token:
                execution = await self.gateway.apply_claimed(
                    effect.effect_id,
                    claim_token=prepared_claim_token,
                )
            else:
                execution = await self.gateway.recover_and_execute(
                    effect.effect_id,
                    claimed_by=claimed_by,
                    orphaned_after_seconds=orphaned_after_seconds,
                )
            final_status = (
                "outcome_unknown"
                if execution.status in {"outcome_unknown", "retryable"}
                else "resolved"
            )
            effect_result = self._execution_result_payload(execution)
            if final_status == "outcome_unknown" and self.on_outcome_unknown is not None:
                # Before the checkpoint settles: the hold is the money-safety
                # step and the settlement is the bookkeeping. A hold that
                # could not be placed is recorded here, not swallowed -- the
                # owner reading "outcome unknown" must also read whether the
                # run is still free to spend.
                try:
                    await self.on_outcome_unknown(checkpoint, execution)
                    effect_result["budget_hold"] = "placed"
                except Exception as exc:  # noqa: BLE001 - recorded, never hides the settlement
                    effect_result["budget_hold"] = f"failed: {str(exc) or type(exc).__name__}"[:300]
            finished = await self.coordinator.finish(
                lease,
                final_status=final_status,
                payload_patch={"effect_result": effect_result},
            )
            if not finished.applied:
                raise RuntimeError(
                    f"effect completed but checkpoint settlement failed: {finished.outcome}"
                )
            return EffectApprovalConsumption(
                status=execution.status,
                effect=execution.record,
                checkpoint=finished.checkpoint,
                provider_called=execution.provider_called,
                replay_noop=execution.replay_noop,
                detail=execution.detail,
            )
        except Exception as exc:
            if not effect_started:
                if prepared_claim_token:
                    try:
                        await asyncio.to_thread(
                            self.gateway.journal.release_claim,
                            effect_id,
                            claim_token=prepared_claim_token,
                            reason=f"checkpoint effect fence failed: {exc}",
                        )
                    except Exception:
                        # The exact claim may already have been recovered or
                        # terminalized. Preserve the original failure and let
                        # startup reconciliation inspect both durable ledgers.
                        pass
                await self.coordinator.release(
                    lease,
                    reason=f"effect approval consumption failed before effect fence: {exc}",
                    payload_patch={"effect_consumer_error": str(exc)},
                )
            else:
                await self.coordinator.finish(
                    lease,
                    final_status="outcome_unknown",
                    payload_patch={
                        "effect_consumer_error": str(exc),
                        "effect_id": effect_id,
                    },
                )
            raise

    @staticmethod
    def authorized_by_from_decision(decision: dict[str, Any]) -> str:
        actor = dict((decision or {}).get("authorization_actor", {}) or {})
        return str(
            actor.get("actor_id")
            or actor.get("login")
            or actor.get("employee_id")
            or ""
        ).strip()

    @staticmethod
    def checkpoint_id_for_effect(effect_id: str) -> str:
        clean_effect_id = str(effect_id or "").strip()
        if not clean_effect_id:
            raise ValueError("effect_id is required")
        return f"effect-approval:{clean_effect_id}"

    @staticmethod
    def domain_key_for_effect(effect_id: str) -> str:
        clean_effect_id = str(effect_id or "").strip()
        if not clean_effect_id:
            raise ValueError("effect_id is required")
        return f"effect:{clean_effect_id}"

    @staticmethod
    def authorization_id(checkpoint_id: str, decision_hash: str) -> str:
        return f"checkpoint:{str(checkpoint_id).strip()}:{str(decision_hash).strip()}"

    @staticmethod
    def _checkpoint_decision(checkpoint: ExecutionCheckpoint) -> dict[str, Any]:
        interaction = dict((checkpoint.payload or {}).get("interaction", {}) or {})
        decision = dict(interaction.get("decision", {}) or {})
        value = decision.get("value")
        if not isinstance(value, dict):
            raise ValueError("effect approval checkpoint has no structured decision")
        return dict(value)

    @staticmethod
    def _checkpoint_effect_identity(effect: EffectRecord) -> dict[str, Any]:
        return {
            "effect_id": effect.effect_id,
            "provider": effect.provider,
            "kind": effect.kind,
            "payload_hash": effect.payload_hash,
            "project_id": effect.project_id,
            "run_id": effect.run_id,
            "customer_id": effect.customer_id,
            "environment_id": effect.environment_id,
            "correlation_id": effect.correlation_id,
            "causation_id": effect.causation_id,
        }

    @classmethod
    def _assert_checkpoint_binds_effect(
        cls,
        checkpoint: ExecutionCheckpoint,
        effect: EffectRecord,
    ) -> None:
        if checkpoint.checkpoint_type != EFFECT_APPROVAL_CHECKPOINT_TYPE:
            raise ValueError("checkpoint is not an effect approval")
        protocol = str(
            dict((checkpoint.payload or {}).get("effect_approval", {}) or {}).get(
                "protocol"
            )
            or ""
        ).strip()
        if protocol != EFFECT_APPROVAL_PROTOCOL:
            raise ValueError("checkpoint is not a SuperOnes effect approval")
        recorded = dict((checkpoint.payload or {}).get("effect", {}) or {})
        expected = cls._checkpoint_effect_identity(effect)
        if recorded != expected:
            raise ValueError("effect approval checkpoint identity does not match journal")
        if checkpoint.project_id != effect.project_id:
            raise ValueError("effect approval crossed project scope")

    @staticmethod
    def _effect_result_payload(effect: EffectRecord) -> dict[str, Any]:
        return {
            "effect_id": effect.effect_id,
            "state": effect.state.value,
            "provider": effect.provider,
            "kind": effect.kind,
            "provider_receipt": dict(effect.provider_receipt or {}),
            "last_error": effect.last_error,
            "resolved_at": effect.resolved_at,
        }

    @classmethod
    def _execution_result_payload(
        cls, execution: EffectExecutionResult
    ) -> dict[str, Any]:
        return {
            **cls._effect_result_payload(execution.record),
            "status": execution.status,
            "provider_called": execution.provider_called,
            "replay_noop": execution.replay_noop,
            "detail": execution.detail,
            "verification_status": execution.verification_status.value,
            "verification_details": dict(execution.verification_details or {}),
        }
