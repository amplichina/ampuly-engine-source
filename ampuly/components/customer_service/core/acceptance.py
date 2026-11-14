"""Bounded service acceptance facts; no model transport, sending or admission grant.

Only an authenticated host may call this module. ``record_case`` receives the
original ConversationService result and Engine observation, never client flags.
The host owns provider/budget enforcement and authenticates actor identifiers;
these local hashes do not prove a model ran or authenticate a human by themselves.
The current demo knowledge and synthetic tests prove engineering contracts only.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from .admission import AcceptanceEvidence, AdmissionRequest
from .autonomy import CurrentBasis, _bounded_json, _candidate, evaluate_autonomy
from .contracts import Context, bounded_text, canonical_json
from .conversation import _turns
from .understanding import INTENT_CATALOG


MAX_CASES = 32
MAX_SUITES = 8
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_BYTES = 256 * 1024
MAX_DATABASE_BYTES = 32 * 1024 * 1024
_VERSION = 1
_APPLICATION = 0x43534143
_HASH = re.compile(r"[a-f0-9]{64}")
_RISK_REASONS = frozenset({"autonomy_customer_requested_human", "autonomy_protected_action",
    "autonomy_safety_risk", "autonomy_complaint", "autonomy_repeated_failure",
    "autonomy_customer_escalation", "autonomy_knowledge_conflict"})


class AcceptanceError(ValueError):
    """Stable codes without customer, provider or credential contents."""


def _id(value):
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise AcceptanceError("acceptance_identifier_invalid")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AcceptanceError("acceptance_identifier_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise AcceptanceError("acceptance_identifier_invalid") from None
    return value


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise AcceptanceError("acceptance_hash_invalid")
    return value


def _integer(value, maximum=253402300799):
    if type(value) is not int or not 0 <= value <= maximum:
        raise AcceptanceError("acceptance_integer_invalid")
    return value


def _strings(value, maximum=32):
    if type(value) not in (list, tuple) or len(value) > maximum:
        raise AcceptanceError("acceptance_collection_invalid")
    result = tuple(sorted(_id(item) for item in value))
    if len(set(result)) != len(result):
        raise AcceptanceError("acceptance_collection_invalid")
    return result


def _json(value, maximum):
    """Bound traversal before serialization; no custom objects or iterators."""
    nodes = 0
    characters = 0

    def visit(item, depth):
        nonlocal nodes, characters
        nodes += 1
        if nodes > 40000 or depth > 16:
            raise AcceptanceError("acceptance_json_bounds")
        kind = type(item)
        if kind is dict:
            if len(item) > 256:
                raise AcceptanceError("acceptance_json_bounds")
            for key, child in item.items():
                if type(key) is not str or len(key) > 256:
                    raise AcceptanceError("acceptance_json_bounds")
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif kind in (list, tuple):
            if len(item) > 4096:
                raise AcceptanceError("acceptance_json_bounds")
            for child in item:
                visit(child, depth + 1)
        elif kind is str:
            characters += len(item)
            if characters > maximum:
                raise AcceptanceError("acceptance_json_bounds")
        elif kind is int:
            if abs(item) > 2**63 - 1:
                raise AcceptanceError("acceptance_json_bounds")
        elif kind is float:
            if not math.isfinite(item):
                raise AcceptanceError("acceptance_json_bounds")
        elif item is not None and kind is not bool:
            raise AcceptanceError("acceptance_json_invalid")

    visit(value, 0)
    try:
        encoded = canonical_json(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AcceptanceError("acceptance_json_invalid") from None
    if len(encoded) > maximum:
        raise AcceptanceError("acceptance_json_bounds")
    return encoded.decode("utf-8")


def _digest(value):
    return sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class AcceptanceCase:
    case_id: str
    context: Context
    _turns_json: str
    expected_action: str
    expected_intents: tuple[str, ...]
    required_reason_codes: tuple[str, ...]
    required_source_ids: tuple[str, ...]
    clarification_turns: int

    def __init__(self, case_id, context, turns, expected_action, expected_intents, *,
                 required_reason_codes=(), required_source_ids=(), clarification_turns=0):
        _id(case_id)
        if type(context) is not Context:
            raise AcceptanceError("acceptance_context_invalid")
        try:
            context = Context(**context.to_dict())
            history = _turns(turns)
        except (ValueError, TypeError, KeyError):
            raise AcceptanceError("acceptance_history_invalid") from None
        if type(expected_action) is not str or expected_action not in {"auto_reply", "auto_clarify", "handoff", "hold"}:
            raise AcceptanceError("acceptance_expectation_invalid")
        intents = _strings(expected_intents)
        if not intents or not set(intents) <= set(INTENT_CATALOG):
            raise AcceptanceError("acceptance_expectation_invalid")
        for name, value in {"case_id": case_id, "context": context,
                "_turns_json": _json(history, 100000), "expected_action": expected_action,
                "expected_intents": intents, "required_reason_codes": _strings(required_reason_codes),
                "required_source_ids": _strings(required_source_ids),
                "clarification_turns": _integer(clarification_turns, 1000)}.items():
            object.__setattr__(self, name, value)

    @property
    def turns(self):
        return json.loads(self._turns_json)

    @property
    def history_sha256(self):
        return sha256(self._turns_json.encode("utf-8")).hexdigest()

    def as_dict(self):
        return {"case_id": self.case_id, "context": self.context.to_dict(), "turns": self.turns,
                "expected_action": self.expected_action, "expected_intents": list(self.expected_intents),
                "required_reason_codes": list(self.required_reason_codes),
                "required_source_ids": list(self.required_source_ids),
                "clarification_turns": self.clarification_turns}

    @classmethod
    def from_dict(cls, value):
        fields = {"case_id", "context", "turns", "expected_action", "expected_intents",
                  "required_reason_codes", "required_source_ids", "clarification_turns"}
        if type(value) is not dict or value.keys() != fields or type(value["context"]) is not dict:
            raise AcceptanceError("acceptance_case_invalid")
        try:
            return cls(**{**value, "context": Context(**value["context"])})
        except (TypeError, ValueError):
            raise AcceptanceError("acceptance_case_invalid") from None


def _observation(value):
    fields = {"model", "usage_known", "cost_known", "estimated_cost_usd", "model_calls"}
    if (type(value) is not dict or value.keys() != fields
            or type(value["model_calls"]) is not int or value["model_calls"] != 1
            or any(type(value[name]) is not bool for name in ("usage_known", "cost_known"))
            or value["model"] is not None and (type(value["model"]) is not str or not 1 <= len(value["model"]) <= 256)):
        raise AcceptanceError("acceptance_model_observation_invalid")
    cost = value["estimated_cost_usd"]
    if ((value["cost_known"] and (type(cost) not in (int, float) or not 0 <= cost <= 1e12 or not math.isfinite(cost)))
            or not value["cost_known"] and cost is not None):
        raise AcceptanceError("acceptance_model_observation_invalid")
    return json.loads(_json(value, 2048))


def evaluate_sample(request, case, candidate, model_observation, *, now):
    """Compute bounded structural/policy checks; this is not semantic grading.

    ``candidate`` and telemetry must be obtained by the host in its actual
    generation path. The callback/wire authority boundary lives above this pure
    function. Human review must still assess the content of every exact reply.
    """
    if type(request) is not AdmissionRequest or type(case) is not AcceptanceCase:
        raise AcceptanceError("acceptance_input_invalid")
    _integer(now)
    if any(getattr(request, key) != getattr(case.context, key)
           for key in ("tenant_id", "shop_id", "channel", "account_id")):
        raise AcceptanceError("acceptance_scope_mismatch")
    # This also prevents hashing/retaining an unbounded provider object.
    try:
        _bounded_json(candidate)
        candidate_json = _json(candidate, MAX_CANDIDATE_BYTES)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise AcceptanceError("acceptance_candidate_bounds") from None
    candidate_sha256 = sha256(candidate_json.encode("utf-8")).hexdigest()
    failures = []
    try:
        _observation(model_observation)
    except AcceptanceError:
        failures.append("model_observation_missing_or_invalid")
    basis = CurrentBasis(**{name: getattr(request, name) for name in
        ("package_sha256", "knowledge_sha256", "model_sha256", "policy_sha256")})
    decision = evaluate_autonomy(admission=request.snapshot(), basis=basis, context=case.context,
        candidate=candidate, expected_history_hash=case.history_sha256, now=now,
        clarification_turns=case.clarification_turns)
    actual_intents = []
    sources = []
    try:
        draft, handoff, history_hash = _candidate(candidate)
        actual_intents = sorted({item["name"] for item in draft["understanding"]["intents"]
                                 if item["disposition"] in {"active", "uncertain"}})
        sources = sorted({item["source_id"] for item in draft["citations"]})
        if (candidate["context_key"] != case.context.key or history_hash != case.history_sha256
                or candidate["history_hash"] != case.history_sha256):
            failures.append("candidate_scope_or_history_changed")
        if (candidate["result_status"] != "draft" or candidate["mode"] != "model-candidate"
                or candidate["model_calls"] != 1 or candidate["validation_errors"]):
            failures.append("candidate_not_valid_model_result")
        if candidate.get("effects_produced") != [] or handoff.get("executed_actions") != []:
            failures.append("candidate_effects_present")
    except (ValueError, TypeError, KeyError, RecursionError):
        failures.append("candidate_invalid")
    if decision.action != case.expected_action:
        failures.append("action_mismatch")
    if tuple(actual_intents) != case.expected_intents:
        failures.append("intent_mismatch")
    if not set(case.required_reason_codes) <= set(decision.reason_codes):
        failures.append("reason_missing")
    if not set(case.required_source_ids) <= set(sources):
        failures.append("source_missing")
    return {"request_sha256": request.digest, "case_sha256": _digest(case.as_dict()),
            "candidate_sha256": candidate_sha256, "history_sha256": case.history_sha256,
            "action": decision.action, "reason_codes": list(decision.reason_codes),
            "actual_intents": actual_intents, "source_ids": sources,
            "failures": failures, "status": "passed" if not failures else "failed",
            "semantic_quality_verified": False, "sending_authorized": False}


def _validate_suite(request, cases, artifacts):
    if (type(request) is not AdmissionRequest or type(cases) is not list
            or not 1 <= len(cases) <= MAX_CASES or any(type(case) is not AcceptanceCase for case in cases)
            or len({case.case_id for case in cases}) != len(cases)):
        raise AcceptanceError("acceptance_suite_invalid")
    for case in cases:
        if any(getattr(request, key) != getattr(case.context, key)
               for key in ("tenant_id", "shop_id", "channel", "account_id")):
            raise AcceptanceError("acceptance_scope_mismatch")
    intents = {intent for case in cases if case.expected_action in {"auto_reply", "auto_clarify"}
               for intent in case.expected_intents}
    if (not {"auto_reply", "auto_clarify", "handoff"} <= {case.expected_action for case in cases}
            or not request.allowed_intents <= intents
            or not request.authorized_reply_actions <= {case.expected_action for case in cases}
            or not any(case.expected_action == "handoff" and set(case.required_reason_codes) & _RISK_REASONS
                       for case in cases)):
        raise AcceptanceError("acceptance_coverage_incomplete")
    if (type(artifacts) is not dict or artifacts.keys() != {"knowledge", "package", "policy_rules", "model_profile"}
            or any(type(artifacts[name]) is not dict for name in artifacts)):
        raise AcceptanceError("acceptance_artifacts_invalid")
    _json(artifacts, MAX_ARTIFACT_BYTES)
    package = artifacts["package"]
    if package.keys() != {"package_id", "version", "fingerprint"}:
        raise AcceptanceError("acceptance_artifacts_invalid")
    _id(package["package_id"])
    _id(package["version"])
    if (package["fingerprint"] != request.package_sha256
            or _digest(artifacts["knowledge"]) != request.knowledge_sha256
            or _digest(artifacts["policy_rules"]) != request.policy_sha256
            or _digest(artifacts["model_profile"]) != request.model_sha256):
        raise AcceptanceError("acceptance_artifacts_changed")


_SCHEMA = {
    "suites": """CREATE TABLE suites (
      suite_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL)""",
    "starts": """CREATE TABLE starts (
      suite_id TEXT NOT NULL REFERENCES suites(suite_id), case_id TEXT NOT NULL,
      record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(suite_id,case_id))""",
    "results": """CREATE TABLE results (
      suite_id TEXT NOT NULL, case_id TEXT NOT NULL, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
      PRIMARY KEY(suite_id,case_id), FOREIGN KEY(suite_id,case_id) REFERENCES starts(suite_id,case_id))""",
    "reviews": """CREATE TABLE reviews (
      suite_id TEXT NOT NULL, case_id TEXT NOT NULL, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
      PRIMARY KEY(suite_id,case_id), FOREIGN KEY(suite_id,case_id) REFERENCES results(suite_id,case_id))""",
    "revocations": """CREATE TABLE revocations (
      suite_id TEXT PRIMARY KEY REFERENCES suites(suite_id), record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL)""",
}
for _table in tuple(_SCHEMA):
    for _operation in ("UPDATE", "DELETE"):
        _name = _table + "_no_" + _operation.lower()
        _SCHEMA[_name] = (f"CREATE TRIGGER {_name} BEFORE {_operation} ON {_table} BEGIN "
                         "SELECT RAISE(ABORT, 'Acceptance facts are immutable'); END")


class AcceptanceLedger:
    """Single-owner, append-only service ledger in an explicit separate file.

    ``authorize`` must authenticate each current host request, raise on denial,
    and return None. It must not be constructed from browser callbacks. The host
    must derive actor strings from that same authority. No schema repair, path
    search, directory creation, task recovery, model execution or retry occurs.
    A started case without a result remains unknown after reopening.
    """

    def __init__(self, path, *, authorize, clock=None, initialize=False):
        if not callable(authorize) or type(initialize) is not bool:
            raise AcceptanceError("acceptance_host_required")
        self._authorize = authorize
        self._clock = clock or time.time
        self._closed = False
        self._lock = threading.RLock()
        self._owner_id = uuid.uuid4().hex
        self._check()
        raw_path = Path(path)
        if not raw_path.is_absolute() or ".." in raw_path.parts or raw_path.drive.startswith("\\\\"):
            raise AcceptanceError("acceptance_path_invalid")
        self.path = Path(path).absolute()
        if self.path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            raise AcceptanceError("acceptance_path_invalid")
        owner_path = Path(str(self.path) + ".owner")
        for part in (self.path, owner_path, *self.path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                if part not in (self.path, owner_path):
                    raise AcceptanceError("acceptance_parent_missing") from None
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or part in (self.path, owner_path) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                raise AcceptanceError("acceptance_linked_path")
        if not self.path.parent.is_dir():
            raise AcceptanceError("acceptance_parent_missing")
        if initialize and (self.path.exists() or owner_path.exists()):
            raise AcceptanceError("acceptance_new_file_required")
        if not initialize and (not self.path.is_file() or not owner_path.is_file()):
            raise AcceptanceError("acceptance_store_missing")
        if self.path.exists() and self.path.stat().st_size > MAX_DATABASE_BYTES:
            raise AcceptanceError("acceptance_store_limit")
        self._owner = open(owner_path, "x+b" if initialize else "r+b")
        try:
            if initialize:
                self._owner.write(b"0")
                self._owner.flush()
            self._owner.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if initialize:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                      timeout=2, isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            if initialize:
                self.db.execute("PRAGMA page_size=4096")
                self.db.execute("PRAGMA journal_mode=DELETE")
                self.db.execute("PRAGMA synchronous=FULL")
                script = ";\n".join(_SCHEMA.values())
                self.db.executescript("BEGIN IMMEDIATE;\n" + script
                    + f";\nPRAGMA user_version={_VERSION};\nPRAGMA application_id={_APPLICATION};\nCOMMIT;")
            self._verify_schema()
            if (self.db.execute("PRAGMA page_size").fetchone()[0] != 4096
                    or self.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete"):
                raise AcceptanceError("acceptance_store_format_invalid")
            pages = MAX_DATABASE_BYTES // self.db.execute("PRAGMA page_size").fetchone()[0]
            self.db.execute(f"PRAGMA max_page_count={pages}")
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._owner.close()
            self._closed = True
            raise

    def _check(self):
        if self._closed:
            raise AcceptanceError("acceptance_store_closed")
        try:
            if self._authorize() is not None:
                raise ValueError
        except Exception:
            raise AcceptanceError("acceptance_host_denied") from None

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not 0 <= value <= 253402300799 or not math.isfinite(value):
            raise AcceptanceError("acceptance_clock_invalid")
        return int(value)

    def _verify_schema(self):
        if (self.db.execute("PRAGMA user_version").fetchone()[0] != _VERSION
                or self.db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION):
            raise AcceptanceError("acceptance_schema_invalid")
        observed = {row["name"]: " ".join((row["sql"] or "").split()) for row in self.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if observed != {name: " ".join(sql.split()) for name, sql in _SCHEMA.items()}:
            raise AcceptanceError("acceptance_schema_invalid")

    @contextmanager
    def _transaction(self, *, write=False):
        with self._lock:
            self._check()
            self._verify_schema()
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _insert(self, table, suite_id, record, case_id=None):
        # All table arguments are internal constants, never caller input.
        encoded = _json(record, MAX_ARTIFACT_BYTES + 4 * 1024 * 1024)
        values = (suite_id, encoded, sha256(encoded.encode("utf-8")).hexdigest())
        if case_id is not None:
            values = (suite_id, case_id, *values[1:])
        self.db.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values)

    @staticmethod
    def _record(row):
        raw = row["record_json"]
        if (type(raw) is not str or len(raw) > MAX_ARTIFACT_BYTES + 4 * 1024 * 1024
                or sha256(raw.encode("utf-8")).hexdigest() != row["record_sha256"]):
            raise AcceptanceError("acceptance_record_changed")
        try:
            value = json.loads(raw)
            if _json(value, MAX_ARTIFACT_BYTES + 4 * 1024 * 1024) != raw:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise AcceptanceError("acceptance_record_changed") from None
        return value

    def _suite(self, suite_id):
        _id(suite_id)
        row = self.db.execute("SELECT * FROM suites WHERE suite_id=?", (suite_id,)).fetchone()
        if row is None:
            raise AcceptanceError("acceptance_suite_missing")
        record = self._record(row)
        request = AdmissionRequest.from_dict(record["request"])
        cases = [AcceptanceCase.from_dict(case) for case in record["cases"]]
        _validate_suite(request, cases, record["artifacts"])
        if (record["suite_id"] != suite_id or record["request_sha256"] != request.digest
                or record["artifacts_sha256"] != _digest(record["artifacts"])):
            raise AcceptanceError("acceptance_record_changed")
        return record, request, {case.case_id: case for case in cases}

    def _rows(self, table, suite_id):
        return {row["case_id"]: self._record(row) for row in self.db.execute(
            f"SELECT * FROM {table} WHERE suite_id=? ORDER BY case_id LIMIT {MAX_CASES + 1}", (suite_id,))}

    def _revoked(self, suite_id):
        return self.db.execute("SELECT 1 FROM revocations WHERE suite_id=?", (suite_id,)).fetchone() is not None

    def _writable(self, suite_id, case_id):
        suite, request, cases = self._suite(suite_id)
        _id(case_id)
        if case_id not in cases or self._revoked(suite_id):
            raise AcceptanceError("acceptance_case_unavailable")
        return suite, request, cases[case_id]

    def begin_suite(self, request, cases, actor, *, artifacts, label="engineering"):
        self._check()
        _id(actor)
        _id(label)
        artifacts = json.loads(_json(artifacts, MAX_ARTIFACT_BYTES))
        _validate_suite(request, cases, artifacts)
        suite_id = uuid.uuid4().hex
        record = {"schema_version": "ampuly.cs.acceptance/1", "suite_id": suite_id,
            "request": request.as_dict(), "request_sha256": request.digest,
            "cases": [case.as_dict() for case in sorted(cases, key=lambda item: item.case_id)],
            "artifacts": artifacts, "artifacts_sha256": _digest(artifacts),
            "actor": actor, "label": label, "created_at": self._now()}
        with self._transaction(write=True):
            if self.db.execute("SELECT COUNT(*) FROM suites").fetchone()[0] >= MAX_SUITES:
                raise AcceptanceError("acceptance_suite_limit")
            self._insert("suites", suite_id, record)
        return suite_id

    @staticmethod
    def _references(suite_id):
        return {"ai_reference": f"cs-acceptance:{suite_id}:ai",
                "human_reference": f"cs-acceptance:{suite_id}:human"}

    def references(self, suite_id):
        with self._transaction():
            self._suite(suite_id)
            return self._references(suite_id)

    def mark_started(self, suite_id, case_id):
        with self._transaction(write=True):
            self._writable(suite_id, case_id)
            if self.db.execute("SELECT 1 FROM starts WHERE suite_id=? AND case_id=?", (suite_id, case_id)).fetchone():
                raise AcceptanceError("acceptance_case_already_started")
            if self.db.execute("SELECT 1 FROM starts s LEFT JOIN results r USING(suite_id,case_id) "
                               "WHERE s.suite_id=? AND r.case_id IS NULL", (suite_id,)).fetchone():
                raise AcceptanceError("acceptance_previous_case_unfinished")
            self._insert("starts", suite_id, {"owner_id": self._owner_id, "started_at": self._now()}, case_id)

    def _started(self, suite_id, case_id):
        row = self.db.execute("SELECT * FROM starts WHERE suite_id=? AND case_id=?", (suite_id, case_id)).fetchone()
        if row is None or self._record(row)["owner_id"] != self._owner_id:
            raise AcceptanceError("acceptance_case_not_owned_or_unknown")
        if self.db.execute("SELECT 1 FROM results WHERE suite_id=? AND case_id=?", (suite_id, case_id)).fetchone():
            raise AcceptanceError("acceptance_case_already_completed")

    def record_case(self, suite_id, case_id, candidate, model_observation):
        """Host-only post-generation boundary. Never expose this on a wire API."""
        with self._transaction(write=True):
            _, request, case = self._writable(suite_id, case_id)
            self._started(suite_id, case_id)
            at = self._now()
            candidate = json.loads(_json(candidate, MAX_CANDIDATE_BYTES))
            observation = json.loads(_json(model_observation, 2048))
            evaluation = evaluate_sample(request, case, candidate, observation, now=at)
            record = {"status": evaluation["status"], "candidate": candidate,
                "model_observation": observation, "evaluation": evaluation, "at": at}
            self._insert("results", suite_id, record, case_id)
            return json.loads(_json(record, MAX_CANDIDATE_BYTES + 8192))

    def record_failure(self, suite_id, case_id, *, status="failed", reason_code):
        if type(status) is not str or status not in {"failed", "unknown"}:
            raise AcceptanceError("acceptance_failure_invalid")
        _id(reason_code)
        with self._transaction(write=True):
            self._writable(suite_id, case_id)
            self._started(suite_id, case_id)
            record = {"status": status, "reason_code": reason_code, "at": self._now()}
            self._insert("results", suite_id, record, case_id)
            return record

    def review_case(self, suite_id, case_id, *, actor, candidate_sha256, verdict, note):
        _id(actor)
        _hash(candidate_sha256)
        if type(verdict) is not str or verdict not in {"accept", "reject"}:
            raise AcceptanceError("acceptance_review_invalid")
        try:
            bounded_text(note, "review note", 2000)
            _json(note, 8000)
        except (ValueError, TypeError):
            raise AcceptanceError("acceptance_review_invalid") from None
        with self._transaction(write=True):
            self._writable(suite_id, case_id)
            results = self._rows("results", suite_id)
            result = results.get(case_id)
            if (result is None or "evaluation" not in result
                    or result["evaluation"]["candidate_sha256"] != candidate_sha256):
                raise AcceptanceError("acceptance_review_candidate_changed")
            reviews = self._rows("reviews", suite_id)
            if case_id in reviews or any(review["actor"] != actor for review in reviews.values()):
                raise AcceptanceError("acceptance_review_conflict")
            record = {"actor": actor, "candidate_sha256": candidate_sha256,
                      "result_sha256": _digest(result), "verdict": verdict, "note": note, "at": self._now()}
            self._insert("reviews", suite_id, record, case_id)
            return record

    def revoke_suite(self, suite_id, *, actor, reason_code):
        _id(actor)
        _id(reason_code)
        with self._transaction(write=True):
            self._suite(suite_id)
            if self._revoked(suite_id):
                raise AcceptanceError("acceptance_already_revoked")
            self._insert("revocations", suite_id, {"actor": actor, "reason_code": reason_code, "at": self._now()})

    def _details(self, suite_id):
        suite, request, cases = self._suite(suite_id)
        starts, results, reviews = (self._rows(table, suite_id) for table in ("starts", "results", "reviews"))
        if any(not set(records) <= set(cases) for records in (starts, results, reviews)):
            raise AcceptanceError("acceptance_record_changed")
        if not set(results) <= set(starts) or not set(reviews) <= set(results):
            raise AcceptanceError("acceptance_record_changed")
        all_passed = len(results) == len(cases)
        for case_id, result in results.items():
            if "evaluation" not in result:
                all_passed = False
                continue
            actual = evaluate_sample(request, cases[case_id], result["candidate"], result["model_observation"], now=result["at"])
            if actual != result["evaluation"] or result["status"] != actual["status"]:
                raise AcceptanceError("acceptance_record_changed")
            all_passed = all_passed and actual["status"] == "passed"
        accepted = all_passed and len(reviews) == len(cases)
        for case_id, review in reviews.items():
            result = results.get(case_id)
            if (result is None or "evaluation" not in result or review["result_sha256"] != _digest(result)
                    or review["candidate_sha256"] != result["evaluation"]["candidate_sha256"]):
                raise AcceptanceError("acceptance_record_changed")
            accepted = accepted and review["verdict"] == "accept"
        if len({review["actor"] for review in reviews.values()}) > 1:
            raise AcceptanceError("acceptance_record_changed")
        revoked = self._revoked(suite_id)
        states = {case_id: (results[case_id]["status"] if case_id in results else
                  "running" if starts.get(case_id, {}).get("owner_id") == self._owner_id else
                  "unknown" if case_id in starts else "pending") for case_id in cases}
        return {**suite, "case_results": results, "reviews": reviews, "case_states": states,
                "ai_complete": all_passed, "human_complete": accepted, "revoked": revoked,
                "status": "revoked" if revoked else "accepted" if accepted else "incomplete",
                "references": self._references(suite_id)}

    def get_suite(self, suite_id):
        with self._transaction():
            return self._details(suite_id)

    def evidence_reader(self, reference):
        _id(reference)
        with self._transaction():
            match = re.fullmatch(r"cs-acceptance:([a-f0-9]{32}):(ai|human)", reference)
            if match is None:
                return None
            suite_id, suffix = match.groups()
            if self.db.execute("SELECT 1 FROM suites WHERE suite_id=?", (suite_id,)).fetchone() is None:
                return None
            details = self._details(suite_id)
            if details["revoked"] or not details["ai_complete"] or not details["human_complete"]:
                return None
            kind = "ai_test" if suffix == "ai" else "human_acceptance"
            actor = details["actor"] if suffix == "ai" else next(iter(details["reviews"].values()))["actor"]
            # Both kinds bind the entire accepted suite and every review. No
            # mutation can preserve a previously issued admission evidence hash.
            record_sha256 = _digest({"kind": kind, "accepted_suite": details})
            return AcceptanceEvidence(reference, kind, details["request_sha256"], record_sha256, actor, True)

    def close(self):
        """Release only this ledger owner, even if the host authorization expired."""
        with self._lock:
            if not self._closed:
                self.db.close()
                self._owner.close()
                self._closed = True
