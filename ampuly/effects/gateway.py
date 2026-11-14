"""Execution boundary for authorized effects."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from .journal import EffectJournal
from .models import (
    ApprovalType,
    EffectClaim,
    EffectExecutionResult,
    EffectProvider,
    EffectRecord,
    EffectState,
    EffectVerification,
    ProviderOutcome,
    SimulationStatus,
    VerificationStatus,
)


class EffectGateway:
    def __init__(self, journal: EffectJournal) -> None:
        self.journal = journal
        self._providers: dict[str, EffectProvider] = {}
        self._execution_guard: Callable[[EffectRecord], Any] | None = None
        self._dispatch_fence: Callable[[Any, EffectRecord], Any] | None = None

    def bind_execution_guard(self, guard: Callable[[EffectRecord], Any]) -> None:
        """Bind the host's CURRENT scope/holder/Stop/budget check, not a health view.

        The host owns those facts; the effect journal remains the sole claim
        owner. Return None/empty to allow, a stable refusal reason to deny.
        Exceptions and malformed results refuse dispatch. The guard may await,
        so dispatch rechecks the durable exact claim afterwards.
        """
        if not callable(guard):
            raise TypeError("effect execution guard must be callable")
        self._execution_guard = guard

    def bind_dispatch_fence(self, fence: Callable[[Any, EffectRecord], Any]) -> None:
        """Bind a synchronous authority check run inside the dispatch transaction.

        ``fence(connection, record)`` reads host facts on the journal's own
        write-locked connection, so authority lost after the (awaiting) guard
        and before the dispatch crossing is refused. Same result contract as
        the guard: None/empty allows, a refusal string denies.
        """
        if not callable(fence):
            raise TypeError("effect dispatch fence must be callable")
        self._dispatch_fence = fence

    def register(self, provider: EffectProvider) -> None:
        name = str(provider.name or "").strip()
        if not name:
            raise ValueError("effect provider name is required")
        if name in self._providers and self._providers[name] is not provider:
            raise ValueError(f"effect provider already registered: {name}")
        self._providers[name] = provider

    def providers(self) -> list[EffectProvider]:
        """Every registered provider, for a caller that has to bind one thing
        into all of them (a business-layer check, for instance) and should not
        have to know the private map to do it."""
        return list(self._providers.values())

    def propose(
        self,
        *,
        provider: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        approval_type: ApprovalType = ApprovalType.BUSINESS,
        project_id: str = "default",
        run_id: str = "",
        customer_id: str = "",
        environment_id: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        server_execution_scope: dict[str, Any] | None = None,
    ) -> EffectRecord:
        implementation = self._provider(provider)
        return self.journal.propose(
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
            provider_supports_idempotency=bool(implementation.supports_idempotency),
            claim_before_apply=True,
        )

    async def simulate(self, effect_id: str) -> EffectRecord:
        record = await asyncio.to_thread(self.journal.get, effect_id)
        provider = self._provider(record.provider)
        simulate = getattr(provider, "simulate", None)
        if not callable(simulate):
            return await asyncio.to_thread(
                self.journal.record_simulation,
                effect_id,
                status=SimulationStatus.UNSUPPORTED,
                details={"reason": "provider does not implement simulation"},
            )
        status, details = await simulate(record)
        return await asyncio.to_thread(
            self.journal.record_simulation,
            effect_id,
            status=SimulationStatus(status),
            details=dict(details or {}),
        )

    async def verify(self, effect_id: str) -> EffectVerification:
        """Record provider verification without performing another write.

        Verification is deliberately best-effort observability: an applied
        effect remains applied even when a provider cannot verify it. Provider
        exceptions become explicit ``incomplete`` evidence instead of being
        mistaken for a second execution attempt.
        """

        record = await asyncio.to_thread(self.journal.get, effect_id)
        if record.state != EffectState.APPLIED:
            return EffectVerification(
                effect_id=record.effect_id,
                status=VerificationStatus.INCOMPLETE,
                details={"reason": f"effect state is {record.state.value}"},
            )
        provider = self._provider(record.provider)
        verifier = getattr(provider, "verify", None)
        if not callable(verifier):
            status = VerificationStatus.UNSUPPORTED
            details: dict[str, Any] = {
                "reason": "provider does not implement verification"
            }
        else:
            try:
                raw_status, raw_details = await verifier(record)
                status = VerificationStatus(raw_status)
                details = dict(raw_details or {})
            except Exception as exc:  # verification never retries the effect
                status = VerificationStatus.INCOMPLETE
                details = {"reason": str(exc) or exc.__class__.__name__}
        event = await asyncio.to_thread(
            self.journal.record_verification,
            record.effect_id,
            status=status,
            details=details,
        )
        return EffectVerification(
            effect_id=record.effect_id,
            status=status,
            details=details,
            event_id=event.event_id,
            verified_at=event.created_at,
        )

    async def look_back(self, effect_id: str, *, requested_by: str = "") -> EffectVerification:
        """Resolve an outcome_unknown effect from independent evidence, never by applying again (C10).

        The provider's verification looks at the world (the page, an API, a
        business event) and holds the effect's stated expectation against it.
        Complete evidence resolves the effect as applied; anything weaker is
        recorded and the effect stays unknown for a person to decide.
        """

        record = await asyncio.to_thread(self.journal.get, effect_id)
        if record.state != EffectState.OUTCOME_UNKNOWN:
            return EffectVerification(
                effect_id=record.effect_id,
                status=VerificationStatus.INCOMPLETE,
                details={"reason": f"effect state is {record.state.value}; a look-back is for outcome_unknown",
                         "resolved": False, "state": record.state.value},
            )
        provider = self._provider(record.provider)
        verifier = getattr(provider, "verify", None)
        if not callable(verifier):
            status = VerificationStatus.UNSUPPORTED
            details: dict[str, Any] = {"reason": "provider does not implement verification"}
        else:
            try:
                raw_status, raw_details = await verifier(record)
                status = VerificationStatus(raw_status)
                details = dict(raw_details or {})
            except Exception as exc:  # a look-back never retries the effect
                status = VerificationStatus.INCOMPLETE
                details = {"reason": str(exc) or exc.__class__.__name__}
        details = {**details, "requested_by": str(requested_by or "")[:120]}
        updated = await asyncio.to_thread(
            self.journal.record_look_back,
            record.effect_id,
            status=status,
            details=details,
        )
        return EffectVerification(
            effect_id=record.effect_id,
            status=status,
            details={**details, "resolved": updated.state == EffectState.APPLIED, "state": updated.state.value},
        )

    async def execute_authorized(
        self, effect_id: str, *, claimed_by: str
    ) -> EffectExecutionResult:
        claim = await asyncio.to_thread(
            self.claim_authorized,
            effect_id,
            claimed_by=claimed_by,
        )
        if not claim.acquired:
            return EffectExecutionResult(
                status=claim.reason,
                record=claim.record,
                provider_called=False,
                replay_noop=claim.reason == "already_applied",
            )
        return await self.apply_claimed(
            claim.record.effect_id,
            claim_token=claim.record.claim_token,
        )

    def claim_authorized(self, effect_id: str, *, claimed_by: str) -> EffectClaim:
        """Durably claim an authorized effect without calling its provider."""

        current = self.journal.get(effect_id)
        if current.state == EffectState.APPLIED:
            return EffectClaim(False, current, "already_applied")
        claim_token = str(uuid.uuid4())
        return self.journal.claim(
            effect_id,
            claim_token=claim_token,
            claimed_by=claimed_by,
        )

    async def apply_claimed(
        self,
        effect_id: str,
        *,
        claim_token: str,
    ) -> EffectExecutionResult:
        """Call a provider only while holding the exact durable effect claim."""

        record = await asyncio.to_thread(self.journal.get, effect_id)
        clean_token = str(claim_token or "").strip()
        if record.state != EffectState.CLAIMED or record.claim_token != clean_token:
            return EffectExecutionResult(
                status="claim_lost",
                record=record,
                provider_called=False,
            )
        return await self._apply_claim(record, claim_token=clean_token)

    async def recover_and_execute(
        self,
        effect_id: str,
        *,
        claimed_by: str,
        orphaned_after_seconds: float,
        now: datetime | None = None,
    ) -> EffectExecutionResult:
        claim_token = str(uuid.uuid4())
        claim = await asyncio.to_thread(
            self.journal.recover_orphaned_claim,
            effect_id,
            claim_token=claim_token,
            claimed_by=claimed_by,
            orphaned_after_seconds=orphaned_after_seconds,
            now=now,
        )
        if not claim.acquired:
            return EffectExecutionResult(
                status=claim.reason,
                record=claim.record,
                provider_called=False,
            )
        return await self._apply_claim(claim.record, claim_token=claim_token)

    async def _apply_claim(
        self, record: EffectRecord, *, claim_token: str
    ) -> EffectExecutionResult:
        # Both ordinary dispatch and orphan recovery pass through this fence.
        if self._execution_guard is not None:
            try:
                refusal = self._execution_guard(record)
                if inspect.isawaitable(refusal):
                    refusal = await refusal
                if refusal is not None and not isinstance(refusal, str):
                    refusal = "execution_guard_invalid_result"
            except Exception:
                refusal = "execution_guard_unavailable"
            if refusal:
                # The provider was not entered. Preserve the authorized intent
                # for explicit revalidation, rather than marking an unknown
                # external action failed or releasing any run budget.
                try:
                    current = await asyncio.to_thread(
                        self.journal.release_claim, record.effect_id,
                        claim_token=claim_token, reason=str(refusal),
                    )
                except Exception:
                    current = await asyncio.to_thread(self.journal.get, record.effect_id)
                return EffectExecutionResult(status=str(refusal), record=current, provider_called=False)
        if self._dispatch_fence is not None:
            dispatch = await asyncio.to_thread(
                self.journal.begin_dispatch, record.effect_id,
                claim_token=claim_token, fence=self._dispatch_fence,
            )
        else:
            dispatch = await asyncio.to_thread(
                self.journal.begin_dispatch, record.effect_id, claim_token=claim_token
            )
        if not dispatch.acquired:
            if dispatch.reason not in {"claim_lost", "dispatch_already_started"}:
                # Fence refusal: the provider was not entered; keep the
                # authorized intent for explicit revalidation, as above.
                try:
                    current = await asyncio.to_thread(
                        self.journal.release_claim, record.effect_id,
                        claim_token=claim_token, reason=str(dispatch.reason),
                    )
                except Exception:
                    current = await asyncio.to_thread(self.journal.get, record.effect_id)
                return EffectExecutionResult(status=str(dispatch.reason), record=current, provider_called=False)
            return EffectExecutionResult(status=dispatch.reason, record=dispatch.record, provider_called=False)
        record = dispatch.record
        provider = self._provider(record.provider)
        try:
            result = await provider.apply(
                record,
                idempotency_key=record.idempotency_key,
            )
        except Exception as exc:  # provider exceptions cannot prove no effect
            detail = str(exc) or exc.__class__.__name__
            if record.provider_supports_idempotency:
                updated = await asyncio.to_thread(
                    self.journal.record_retryable_error,
                    record.effect_id,
                    claim_token=claim_token,
                    error=detail,
                )
                return EffectExecutionResult(
                    status="retryable",
                    record=updated,
                    provider_called=True,
                    detail=detail,
                )
            updated = await asyncio.to_thread(
                self.journal.mark_outcome_unknown,
                record.effect_id,
                claim_token=claim_token,
                error=detail,
            )
            return EffectExecutionResult(
                status=EffectState.OUTCOME_UNKNOWN.value,
                record=updated,
                provider_called=True,
                detail=detail,
            )

        if result.outcome == ProviderOutcome.APPLIED:
            updated = await asyncio.to_thread(
                self.journal.finish_applied,
                record.effect_id,
                claim_token=claim_token,
                provider_receipt=result.receipt,
            )
            execution = EffectExecutionResult(
                status=EffectState.APPLIED.value,
                record=updated,
                provider_called=True,
                detail=result.detail,
            )
            verification = await self.verify(updated.effect_id)
            return replace(
                execution,
                verification_status=verification.status,
                verification_details={
                    **verification.details,
                    "event_id": verification.event_id,
                    "verified_at": verification.verified_at,
                },
            )
        if result.outcome == ProviderOutcome.KNOWN_NO_EFFECT:
            updated = await asyncio.to_thread(
                self.journal.finish_failed,
                record.effect_id,
                claim_token=claim_token,
                error=result.detail or "provider confirmed no effect",
            )
            return EffectExecutionResult(
                status=EffectState.FAILED.value,
                record=updated,
                provider_called=True,
                detail=result.detail,
            )

        detail = result.detail or "provider outcome is unknown"
        if record.provider_supports_idempotency:
            updated = await asyncio.to_thread(
                self.journal.record_retryable_error,
                record.effect_id,
                claim_token=claim_token,
                error=detail,
            )
            status = "retryable"
        else:
            updated = await asyncio.to_thread(
                self.journal.mark_outcome_unknown,
                record.effect_id,
                claim_token=claim_token,
                error=detail,
            )
            status = EffectState.OUTCOME_UNKNOWN.value
        return EffectExecutionResult(
            status=status,
            record=updated,
            provider_called=True,
            detail=detail,
        )

    def _provider(self, name: str) -> EffectProvider:
        clean_name = str(name or "").strip()
        try:
            return self._providers[clean_name]
        except KeyError as exc:
            raise KeyError(f"effect provider is not registered: {clean_name}") from exc
