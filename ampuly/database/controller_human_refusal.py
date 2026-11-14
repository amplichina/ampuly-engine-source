"""Private controller-refusal observation, not EmployeeRun or review authority.

No connection, migration, client writer, scheduler switch or permission lives here.
The native Store supplies the fenced scope on its own transaction connection.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
from typing import Any


TABLE_NAME = "controller_human_refusal_facts_v1"
MAX_RECORD_BYTES = 64 * 1024
MAX_INT = 2**63 - 1
SOURCE_KIND = "controller_refusal_only"
_CODES = frozenset({"invalid_source", "unavailable", "conflict", "outcome_unknown"})
SCHEMA_STATEMENTS = (
    """CREATE TABLE controller_human_refusal_facts_v1 (
  fact_id TEXT NOT NULL PRIMARY KEY,
  project_id TEXT NOT NULL, run_id TEXT NOT NULL,
  work_item_id TEXT NOT NULL, task_id TEXT NOT NULL,
  claim_attempt_seq INTEGER NOT NULL CHECK(typeof(claim_attempt_seq)='integer' AND claim_attempt_seq>0),
  controller_lease_generation INTEGER NOT NULL CHECK(typeof(controller_lease_generation)='integer' AND controller_lease_generation>0),
  controller_owner_sha256 TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind='controller_refusal_only'),
  record_sha256 TEXT NOT NULL,
  record_bytes BLOB NOT NULL CHECK(typeof(record_bytes)='blob' AND length(record_bytes)<=65536),
  UNIQUE(project_id,run_id,work_item_id,task_id,claim_attempt_seq)
)""",
    """CREATE TRIGGER controller_human_refusal_facts_v1_no_update
BEFORE UPDATE ON controller_human_refusal_facts_v1
BEGIN SELECT RAISE(ABORT,'controller refusal fact immutable'); END""",
    """CREATE TRIGGER controller_human_refusal_facts_v1_no_delete
BEFORE DELETE ON controller_human_refusal_facts_v1
BEGIN SELECT RAISE(ABORT,'controller refusal fact immutable'); END""",
    """CREATE TRIGGER controller_human_refusal_facts_v1_no_replace
BEFORE INSERT ON controller_human_refusal_facts_v1
WHEN EXISTS(SELECT 1 FROM controller_human_refusal_facts_v1
  WHERE fact_id=NEW.fact_id OR
   (project_id=NEW.project_id AND run_id=NEW.run_id AND work_item_id=NEW.work_item_id
    AND task_id=NEW.task_id AND claim_attempt_seq=NEW.claim_attempt_seq))
BEGIN SELECT RAISE(ABORT,'controller refusal fact immutable'); END""",
)
_SCHEMA_NAMES = (
    TABLE_NAME, TABLE_NAME + "_no_update", TABLE_NAME + "_no_delete",
    TABLE_NAME + "_no_replace",
)
_OBSERVATION_FIELDS = (
    "task_id", "project_id", "observed_node_id", "observed_node_type",
    "observed_rework_to", "observed_assignee", "reply_excerpt",
    "reply_truncated", "observed_at", "blocked_reason", "progress_message",
)


class ControllerHumanRefusalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _CODES else "invalid_source"
        super().__init__(self.code)


def _fail(code: str = "invalid_source") -> None:
    raise ControllerHumanRefusalError(code) from None


def _text(value: Any, *, optional: bool = False, display: bool = False,
          limit: int = 512) -> str:
    if type(value) is not str or len(value) > limit or not value and not optional:
        _fail()
    if not display and value.strip() != value:
        _fail()
    for char in value:
        number = ord(char)
        if (0xD800 <= number <= 0xDFFF or number == 127
                or number < 32 and not (display and char in "\r\n\t")):
            _fail()
    if len(value.encode("utf-8")) > limit:
        _fail()
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INT:
        _fail()
    return value


def _canonical(value: Any) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RECORD_BYTES:
        _fail()
    return raw


@dataclass(frozen=True, slots=True, repr=False)
class ControllerHumanRefusalObservation:
    task_id: str
    project_id: str
    observed_node_id: str
    observed_node_type: str
    observed_rework_to: str
    observed_assignee: str
    reply_excerpt: str
    reply_truncated: bool
    observed_at: str
    blocked_reason: str
    progress_message: str


def prepare_controller_human_refusal_observation(
    *, task_id: str, project_id: str, observed_node_id: str,
    observed_node_type: str, observed_rework_to: str, observed_assignee: str,
    reply_excerpt: str, reply_truncated: bool, observed_at: str,
    blocked_reason: str, progress_message: str,
) -> ControllerHumanRefusalObservation:
    _text(task_id)
    _text(project_id)
    _text(observed_node_id, optional=True)
    if type(observed_node_type) is not str or observed_node_type != "human_review":
        _fail()
    _text(observed_rework_to, optional=True)
    _text(observed_assignee, optional=True)
    _text(reply_excerpt, display=True, limit=16000)
    if len(reply_excerpt) > 4000 or type(reply_truncated) is not bool:
        _fail()
    _text(observed_at, limit=128)
    _text(blocked_reason, display=True, limit=16000)
    _text(progress_message, display=True, limit=32000)
    result = ControllerHumanRefusalObservation(
        task_id, project_id, observed_node_id, observed_node_type,
        observed_rework_to, observed_assignee, reply_excerpt, reply_truncated,
        observed_at, blocked_reason, progress_message,
    )
    _canonical({name: getattr(result, name) for name in _OBSERVATION_FIELDS})
    return result


def validate_controller_human_refusal_observation(
    observation: Any,
) -> ControllerHumanRefusalObservation:
    if type(observation) is not ControllerHumanRefusalObservation:
        _fail()
    checked = prepare_controller_human_refusal_observation(
        **{name: getattr(observation, name) for name in _OBSERVATION_FIELDS})
    if checked != observation:
        _fail()
    return observation


def validate_controller_refusal_sink(sink: Any) -> None:
    invalid = False
    try:
        invalid = (not callable(sink) or inspect.iscoroutinefunction(sink)
                   or inspect.iscoroutinefunction(getattr(sink, "__call__", None)))
    except Exception:
        invalid = True
    if invalid:
        _fail()


def deliver_controller_refusal_observation(
    sink: Any, observation: ControllerHumanRefusalObservation,
) -> None:
    validate_controller_refusal_sink(sink)
    validate_controller_human_refusal_observation(observation)
    failed = False
    result = None
    try:
        result = sink(observation)
    except Exception:
        failed = True
    if failed:
        _fail()
    if result is not None:
        if inspect.iscoroutine(result):
            try:
                result.close()
            except Exception:
                pass
        _fail()


def _rows(conn: Any, sql: str, params: tuple, *, width: int,
          maximum: int = 2) -> list[tuple]:
    cursor = conn.execute(sql, params)
    try:
        rows = cursor.fetchmany(maximum + 1)
        if type(rows) is not list or len(rows) > maximum or any(
                type(row) is not tuple or len(row) != width for row in rows):
            _fail()
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    else:
        cursor.close()
        return rows


def _normalized_sql(value: str) -> str:
    value = value.strip()
    return value[:-1].rstrip() if value.endswith(";") else value


def require_controller_human_refusal_schema(conn: Any) -> None:
    """Metadata only; no schema installation, normalization or repair."""
    for index, name in enumerate(_SCHEMA_NAMES):
        measures = _rows(conn, "SELECT type, typeof(sql), length(CAST(sql AS BLOB)) "
                         "FROM sqlite_schema WHERE name = ? LIMIT 2", (name,), width=3)
        if not measures:
            _fail("unavailable" if index == 0 else "invalid_source")
        if (len(measures) != 1 or measures[0][0] != ("table" if index == 0 else "trigger")
                or measures[0][1] != "text" or type(measures[0][2]) is not int
                or not 0 < measures[0][2] <= 16384):
            _fail()
        rows = _rows(conn, "SELECT sql FROM sqlite_schema WHERE name = ? LIMIT 2",
                     (name,), width=1)
        if (len(rows) != 1 or type(rows[0][0]) is not str
                or _normalized_sql(rows[0][0]) != _normalized_sql(SCHEMA_STATEMENTS[index])):
            _fail()
    name_sizes = _rows(conn, "SELECT typeof(name), length(CAST(name AS BLOB)) "
                      "FROM sqlite_schema WHERE tbl_name = ? ORDER BY name LIMIT 8",
                      (TABLE_NAME,), width=2, maximum=7)
    if any(kind != "text" or type(size) is not int or not 0 < size <= 512
           for kind, size in name_sizes):
        _fail()
    names = _rows(conn, "SELECT name FROM sqlite_schema "
                  "WHERE tbl_name = ? ORDER BY name LIMIT 8", (TABLE_NAME,),
                  width=1, maximum=7)
    expected_names = set(_SCHEMA_NAMES) | {
        "sqlite_autoindex_" + TABLE_NAME + "_1",
        "sqlite_autoindex_" + TABLE_NAME + "_2",
    }
    if (len(names) != len(name_sizes) or len(names) != len(expected_names)
            or any(type(row[0]) is not str for row in names)
            or {row[0] for row in names} != expected_names):
        _fail()


def append_controller_human_refusal(
    conn: Any, *, observation: ControllerHumanRefusalObservation,
    project_id: str, run_id: str, work_item_id: str, task_id: str,
    claim_attempt_seq: int, controller_lease_generation: int,
    controller_owner_token: str,
) -> str:
    """Strict same-connection append, not independent proof of a FAILED write."""
    failure = None
    result = None
    try:
        validate_controller_human_refusal_observation(observation)
        for value in (project_id, run_id, work_item_id, task_id, controller_owner_token):
            _text(value)
        _integer(claim_attempt_seq)
        _integer(controller_lease_generation)
        if (conn.in_transaction is not True or observation.project_id != project_id
                or observation.task_id != task_id):
            _fail()
        require_controller_human_refusal_schema(conn)
        identity = _canonical(["controller_refusal_only.v1", project_id, run_id,
                               work_item_id, task_id, claim_attempt_seq])
        result = "crf-" + hashlib.sha256(identity).hexdigest()
        owner_hash = hashlib.sha256(controller_owner_token.encode("utf-8")).hexdigest()
        record = {name: getattr(observation, name) for name in _OBSERVATION_FIELDS}
        record.update(schema_version=1, source_kind=SOURCE_KIND, run_id=run_id,
                      work_item_id=work_item_id, claim_attempt_seq=claim_attempt_seq,
                      controller_lease_generation=controller_lease_generation,
                      controller_owner_sha256=owner_hash)
        raw = _canonical(record)
        digest = hashlib.sha256(raw).hexdigest()
        for sql, params in (
            ("SELECT 1 FROM controller_human_refusal_facts_v1 "
             "INDEXED BY sqlite_autoindex_controller_human_refusal_facts_v1_1 "
             "WHERE fact_id = ? LIMIT 2", (result,)),
            ("SELECT 1 FROM controller_human_refusal_facts_v1 "
             "INDEXED BY sqlite_autoindex_controller_human_refusal_facts_v1_2 "
             "WHERE project_id=? AND run_id=? AND work_item_id=? AND task_id=? "
             "AND claim_attempt_seq=? LIMIT 2",
             (project_id, run_id, work_item_id, task_id, claim_attempt_seq)),
        ):
            if _rows(conn, sql, params, width=1):
                _fail("conflict")
        cursor = conn.execute(
            "INSERT INTO controller_human_refusal_facts_v1 "
            "(fact_id,project_id,run_id,work_item_id,task_id,claim_attempt_seq,"
            "controller_lease_generation,controller_owner_sha256,source_kind,"
            "record_sha256,record_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (result, project_id, run_id, work_item_id, task_id, claim_attempt_seq,
             controller_lease_generation, owner_hash, SOURCE_KIND, digest, raw),
        )
        try:
            if type(cursor.rowcount) is not int or cursor.rowcount != 1:
                _fail()
        except BaseException:
            try:
                cursor.close()
            except BaseException:
                pass
            raise
        else:
            cursor.close()
    except ControllerHumanRefusalError as exc:
        failure = exc.code if type(exc) is ControllerHumanRefusalError else "invalid_source"
    except Exception:
        failure = "unavailable"
    if failure is not None:
        _fail(failure)
    return result
