"""Bounded read-only recovery of original lifecycle IDs from the original ledger.

No initializer, worker, publication dispatch, migration or customer DB is used.
An empty result is never evidence that an uncertain browser request had no effect.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .employee_publication_authority import PublicationAuthorityLedger

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TRIAL = re.compile(r"et-[a-f0-9]{24}\Z")
_STATES = {"prepared", "running", "awaiting_review", "approved", "rejected", "failed", "cancelled", "outcome_unknown", "abandoned"}
_BLOCKING = {"prepared", "running", "outcome_unknown"}
_LIMIT = 8


def _fail():
    raise AIEmployeeContractError("storage_record_invalid", "历史记录暂不能核实；未启动测试、发布或替换原请求。")


def _valid(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def validate_history_view(raw, *, project_id, package_id):
    """Strict projection also used by the existing wire dispatcher."""
    fields = {"project_id", "package_id", "revision", "trials", "publications", "has_more", "blocked_elsewhere",
              "effects_produced", "allowed_actions"}
    if (type(raw) is not dict or set(raw) != fields or raw["project_id"] != project_id
            or raw["package_id"] != package_id or not _valid(project_id, _ID) or not _valid(package_id, _PACKAGE)
            or not _valid(raw["revision"], _SHA) or type(raw["has_more"]) is not bool
            or type(raw["blocked_elsewhere"]) is not bool or raw["effects_produced"] is not False
            or raw["allowed_actions"] != []):
        _fail()
    seen = set()
    for collection, extra in (("trials", {"trial_id", "state", "observed_at"}),
                              ("publications", {"state", "observed_at"})):
        rows = raw[collection]
        if type(rows) is not list or len(rows) > _LIMIT:
            _fail()
        for row in rows:
            expected = {"request_id", "candidate_revision", "fingerprint"} | extra
            if collection == "trials" and type(row) is dict and "instance_selection" in row:
                from .employee_qualification import validate_selection
                validate_selection(row["instance_selection"])
                expected.add("instance_selection")
            if (type(row) is not dict or set(row) != expected or not _valid(row["request_id"], _ID)
                    or not _valid(row["candidate_revision"], _SHA) or not _valid(row["fingerprint"], _SHA)
                    or type(row["observed_at"]) is not str or not 1 <= len(row["observed_at"]) <= 128
                    or any(ord(char) < 32 for char in row["observed_at"])
                    or (collection, row["request_id"]) in seen):
                _fail()
            if collection == "trials":
                if not _valid(row["trial_id"], _TRIAL) or row["state"] not in _STATES:
                    _fail()
            elif row["state"] not in {"prepared", "unknown", "committed"}:
                _fail()
            seen.add((collection, row["request_id"]))
    body = {key: value for key, value in raw.items() if key != "revision"}
    if hashlib.sha256(canonical_json(body)).hexdigest() != raw["revision"] or len(canonical_json(raw)) > 32 * 1024:
        _fail()
    return deepcopy(raw)


def read_lifecycle_history(service, *, project_id, actor, package_id):
    """Current owner + exact actor/project; only opaque IDs and states are exposed."""
    if not _valid(project_id, _ID) or not _valid(package_id, _PACKAGE):
        _fail()
    service._check(project_id, actor)
    result = {"project_id": project_id, "package_id": package_id, "trials": [], "publications": [],
              "has_more": False, "blocked_elsewhere": False, "effects_produced": False, "allowed_actions": []}
    state = service.state
    if state.exists():
        # Original connection is mode=ro/query_only, schema-checked, rejects hot
        # WAL/journals and never creates a missing ledger.
        with state.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM employee_trials").fetchone()[0]
            if type(count) is not int or not 0 <= count <= 200:
                _fail()
            rows = conn.execute("""SELECT trial_id,fingerprint,request_id,state,observed_at,candidate_json
                FROM employee_trials WHERE project_id=? AND package_id=? AND actor=?
                ORDER BY CASE WHEN state IN ('prepared','running','outcome_unknown') THEN 0 ELSE 1 END,rowid DESC LIMIT 9""",
                (project_id, package_id, actor)).fetchall()
            result["has_more"] = len(rows) > _LIMIT
            for trial, fingerprint, request, status, observed, frozen_raw in rows[:_LIMIT]:
                if type(frozen_raw) not in (bytes, str) or len(frozen_raw) > 2 * 1024 * 1024:
                    _fail()
                try:
                    frozen = json.loads(frozen_raw)
                    revision = frozen["revision"]
                    manifest = frozen["manifest"]
                    if (manifest["package_id"] != package_id or manifest["package_fingerprint"] != fingerprint):
                        _fail()
                except (ValueError, KeyError, TypeError):
                    _fail()
                entry = {"trial_id": trial, "fingerprint": fingerprint, "request_id": request,
                         "state": status, "candidate_revision": revision, "observed_at": observed}
                if frozen.get("instance_selection") is not None:
                    from .employee_qualification import validate_selection
                    entry["instance_selection"] = validate_selection(frozen["instance_selection"])
                result["trials"].append(entry)
            # Active attempts outside the displayed owner/package are a blocker,
            # not an invitation to launch a replacement. No other actor's IDs,
            # model inputs, outputs, personnel or budgets are returned.
            result["blocked_elsewhere"] = bool(conn.execute("""SELECT 1 FROM employee_trials
                WHERE state IN ('prepared','running','outcome_unknown')
                AND NOT (project_id=? AND package_id=? AND actor=?) LIMIT 1""",
                (project_id, package_id, actor)).fetchone())
            # Publication plans are validated by the original authority codec;
            # no mutable publication index or directory is treated as a receipt.
            publication_count = conn.execute("SELECT COUNT(*) FROM ae_publication_requests").fetchone()[0]
            if type(publication_count) is not int or not 0 <= publication_count <= 10000:
                _fail()
            # JSON1 only selects identities; every returned plan/lease is then
            # rebuilt and hashed by the original authority codec. Malformed
            # ledger JSON or unavailable JSON1 is a read refusal, not fallback.
            keys = conn.execute("""SELECT request_key,state FROM ae_publication_requests WHERE package_id=?
                AND json_extract(plan_json,'$.request.project_id')=?
                AND json_extract(plan_json,'$.request.actor')=?
                AND json_extract(plan_json,'$.request.action')='employee_package_publish'
                ORDER BY CASE WHEN state!='committed' THEN 0 ELSE 1 END,rowid DESC LIMIT 9""",
                (package_id, project_id, actor)).fetchall()
            owned = []
            result["has_more"] = result["has_more"] or len(keys) > _LIMIT
            for key, _status in keys[:_LIMIT]:
                packed = PublicationAuthorityLedger._load(conn, key)
                if packed is None:
                    _fail()
                plan, _lease, status = PublicationAuthorityLedger._unpack(key, packed)
                identity = plan.request
                if (identity.action != "employee_package_publish" or identity.project_id != project_id
                        or identity.actor != actor or plan.package_id != package_id):
                    _fail()
                owned.append({"request_id": identity.request_id, "candidate_revision": plan.source_draft_revision,
                              "fingerprint": plan.fingerprint, "state": status.state, "observed_at": plan.at})
            result["publications"] = owned
            if conn.execute("""SELECT 1 FROM ae_publication_requests WHERE state!='committed'
                AND NOT (package_id=? AND json_extract(plan_json,'$.request.project_id')=?
                         AND json_extract(plan_json,'$.request.actor')=?) LIMIT 1""",
                (package_id, project_id, actor)).fetchone():
                result["blocked_elsewhere"] = True
            if ((len(rows) > _LIMIT and rows[_LIMIT][3] in _BLOCKING)
                    or (len(keys) > _LIMIT and keys[_LIMIT][1] != "committed")):
                # More active work than this bounded projection can represent
                # must not become permission to start another paid attempt.
                result["blocked_elsewhere"] = True
    service._check(project_id, actor)
    result["revision"] = hashlib.sha256(canonical_json(result)).hexdigest()
    return validate_history_view(result, project_id=project_id, package_id=package_id)
