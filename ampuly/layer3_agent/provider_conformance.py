"""Provider declaration checks, never a substitute for behavior evidence.

The report is intentionally evidence-oriented.  A provider is production
eligible only when every fence is ``pass``; missing proof is ``unverified``
rather than an optimistic pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from ampuly.layer3_agent.adapters.base import ExternalAgentAdapter


ConformanceState = Literal["pass", "fail", "unverified"]


@dataclass(frozen=True)
class ConformanceCheck:
    name: str
    state: ConformanceState
    evidence: str
    required: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "evidence": self.evidence,
            "required": self.required,
        }


@dataclass
class ProviderConformanceReport:
    provider: str
    checks: list[ConformanceCheck] = field(default_factory=list)

    @property
    def production_ready(self) -> bool:
        # This report is built exclusively from describe/capabilities. Even
        # unanimous declarations cannot authorize a production process. The
        # identity-bound behavior-evidence admission path is a separate gate.
        return False

    @property
    def declarations_complete(self) -> bool:
        expected = {"identity", "scope", "sandbox", "credentials", "tool_whitelist", "approval", "cancel_resume", "resource_budget"}
        names = [check.name for check in self.checks]
        return len(names) == len(set(names)) and expected.issubset(names) and all(
            check.state == "pass" and bool(check.evidence.strip())
            for check in self.checks if check.required or check.name in expected
        )

    @property
    def blockers(self) -> list[str]:
        return [check.name for check in self.checks if check.required and check.state != "pass"] + ["identity_bound_behavior_evidence_missing"]

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "production_ready": self.production_ready,
            "evidence_kind": "declaration_only",
            "declarations_complete": self.declarations_complete,
            "blockers": self.blockers,
            "checks": [check.to_payload() for check in self.checks],
        }


def _sandbox_evidence(sandbox: dict[str, Any]) -> str:
    """What we asked the provider for, never a verdict about what it did.

    The token alone read the same for four adapters that request four
    different things, and some of those requests widen what the provider
    will do rather than confine it. An operator deciding whether to admit
    a provider needs the flags, and needs to be told whose enforcement it
    would be.
    """
    parts = [str(sandbox.get("sandbox_enforcement") or "sandbox enforcement evidence missing")]
    flags = [str(item) for item in (sandbox.get("sandbox_requested_flags") or []) if str(item).strip()]
    if flags:
        parts.append("requested: " + " ".join(flags))
    if str(sandbox.get("sandbox_enforcement_owner") or "") == "none":
        parts.append("nothing confines this run")
    return "; ".join(parts)


def evaluate_provider(adapter: ExternalAgentAdapter) -> ProviderConformanceReport:
    """Evaluate the eight production fences without launching a provider."""
    provider = str(getattr(adapter, "agent_type", "unknown") or "unknown").strip() or "unknown"
    try:
        description = adapter.describe()
    except Exception as exc:
        description = {"_error": f"describe failed: {exc}"}
    try:
        capabilities = adapter.capabilities()
    except Exception as exc:
        capabilities = {"_error": f"capabilities failed: {exc}"}
    try:
        sandbox = adapter.sandbox_status()
    except Exception as exc:
        sandbox = {"_error": f"sandbox status failed: {exc}"}
    if not isinstance(description, dict):
        description = {"_error": "provider description is not a mapping"}
    if not isinstance(capabilities, dict):
        capabilities = {"_error": "provider capabilities are not a mapping"}
    if not isinstance(sandbox, dict):
        sandbox = {"_error": "provider sandbox status is not a mapping"}

    def positive_timeout(value: Any) -> bool:
        try:
            number = float(value or 0)
            return not isinstance(value, bool) and math.isfinite(number) and number > 0
        except (TypeError, ValueError):
            return False

    project_dir = str(description.get("project_dir") or "").strip()

    checks = [
        ConformanceCheck(
            "identity",
            "pass" if str(description.get("agent") or "").strip() == adapter.agent_type else "fail",
            "adapter.agent_type matches describe().agent" if str(description.get("agent") or "").strip() == adapter.agent_type else "agent identity mismatch",
        ),
        ConformanceCheck(
            "scope",
            "pass" if project_dir else "unverified",
            "project_dir is exposed in the provider description" if project_dir else "provider does not expose a non-empty project scope",
        ),
        ConformanceCheck(
            "sandbox",
            "pass" if sandbox.get("sandbox_enforced") is True and sandbox.get("sandbox_mode") != "unverified" else "unverified",
            _sandbox_evidence(sandbox),
        ),
        ConformanceCheck(
            "credentials",
            "pass" if capabilities.get("credential_boundary") is True else "unverified",
            "provider reports credential boundary" if capabilities.get("credential_boundary") is True else "no provider credential-boundary evidence",
        ),
        ConformanceCheck(
            "tool_whitelist",
            "pass" if capabilities.get("tool_whitelist") is True else "unverified",
            "provider reports execution-layer tool whitelist" if capabilities.get("tool_whitelist") is True else "no execution-layer whitelist evidence",
        ),
        ConformanceCheck(
            "approval",
            "pass" if capabilities.get("approval_bridge") is True else "unverified",
            "provider reports approval bridge" if capabilities.get("approval_bridge") is True else "approval bridge not proven",
        ),
        ConformanceCheck(
            "cancel_resume",
            "pass" if capabilities.get("session_resume") is True and adapter.__class__.cancel is not ExternalAgentAdapter.cancel else "unverified",
            "resume and adapter cancellation are implemented" if capabilities.get("session_resume") is True and adapter.__class__.cancel is not ExternalAgentAdapter.cancel else "resume/cancel evidence incomplete",
        ),
        ConformanceCheck(
            "resource_budget",
            "pass" if positive_timeout(description.get("idle_timeout_seconds")) else "fail",
            "positive idle timeout" if positive_timeout(description.get("idle_timeout_seconds")) else "idle timeout is not bounded",
        ),
    ]
    return ProviderConformanceReport(provider=provider, checks=checks)
