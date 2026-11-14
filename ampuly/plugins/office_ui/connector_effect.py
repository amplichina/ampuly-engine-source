"""Effect Provider for one admitted declarative connector contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from ampuly.effects import (
    EffectRecord,
    ProviderOutcome,
    ProviderResult,
    SimulationStatus,
    VerificationStatus,
)

from .connector_contract import WRITE
from .connector_runtime import DeclarativeConnectorAdapter


class DeclarativeConnectorEffectProvider:
    """Apply only payloads pinned to this exact connector contract.

    The tool layer creates proposals.  Only EffectGateway calls ``apply``
    after durable authorization and claim.  A transport exception remains
    ambiguous; it is never rewritten as a clean failure.
    """

    def __init__(
        self,
        adapter: DeclarativeConnectorAdapter,
        *,
        environment: Callable[[], Mapping[str, str]],
        transport: Callable[..., Any],
        enabled: Callable[[], bool | tuple[bool, str]] | None = None,
    ) -> None:
        self.adapter = adapter
        self.environment = environment
        self.transport = transport
        self.enabled = enabled
        # The provider identity is version/fingerprint specific.  Replacing a
        # connector must not make an in-flight Effect execute through newer
        # code after restart.
        self.name = (
            f"connector:{adapter.contract.name}:"
            f"{adapter.contract.fingerprint()[:16]}"
        )
        self.supports_idempotency = all(
            bool(operation.idempotency)
            for operation in adapter.contract.writes
        )

    async def simulate(self, effect: EffectRecord) -> tuple[SimulationStatus, dict[str, Any]]:
        operation, params = self._payload(effect)
        # Prepare validates path/parameter/auth-reference shape.  The preview
        # deliberately omits values and the rendered URL.
        self.adapter.prepare(
            operation, params, environment=dict(self.environment()),
            write_authorized=True, idempotency_key=effect.idempotency_key,
        )
        return SimulationStatus.PREVIEW_ONLY, {
            "connector": self.adapter.contract.name,
            "connector_version": self.adapter.contract.version,
            "contract_fingerprint": self.adapter.contract.fingerprint(),
            "operation": operation,
            "parameter_names": sorted(params),
        }

    async def apply(self, effect: EffectRecord, *, idempotency_key: str) -> ProviderResult:
        allowed, reason = self._enabled()
        if not allowed:
            return ProviderResult(
                ProviderOutcome.KNOWN_NO_EFFECT,
                detail="connector disabled" + (f": {reason}" if reason else ""),
            )
        operation, params = self._payload(effect)
        response = await asyncio.to_thread(
            self.adapter.execute,
            operation,
            params,
            environment=dict(self.environment()),
            transport=self.transport,
            write_authorized=True,
            idempotency_key=idempotency_key,
        )
        if response.status == "succeeded":
            digest = hashlib.sha256(
                json.dumps(response.body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            return ProviderResult(
                ProviderOutcome.APPLIED,
                receipt={
                    "connector": self.adapter.contract.name,
                    "operation": operation,
                    "status_code": response.status_code,
                    "response_sha256": digest,
                },
            )
        if response.status == "outcome_unknown":
            return ProviderResult(ProviderOutcome.UNKNOWN, detail=response.detail)
        # A non-success HTTP response proves a response, not that a remote
        # service definitely performed no mutation before returning it.
        return ProviderResult(
            ProviderOutcome.UNKNOWN,
            detail=response.detail or f"connector returned {response.status_code or response.status}",
        )

    async def verify(self, effect: EffectRecord) -> tuple[VerificationStatus, dict[str, Any]]:
        return VerificationStatus.UNSUPPORTED, {
            "reason": "connector contract declares no independent read-back verification"
        }

    def _payload(self, effect: EffectRecord) -> tuple[str, dict[str, Any]]:
        payload = dict(effect.payload or {})
        contract = self.adapter.contract
        if str(payload.get("connector", "") or "") != contract.name:
            raise ValueError("effect connector identity differs from provider")
        if str(payload.get("contract_fingerprint", "") or "") != contract.fingerprint():
            raise ValueError("effect connector contract fingerprint drifted")
        operation = str(payload.get("operation", "") or "").strip()
        declared = next((item for item in contract.operations if item.op_id == operation), None)
        if declared is None or declared.kind != WRITE:
            raise ValueError("effect does not name a declared connector write")
        params = payload.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("effect connector params must be an object")
        return operation, dict(params)

    def _enabled(self) -> tuple[bool, str]:
        if self.enabled is None:
            return True, ""
        result = self.enabled()
        return result if isinstance(result, tuple) else (bool(result), "")
