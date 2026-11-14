"""Deterministic, scoped export of an already-sanitized Computer Use audit trail."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ampuly.computer_use.audit import COMBINATION_KEYS, DELEGATION_KEYS, SESSION_KEYS, _safe_page
from ampuly.computer_use.download_safety import prepare_confined_directory

SCHEMA = "superones.computer-use-audit.v1"
_TIMELINE_KEYS = frozenset({
    "source", "type", "at", "kind", "risk", "outcome", "reason", "attempt_id", "action_id", "sequence",
    "state", "action", "actor", "effect_id", "checkpoint_id", "function", "model", "decided_on", "observed",
    "stage", "durable", "page", "expect", "state_now", "authorized_by", "from", "to", "status", "domains",
    "vision", "downloads", "lease_minutes", "by", "name",
    # 4.7-A17: who suggested a decision, who checked it, who has it now (vocabulary ids, never page words).
    "suggested_by", "reviewed_by", "fallback_to",
})
_SUMMARY_KEYS = frozenset({
    "admitted", "refused", "receipts", "in_flight", "proposals", "effects", "open_questions", "lease_changes",
    "console", "artifacts",
})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _single_segment(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if (
        not text or text in {".", ".."} or text.rstrip(" .") != text
        or any(mark in text for mark in ("/", "\\", ":", "\x00"))
    ):
        raise ValueError(f"invalid {label}")
    return text


def export_audit_trail(trail: dict[str, Any], *, order_root: Path, order_id: str = "") -> dict[str, Any]:
    """Write one immutable evidence bundle below the already-authorized order root.

    `audit_trail()` owns field redaction.  This layer validates identity,
    creates a reproducible digest, and publishes without replacing an earlier
    export.  It never accepts a caller-chosen output path or filename.
    ``order_id`` is the business order the session worked for (data phase 1):
    the bundle then goes under that order's folder and names it, keeping the
    session's own anchor task as ``task_id``; without one, the anchor task's
    folder, as before (and the same digest as before).
    """
    task_id = _single_segment(trail.get("task_id"), "task id")
    binding_key = _single_segment(trail.get("binding_key"), "binding key")
    business_order = _single_segment(order_id, "order id") if str(order_id or "").strip() else ""
    root = Path(order_root)
    if root.name != (business_order or task_id):
        raise ValueError("audit task does not match order root")
    audit_dir = root / "audit"
    prepare_confined_directory(audit_dir, root)

    delegation = dict(trail.get("delegation", {}) or {})
    session = dict(trail.get("session", {}) or {})
    combination = dict(trail.get("combination", {}) or {})
    summary = dict(trail.get("summary", {}) or {})
    timeline: list[dict[str, Any]] = []
    for raw in list(trail.get("timeline", []) or []):
        if not isinstance(raw, dict):
            continue
        row = {key: raw[key] for key in _TIMELINE_KEYS if key in raw}
        if "page" in row:
            row["page"] = _safe_page(row["page"])
        if "name" in row:
            row["name"] = Path(str(row["name"] or "")).name[:120]
        timeline.append(row)
    payload = {
        "schema": SCHEMA,
        "binding_key": binding_key,
        "task_id": task_id,
        "delegation": {key: delegation[key] for key in DELEGATION_KEYS if key in delegation},
        "session": {key: session[key] for key in SESSION_KEYS if key in session},
        "combination": {key: combination[key] for key in COMBINATION_KEYS if key in combination},
        "summary": {key: summary[key] for key in _SUMMARY_KEYS if key in summary},
        "timeline": timeline,
    }
    if business_order:
        payload["order_id"] = business_order
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    bundle = {**payload, "integrity": {"algorithm": "sha256", "content_sha256": digest}}
    encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    target = audit_dir / f"browser-audit-{digest[:12]}.json"
    if target.exists():
        if target.is_file() and target.read_bytes() == encoded:
            return {"path": target, "sha256": digest, "created": False}
        raise FileExistsError("audit export name is already occupied")

    fd, staged_name = tempfile.mkstemp(prefix=".audit-", suffix=".tmp", dir=audit_dir)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staged, target)
        except OSError:
            with staged.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1 << 20)
        return {"path": target, "sha256": digest, "created": True}
    finally:
        staged.unlink(missing_ok=True)
