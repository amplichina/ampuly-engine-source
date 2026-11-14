"""Technical v1 consistency math, not tool observation or source authority.

Already-supplied JSON/bytes only: no I/O, registry, dependency lookup, clock,
runtime record, sampling, permission, final-row generation or persistence.
Computed hashes do not prove schema validity, loaded-source identity or a
complete implementation/dependency inventory. Typed values are declarations,
never trusted bindings. Remote revision comparisons are unavailable in v1.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ampuly.plugins.office_ui.ai_employee_contract import (
    MAX_FILES,
    MAX_FILE_BYTES,
    _portable_path,
    canonical_json,
)


SCHEMA_PROJECTION = "tool_schema_v1"
IMPLEMENTATION_PROJECTION = "tool_local_implementation_v1"
RECORD_PROJECTION = "tool_record_v1"
ABSENT = object()
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_HASH_KINDS = {
    "schema_fingerprint": SCHEMA_PROJECTION,
    "implementation_fingerprint": IMPLEMENTATION_PROJECTION,
    "record_fingerprint": RECORD_PROJECTION,
}
_RECORD_REQUIRED = {
    "tool", "impl", "schema_fingerprint", "implementation_fingerprint",
    "remote_revision", "verifiable",
}
_RECORD_OPTIONAL = {"action", "dependency_versions"}


@dataclass(frozen=True)
class FingerprintResult:
    """Mathematical digest or safe unknown diagnostic; not a final tool row."""

    fingerprint: str | None
    projection: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TypedFingerprintValue:
    """Explicit comparison declaration, NOT verified host/instance identity.

    Scope is opaque matching JSON supplied by the caller. Its contents do not
    establish project, revision, loaded-source authority or authorization.
    Missing fields retain absence; there is no inferred/default scope.
    """

    kind: str = field(repr=False)
    value: Any = field(default=ABSENT, repr=False)
    scope: Any = field(default=ABSENT, repr=False)
    projection: Any = field(default=ABSENT, repr=False)
    dependency_name: Any = field(default=ABSENT, repr=False)


@dataclass(frozen=True)
class FingerprintComparison:
    comparison: Literal["unchanged", "drifted", "unknown"]
    reason_codes: tuple[str, ...] = ()


def _unknown(projection: str, reason: str) -> FingerprintResult:
    return FingerprintResult(None, projection, (reason,))


def _sha(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _nonempty_text(value: Any) -> bool:
    return type(value) is str and bool(value)


def _is_sha(value: Any) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _dependencies(value: Any) -> dict[str, str]:
    if type(value) is not dict:
        raise ValueError
    # Shared strict caps apply before walking versions. JSON round-trip gives
    # this computation a detached value; it does not prove a complete source.
    detached = json.loads(canonical_json(value))
    if any(not _nonempty_text(name) or not _nonempty_text(version) for name, version in detached.items()):
        raise ValueError
    return detached


def _has_reference(value: Any) -> bool:
    pending = [value]
    while pending:
        node = pending.pop()
        if type(node) is dict:
            if any(key in node for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                return True
            pending.extend(node.values())
        elif type(node) is list:
            pending.extend(node)
    return False


def _bounded_operand(value: TypedFingerprintValue) -> None:
    declared = {"kind": value.kind}
    for name in ("value", "scope", "projection", "dependency_name"):
        item = getattr(value, name)
        if item is not ABSENT:
            declared[name] = item
    canonical_json(declared)


def schema_fingerprint(
    parameters_schema: Any = ABSENT, return_schema: Any = ABSENT,
) -> FingerprintResult:
    """Hash explicit schema structure; do not validate semantics/resolve refs."""
    try:
        if parameters_schema is ABSENT or parameters_schema is None:
            return _unknown(SCHEMA_PROJECTION, "schema_unavailable")
        if return_schema is ABSENT or return_schema is None:
            return _unknown(SCHEMA_PROJECTION, "return_schema_unavailable")
        if type(parameters_schema) not in (dict, bool) or type(return_schema) not in (dict, bool):
            raise ValueError
        encoded = canonical_json({
            "projection": SCHEMA_PROJECTION,
            "parameters_schema": parameters_schema,
            "return_schema": return_schema,
        })
        # Conservative, bounded diagnostic only. No local/remote $ref resolver
        # or source_verified flag is added to this purely mathematical leaf.
        if _has_reference(json.loads(encoded)):
            return _unknown(SCHEMA_PROJECTION, "schema_source_incomplete")
        return FingerprintResult(_sha(encoded), SCHEMA_PROJECTION)
    except Exception:
        return _unknown(SCHEMA_PROJECTION, "observation_failed")


def local_implementation_fingerprint(
    files: Any = ABSENT, dependency_versions: Any = ABSENT,
) -> FingerprintResult:
    """Hash explicit ref→original-bytes inventory, not loaded implementation.

    Portable refs are labels only. Nothing is opened. Aggregate bytes are
    capped at 20 MiB/500 files; JSON metadata keeps canonical_json's own caps.
    No 2 MiB per-file ceiling is invented for already-supplied raw bytes.
    """
    try:
        if files is ABSENT or files is None:
            return _unknown(IMPLEMENTATION_PROJECTION, "implementation_unobserved")
        if type(files) is not dict or not files:
            return _unknown(IMPLEMENTATION_PROJECTION, "implementation_scope_incomplete")
        if len(files) > MAX_FILES:
            raise ValueError
        if dependency_versions is ABSENT or dependency_versions is None:
            return _unknown(IMPLEMENTATION_PROJECTION, "dependency_inventory_unknown")
        versions = _dependencies(dependency_versions)
        total = 0
        projected = []
        portable_names: set[str] = set()
        for ref, content in files.items():
            checked = _portable_path(ref)
            # Existing portable filenames target Windows as well as POSIX.
            # Case collisions cannot be represented as distinct local files.
            folded = checked.casefold()
            if folded in portable_names or type(content) is not bytes:
                raise ValueError
            portable_names.add(folded)
            total += len(content)
            if total > MAX_FILE_BYTES:
                raise ValueError
            projected.append({"ref": checked, "size": len(content), "sha256": _sha(content)})
        projected.sort(key=lambda item: item["ref"])
        encoded = canonical_json({
            "projection": IMPLEMENTATION_PROJECTION,
            "files": projected,
            "dependency_versions": versions,
        })
        return FingerprintResult(_sha(encoded), IMPLEMENTATION_PROJECTION)
    except Exception:
        return _unknown(IMPLEMENTATION_PROJECTION, "observation_failed")


def tool_record_fingerprint(projection_fields: Any = ABSENT) -> FingerprintResult:
    """Hash only supplied approved fields; never produce/validate a final row.

    In particular the supplied verifiable boolean is only a JSON literal,
    not accepted source evidence and is never returned as an authority marker.
    Timestamp/drift/proof fields are not inputs to this projection-only API.
    """
    try:
        if type(projection_fields) is not dict:
            raise ValueError
        encoded = canonical_json(projection_fields)
        projected = json.loads(encoded)
        if not _RECORD_REQUIRED.issubset(projected) or set(projected) - _RECORD_REQUIRED - _RECORD_OPTIONAL:
            raise ValueError
        if not _nonempty_text(projected["tool"]) or projected["impl"] not in {"builtin", "connector", "mcp"}:
            raise ValueError
        if type(projected["impl"]) is not str or not _is_sha(projected["schema_fingerprint"]):
            raise ValueError
        implementation = projected["implementation_fingerprint"]
        remote = projected["remote_revision"]
        if implementation is not None and not _is_sha(implementation):
            raise ValueError
        if remote is not None and not _nonempty_text(remote):
            raise ValueError
        if type(projected["verifiable"]) is not bool:
            raise ValueError
        if "action" in projected and not _nonempty_text(projected["action"]):
            raise ValueError
        if "dependency_versions" in projected:
            _dependencies(projected["dependency_versions"])
        # No projection discriminator is inserted into tool_record_v1 bytes.
        return FingerprintResult(_sha(encoded), RECORD_PROJECTION)
    except Exception:
        return _unknown(RECORD_PROJECTION, "observation_failed")


def compare_typed_baseline(
    baseline: Any = ABSENT, current: Any = ABSENT,
) -> FingerprintComparison:
    """Compare declared values/context mathematically, never authenticate them."""
    try:
        if baseline is ABSENT or baseline is None:
            return FingerprintComparison("unknown", ("baseline_missing",))
        if type(baseline) is not TypedFingerprintValue or type(current) is not TypedFingerprintValue:
            return FingerprintComparison("unknown", ("baseline_untyped",))
        if type(baseline.kind) is not str or type(current.kind) is not str or baseline.kind != current.kind:
            return FingerprintComparison("unknown", ("baseline_kind_mismatch",))
        kind = baseline.kind
        if kind == "remote_revision":
            return FingerprintComparison("unknown", ("remote_unverifiable",))
        if kind not in _HASH_KINDS and kind != "dependency_version":
            return FingerprintComparison("unknown", ("baseline_untyped",))
        _bounded_operand(baseline)
        _bounded_operand(current)
        if any(type(value.scope) is not dict or not value.scope for value in (baseline, current)):
            return FingerprintComparison("unknown", ("source_binding_unverified",))
        if canonical_json(baseline.scope) != canonical_json(current.scope):
            return FingerprintComparison("unknown", ("source_binding_unverified",))
        if kind in _HASH_KINDS:
            expected = _HASH_KINDS[kind]
            if any(type(value.projection) is not str or value.projection != expected or value.dependency_name is not ABSENT for value in (baseline, current)):
                return FingerprintComparison("unknown", ("baseline_kind_mismatch",))
            if baseline.value is ABSENT or baseline.value is None:
                return FingerprintComparison("unknown", ("baseline_missing",))
            if current.value is ABSENT or current.value is None:
                reason = "schema_unavailable" if kind == "schema_fingerprint" else "implementation_unobserved"
                return FingerprintComparison("unknown", (reason,))
            if not _is_sha(baseline.value) or not _is_sha(current.value):
                raise ValueError
        else:
            if any(value.projection is not ABSENT or not _nonempty_text(value.dependency_name) for value in (baseline, current)):
                return FingerprintComparison("unknown", ("baseline_kind_mismatch",))
            if baseline.dependency_name != current.dependency_name:
                return FingerprintComparison("unknown", ("baseline_kind_mismatch",))
            if not _nonempty_text(baseline.value) or not _nonempty_text(current.value):
                return FingerprintComparison("unknown", ("dependency_inventory_unknown",))
        return FingerprintComparison("unchanged" if baseline.value == current.value else "drifted")
    except Exception:
        return FingerprintComparison("unknown", ("observation_failed",))
