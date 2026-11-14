"""Deterministic evaluation contracts for supervised RSI candidates.

The model may propose a hypothesis, but it cannot move the goalposts after a
candidate exists and it cannot grade itself with prose.  This module freezes
the exact workflow-asset identity and numeric measures before a baseline and a
candidate are compared.  It performs no activation or file replacement.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class RSIEvaluationError(ValueError):
    pass


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SPEC_KEYS = frozenset({"asset", "hypothesis", "metrics", "min_samples"})
_LEGACY_ASSET_KEYS = frozenset({"workflow_id", "version", "candidate_fingerprint", "manifest_sha256"})
_GENERIC_ASSET_KEYS = frozenset({"asset_kind", "asset_id", "version", "candidate_fingerprint", "manifest_sha256"})
_ASSET_KINDS = frozenset({"workflow", "process", "connector", "skill"})
_METRIC_KEYS = frozenset({"name", "direction", "min_delta", "lower_bound", "upper_bound"})
_RESULT_KEYS = frozenset({"spec_fingerprint", "run_id", "sample_count", "evidence_sha256", "metrics"})


@dataclass(frozen=True)
class FrozenEvaluationSpec:
    payload: dict[str, Any]
    fingerprint: str

    @property
    def asset_fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.payload["asset"])).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {"schema_version": 1, "spec_fingerprint": self.fingerprint, **self.payload}


def freeze_evaluation_spec(payload: dict[str, Any]) -> FrozenEvaluationSpec:
    """Normalize and fingerprint the goal before either side is scored."""

    if not isinstance(payload, dict) or set(payload) != _SPEC_KEYS:
        raise RSIEvaluationError("evaluation spec fields differ from the supported contract")
    raw_asset = payload.get("asset")
    if not isinstance(raw_asset, dict) or set(raw_asset) not in {
        _LEGACY_ASSET_KEYS, _GENERIC_ASSET_KEYS,
    }:
        raise RSIEvaluationError("evaluation asset identity is incomplete")
    if set(raw_asset) == _LEGACY_ASSET_KEYS:
        # Preserve the exact v1 shape and therefore its historical
        # fingerprint.  Existing Skill candidates also used this wire shape;
        # accepting it remains a compatibility promise, not a recommendation.
        asset = {
            "workflow_id": _text(raw_asset.get("workflow_id"), "workflow_id", 128),
            "version": _text(raw_asset.get("version"), "version", 64),
            "candidate_fingerprint": _sha(raw_asset.get("candidate_fingerprint"), "candidate_fingerprint"),
            "manifest_sha256": _sha(raw_asset.get("manifest_sha256"), "manifest_sha256"),
        }
    else:
        asset_kind = str(raw_asset.get("asset_kind", "") or "").strip().lower()
        if asset_kind not in _ASSET_KINDS:
            raise RSIEvaluationError("asset_kind must be workflow, process, connector or skill")
        asset = {
            "asset_kind": asset_kind,
            "asset_id": _text(raw_asset.get("asset_id"), "asset_id", 128),
            "version": _text(raw_asset.get("version"), "version", 64),
            "candidate_fingerprint": _sha(raw_asset.get("candidate_fingerprint"), "candidate_fingerprint"),
            "manifest_sha256": _sha(raw_asset.get("manifest_sha256"), "manifest_sha256"),
        }
    hypothesis = _text(payload.get("hypothesis"), "hypothesis", 1000)
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, list) or not 1 <= len(raw_metrics) <= 12:
        raise RSIEvaluationError("metrics must contain 1-12 measures")
    metrics: list[dict[str, str]] = []
    names: set[str] = set()
    for raw in raw_metrics:
        if not isinstance(raw, dict) or set(raw) != _METRIC_KEYS:
            raise RSIEvaluationError("metric fields differ from the supported contract")
        name = _text(raw.get("name"), "metric name", 64)
        if not _NAME.fullmatch(name) or name in names:
            raise RSIEvaluationError("metric names must be unique snake_case identifiers")
        names.add(name)
        direction = str(raw.get("direction", "") or "").strip().lower()
        if direction not in {"higher", "lower"}:
            raise RSIEvaluationError("metric direction must be higher or lower")
        delta = _decimal(raw.get("min_delta"), f"{name} min_delta")
        lower = _decimal(raw.get("lower_bound"), f"{name} lower_bound")
        upper = _decimal(raw.get("upper_bound"), f"{name} upper_bound")
        if delta <= 0:
            raise RSIEvaluationError("metric min_delta must be greater than zero")
        if lower >= upper:
            raise RSIEvaluationError("metric lower_bound must be below upper_bound")
        metrics.append({
            "name": name,
            "direction": direction,
            "min_delta": _decimal_text(delta),
            "lower_bound": _decimal_text(lower),
            "upper_bound": _decimal_text(upper),
        })
    samples = payload.get("min_samples")
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 10000:
        raise RSIEvaluationError("min_samples must be an integer from 1 to 10000")
    normalized = {
        "asset": asset,
        "hypothesis": hypothesis,
        "metrics": metrics,
        "min_samples": samples,
    }
    return FrozenEvaluationSpec(normalized, hashlib.sha256(_canonical(normalized)).hexdigest())


def compare_evaluation_runs(
    spec: FrozenEvaluationSpec,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Compare two independently evidenced runs against the frozen contract."""

    base = _normalize_result(spec, baseline, "baseline")
    trial = _normalize_result(spec, candidate, "candidate")
    flags: list[str] = []
    if base["run_id"] == trial["run_id"]:
        flags.append("same_run_used_for_baseline_and_candidate")
    if base["evidence_sha256"] == trial["evidence_sha256"]:
        flags.append("same_evidence_used_for_baseline_and_candidate")
    measures: list[dict[str, Any]] = []
    all_passed = True
    for metric in spec.payload["metrics"]:
        name = metric["name"]
        before = Decimal(base["metrics"][name])
        after = Decimal(trial["metrics"][name])
        delta = after - before if metric["direction"] == "higher" else before - after
        passed = delta >= Decimal(metric["min_delta"])
        all_passed = all_passed and passed
        measures.append({
            "name": name,
            "direction": metric["direction"],
            "baseline": _decimal_text(before),
            "candidate": _decimal_text(after),
            "improvement": _decimal_text(delta),
            "required_improvement": metric["min_delta"],
            "passed": passed,
        })
    verdict = "passed" if all_passed and not flags else "failed"
    report_body = {
        "schema_version": 1,
        "spec_fingerprint": spec.fingerprint,
        "asset_fingerprint": spec.asset_fingerprint,
        "baseline_run_id": base["run_id"],
        "candidate_run_id": trial["run_id"],
        "baseline_evidence_sha256": base["evidence_sha256"],
        "candidate_evidence_sha256": trial["evidence_sha256"],
        "verdict": verdict,
        "reward_hack_flags": flags,
        "metrics": measures,
    }
    return {**report_body, "report_sha256": hashlib.sha256(_canonical(report_body)).hexdigest()}


def _normalize_result(spec: FrozenEvaluationSpec, payload: dict[str, Any], side: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _RESULT_KEYS:
        raise RSIEvaluationError(f"{side} result fields differ from the supported contract")
    if str(payload.get("spec_fingerprint", "") or "").strip().lower() != spec.fingerprint:
        raise RSIEvaluationError(f"{side} result does not use the frozen evaluation spec")
    run_id = _text(payload.get("run_id"), f"{side} run_id", 192)
    evidence_sha = _sha(payload.get("evidence_sha256"), f"{side} evidence_sha256")
    samples = payload.get("sample_count")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < spec.payload["min_samples"]:
        raise RSIEvaluationError(f"{side} result has too few samples")
    raw_metrics = payload.get("metrics")
    expected = {item["name"]: item for item in spec.payload["metrics"]}
    if not isinstance(raw_metrics, dict) or set(raw_metrics) != set(expected):
        raise RSIEvaluationError(f"{side} result metric set changed after the spec was frozen")
    metrics: dict[str, str] = {}
    for name, metric in expected.items():
        value = _decimal(raw_metrics[name], f"{side} metric {name}")
        if value < Decimal(metric["lower_bound"]) or value > Decimal(metric["upper_bound"]):
            raise RSIEvaluationError(f"{side} metric {name} is outside its frozen bounds")
        metrics[name] = _decimal_text(value)
    return {
        "spec_fingerprint": spec.fingerprint,
        "run_id": run_id,
        "sample_count": samples,
        "evidence_sha256": evidence_sha,
        "metrics": metrics,
    }


def _text(value: Any, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise RSIEvaluationError(f"{name} is required and must be at most {limit} characters")
    return text


def _sha(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256.fullmatch(text):
        raise RSIEvaluationError(f"{name} must be sha256")
    return text


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise RSIEvaluationError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RSIEvaluationError(f"{name} must be a finite number") from None
    if not number.is_finite() or not math.isfinite(float(number)):
        raise RSIEvaluationError(f"{name} must be a finite number")
    return number


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
