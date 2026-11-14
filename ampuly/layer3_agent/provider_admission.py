"""The path from behaviour evidence to an admission decision.

Two gates existed and neither could ever say yes. The declaration report is
built from what a Provider says about itself and returns ``production_ready``
as a hardcoded False, correctly: a declaration is not evidence. The behaviour
suite could produce evidence but nothing read its report, so a Provider that
actually passed had no way to be admitted.

This is the missing half, and it is deliberately narrow. Admission needs a
suite report that ran live, whose identity matches the Provider standing in
front of us right now, and whose every required assertion passed with
evidence. Anything else is refused with the reasons named, because "not
admitted" has to be readable to be actionable.

Nothing here admits anything today: the suite still cannot exercise five of
its assertions, so a real report still carries blockers. That is the honest
state, and this module reports it rather than hiding it behind a False.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ampuly.layer3_agent.conformance_suite import AssertionResult, EvidenceIdentity, SuiteReport

# Where a report lives once it has been produced. One file per provider, under
# the runtime home, because an admission decision must survive the process
# that made the evidence.
ADMISSION_DIRNAME = "provider_admission"


@dataclass
class AdmissionDecision:
    """Whether this Provider may run production work, and why."""

    provider: str
    admitted: bool
    reasons: list[str] = field(default_factory=list)
    evidence_kind: str = "none"
    recorded_at: str = ""
    identity: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "admitted": self.admitted,
            "reasons": list(self.reasons),
            "evidence_kind": self.evidence_kind,
            "recorded_at": self.recorded_at,
            "identity": self.identity,
        }


def _report_path(home: Path, provider: str) -> Path:
    safe = "".join(ch for ch in str(provider or "") if ch.isalnum() or ch in "-_") or "unknown"
    return Path(home) / ADMISSION_DIRNAME / f"{safe}.json"


def record_suite_report(home: Path, report: SuiteReport) -> Path:
    """Keep one Provider's behaviour evidence where a later process can read it.

    Stored verbatim, including its blockers: a report that did not admit is
    still the answer to "what happened when somebody last checked", and
    discarding it would leave that question unanswerable.
    """
    path = _report_path(Path(home), report.provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorded_at": datetime.now().isoformat(),
        "report": report.to_payload(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path


def stored_suite_report(home: Path, provider: str) -> dict[str, Any] | None:
    """The last recorded evidence for this Provider, or nothing."""
    path = _report_path(Path(home), provider)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            content = handle.read(1_000_001)
        if len(content) > 1_000_000:
            return None
        value = json.loads(content.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        # Unreadable evidence is absent evidence. It is never a pass, and it
        # is not an error either: the caller is asking whether we may admit.
        return None


def _rebuild_report(payload: dict[str, Any]) -> SuiteReport:
    """Reuse the suite's rules after strict decoding, never trust its summary."""
    if not isinstance(payload.get("provider"), str) or not isinstance(payload.get("verification_mode"), str):
        raise ValueError("report identity fields must be strings")
    identity = payload.get("identity")
    identity_fields = {"provider", "provider_version", "implementation_digest", "platform", "configuration_digest"}
    if not isinstance(identity, dict) or set(identity) != identity_fields or any(
        not isinstance(value, str) or len(value) > 4096 for value in identity.values()
    ):
        raise ValueError("invalid evidence identity")
    rows = payload.get("results")
    blockers = payload.get("blockers")
    if not isinstance(rows, list) or len(rows) > 64 or not isinstance(blockers, list) or any(
        not isinstance(item, str) for item in blockers
    ):
        raise ValueError("invalid report collections")
    results = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("required")) is not bool or any(
            not isinstance(row.get(field), str) for field in ("assertion", "state", "evidence")
        ) or not isinstance(row.get("detail", ""), str):
            raise ValueError("invalid assertion fields")
        results.append(AssertionResult(row["assertion"], row["state"], row["evidence"],
                                       row.get("detail", ""), row["required"]))
    return SuiteReport(provider=payload["provider"], results=results,
                       identity=EvidenceIdentity(**identity), verification_mode=payload["verification_mode"])


def decide(
    *,
    provider: str,
    current_identity: EvidenceIdentity | None,
    stored: dict[str, Any] | None,
) -> AdmissionDecision:
    """Admit only on live behaviour evidence that still describes this Provider.

    The four things that must all hold, each refused by name when it does not:
    evidence exists, it ran live, every required assertion passed with its
    evidence, and the identity it was taken under is the identity in front of
    us now. A Provider that changed version, binary or policy since the run
    has no evidence about the thing it is today.
    """
    reasons: list[str] = []
    if current_identity is None or not current_identity.complete:
        reasons.append("current_identity_incomplete")
    if stored is None:
        reasons.append("no_behaviour_evidence_recorded")
        return AdmissionDecision(provider=provider, admitted=False, reasons=reasons, evidence_kind="none")

    report = stored.get("report") if isinstance(stored, dict) else None
    if not isinstance(report, dict):
        return AdmissionDecision(provider=provider, admitted=False,
                                 reasons=reasons + ["malformed_evidence_report"], evidence_kind="invalid")
    try:
        rebuilt = _rebuild_report(report)
    except (TypeError, ValueError, AttributeError):
        return AdmissionDecision(provider=provider, admitted=False,
                                 reasons=reasons + ["malformed_evidence_report"], evidence_kind="invalid")
    recorded_at = str(stored.get("recorded_at", "") or "")
    if str(report.get("provider", "")) != str(provider):
        reasons.append("evidence_belongs_to_another_provider")
    if str(report.get("verification_mode", "")) != "live":
        reasons.append("evidence_was_not_live")
    blockers = list(report["blockers"]) + rebuilt.blockers
    if blockers:
        reasons.extend(blockers)
    if report.get("production_ready") is not True or not rebuilt.production_ready:
        reasons.append("suite_did_not_admit")

    recorded_identity = report.get("identity")
    if current_identity is not None and current_identity.complete:
        if not isinstance(recorded_identity, dict) or recorded_identity != asdict(current_identity):
            reasons.append("identity_changed_since_the_evidence_was_taken")

    return AdmissionDecision(
        provider=provider,
        admitted=not reasons,
        reasons=sorted(set(reasons)),
        evidence_kind="behaviour_suite",
        recorded_at=recorded_at,
        identity=recorded_identity if isinstance(recorded_identity, dict) else None,
    )


# ── the identity, computed one way ────────────────────────────────────────
#
# The probe used to build it by hand and the product never built it at all,
# so a report's identity and a running Provider's identity were compared by
# two different recipes. One recipe now; the probe imports it.


def file_digest(path: Path | str) -> str:
    """sha256 of a file, or "" when there is no such file to digest."""
    target = Path(str(path or ""))
    if not target.is_file():
        return ""
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def configuration_digest(config: Any) -> str:
    """sha256 of the adapter configuration as the product serialises it."""
    dump = getattr(config, "model_dump", None)
    payload = dump() if callable(dump) else dict(config or {})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def resolved_executable(command: str) -> str:
    """The file a command name resolves to, on this machine, or ""."""
    clean = str(command or "").strip()
    if not clean:
        return ""
    if Path(clean).is_file():
        return str(Path(clean))
    return shutil.which(clean) or ""


def evidence_identity(adapter: Any, *, version: str) -> EvidenceIdentity:
    """The subject an admission decision is about, from a live adapter.

    ``version`` is supplied rather than discovered: learning it means running
    the binary, which the product does not do inside a fence. Pass "unknown"
    and the identity is honestly incomplete, which the decision refuses by
    name; the probe, which does run the binary, passes what it saw.
    """
    config = getattr(adapter, "config", None)
    command = str(getattr(config, "command", "") or "")
    return EvidenceIdentity(
        provider=str(getattr(adapter, "agent_type", "") or ""),
        provider_version=str(version or "unknown"),
        implementation_digest=file_digest(resolved_executable(command)),
        platform=platform.platform(),
        configuration_digest=configuration_digest(config) if config is not None else "",
    )


async def probe_version(command: str, *, timeout: float = 30.0) -> str:
    """`<command> --version`, first line, or "unknown (<why>)". Never guesses."""
    try:
        proc = await asyncio.create_subprocess_exec(
            command, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", "replace").strip().splitlines()[0][:80] if out else "unknown"
    except Exception as exc:  # noqa: BLE001 - the caller wants the reason, not a raise
        return f"unknown ({type(exc).__name__})"


def decide_from_home(home: Path, *, provider: str, current_identity: EvidenceIdentity | None) -> AdmissionDecision:
    """The same decision, reading the evidence this machine has on file."""
    return decide(
        provider=provider,
        current_identity=current_identity,
        stored=stored_suite_report(Path(home), provider),
    )
