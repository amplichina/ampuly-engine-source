"""Turn pre-bound Harness sessions into deterministic RSI run evidence.

One session is one sample and must be bound before its first turn.  The
extractor reads only durable registry facts; callers cannot submit metric
values, choose a terminal event after seeing several turns, or call an
owner-settled unknown outcome a success.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from ampuly.plugins.office_ui.rsi_evaluation import FrozenEvaluationSpec, RSIEvaluationError


_SUPPORTED_METRICS = {
    "completion_rate": ("higher", Decimal("0"), Decimal("1")),
    "artifact_rate": ("higher", Decimal("0"), Decimal("1")),
    "failure_rate": ("lower", Decimal("0"), Decimal("1")),
    "human_pause_rate": ("lower", Decimal("0"), Decimal("1")),
}
_TERMINAL = frozenset({"turn_finished", "turn_failed", "turn_cancelled"})


def harness_evaluation_result(
    registry: Any,
    spec: FrozenEvaluationSpec,
    session_ids: list[str] | tuple[str, ...],
    *,
    variant: str,
    project_id: str = "",
) -> dict[str, Any]:
    """Derive one comparison side from exact, pre-bound Harness sessions."""

    side = str(variant or "").strip().lower()
    if side not in {"baseline", "candidate"}:
        raise RSIEvaluationError("evaluation variant must be baseline or candidate")
    identifiers = [str(value or "").strip() for value in session_ids]
    if not identifiers or len(identifiers) > 1000 or any(not value for value in identifiers):
        raise RSIEvaluationError("evaluation needs 1-1000 named Harness sessions")
    if len(set(identifiers)) != len(identifiers):
        raise RSIEvaluationError("evaluation session ids must be unique")
    if len(identifiers) < int(spec.payload["min_samples"]):
        raise RSIEvaluationError("evaluation has too few pre-bound Harness sessions")

    metric_specs = {str(item["name"]): item for item in spec.payload["metrics"]}
    unsupported = sorted(set(metric_specs) - set(_SUPPORTED_METRICS))
    if unsupported:
        raise RSIEvaluationError(
            "Harness evidence cannot derive metric(s): " + ", ".join(unsupported)
        )
    for name, metric in metric_specs.items():
        direction, floor, ceiling = _SUPPORTED_METRICS[name]
        if metric["direction"] != direction:
            raise RSIEvaluationError(f"{name} direction differs from the Harness evidence contract")
        if Decimal(metric["lower_bound"]) < floor or Decimal(metric["upper_bound"]) > ceiling:
            raise RSIEvaluationError(f"{name} bounds exceed the Harness evidence contract")

    receipts = [
        _session_receipt(registry, spec, session_id, side, project_id)
        for session_id in sorted(identifiers)
    ]
    count = Decimal(len(receipts))
    totals = {
        "completion_rate": sum(Decimal(receipt["completed"]) for receipt in receipts) / count,
        "artifact_rate": sum(Decimal(receipt["has_artifacts"]) for receipt in receipts) / count,
        "failure_rate": sum(Decimal(receipt["failed"]) for receipt in receipts) / count,
        "human_pause_rate": sum(Decimal(receipt["human_pause"]) for receipt in receipts) / count,
    }
    evidence = {
        "schema_version": 1,
        "spec_fingerprint": spec.fingerprint,
        "asset_fingerprint": spec.asset_fingerprint,
        "variant": side,
        "sessions": receipts,
    }
    evidence_bytes = _canonical(evidence)
    evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
    return {
        "spec_fingerprint": spec.fingerprint,
        "run_id": f"harness-eval-{side}-{evidence_sha[:16]}",
        "sample_count": len(receipts),
        "evidence_sha256": evidence_sha,
        "metrics": {name: _decimal_text(totals[name]) for name in metric_specs},
    }


def _session_receipt(
    registry: Any, spec: FrozenEvaluationSpec, session_id: str, side: str,
    project_id: str,
) -> dict[str, Any]:
    record = registry.session(session_id)
    if record is None:
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} does not exist")
    if project_id and str(record.project_id or "") != str(project_id):
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} belongs to another project")
    events = list(registry.events_after(session_id, 0, limit=10000))
    # A full page might be truncated; ambiguity is not evaluation evidence.
    if len(events) == 10000:
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} exceeds the evidence page")
    bindings = [event for event in events if event.event_type == "evaluation_bound"]
    starts = [event for event in events if event.event_type == "turn_started"]
    terminals = [event for event in events if event.event_type in _TERMINAL]
    if len(bindings) != 1 or len(starts) != 1 or len(terminals) != 1:
        raise RSIEvaluationError(
            f"Harness evaluation session {session_id!r} must have one binding, one turn and one terminal event"
        )
    binding, started, terminal = bindings[0], starts[0], terminals[0]
    expected_binding = {
        "spec_fingerprint": spec.fingerprint,
        "asset_fingerprint": spec.asset_fingerprint,
        "variant": side,
    }
    if binding.data != expected_binding:
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} binding differs from the frozen spec")
    if not binding.seq < started.seq < terminal.seq or started.turn_id != terminal.turn_id:
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} event chain is inconsistent")
    if record.state == "lost" or registry.pending_turn_id(session_id):
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} has an unknown outcome")
    data = dict(terminal.data or {})
    if terminal.event_type == "turn_failed" and data.get("outcome_known") is False:
        raise RSIEvaluationError(f"Harness evaluation session {session_id!r} was settled without a known outcome")
    status = str(data.get("status", "") or "").strip().lower()
    human_pause = terminal.event_type == "turn_finished" and status in {
        "awaiting_human", "awaiting-human", "waiting_for_human", "waiting-for-human",
    }
    failed = terminal.event_type in {"turn_failed", "turn_cancelled"} or status in {
        "failed", "cancelled", "canceled", "interrupted",
    }
    # ``turn_finished`` is the durable success fact across Providers.  Some
    # transports additionally record ``done``/``completed`` while others have
    # no status field; requiring one spelling would turn real successes into
    # failures.  A human pause is terminal for the turn, but not completion.
    completed = terminal.event_type == "turn_finished" and not human_pause and not failed
    artifacts = data.get("artifacts")
    has_artifacts = (
        bool(artifacts) if isinstance(artifacts, (dict, list, tuple, set, str))
        else artifacts > 0 if isinstance(artifacts, (int, float)) and not isinstance(artifacts, bool)
        else False
    )
    return {
        "session_id": session_id,
        "provider": str(record.provider or ""),
        "project_id": str(record.project_id or ""),
        "run_id": str(record.run_id or ""),
        "turn_id": terminal.turn_id,
        "terminal_seq": terminal.seq,
        "terminal_type": terminal.event_type,
        "status": status,
        "completed": int(completed),
        "failed": int(failed),
        "human_pause": int(human_pause),
        "has_artifacts": int(has_artifacts),
    }


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"", "-0"} else rendered


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
