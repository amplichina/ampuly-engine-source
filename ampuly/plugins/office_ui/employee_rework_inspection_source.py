"""Owner-only M4 observation from existing sources, never a rework command.

Only an order's exact launched Task is supported. Child lineage metadata is
not an order binding. This module does not construct a store/engine, bootstrap
accounts, attach projects, repair schema, dispatch, or query credentials.
Two RO observations detect observed changes, not an atomic cross-DB instant,
ABA, a lease, durable approval, or an authoritative attempt producer.
SQLite mode=ro does not create the main DB; normal WAL coordination may occur.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
from urllib.parse import quote

from ampuly.core.sqlite_open import opened
from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .employee_rework_inspection_projection import INSPECT_ACTION, project_rework_observation

_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_.@-]{2,64}\Z", re.ASCII)
_MAX_JSON = 2 * 1024 * 1024
_MESSAGES = {
    "employee_rework_request_invalid": "核对请求不完整或包含不支持的参数。",
    "owner_required": "请由当前有效的企业所有者登录后核对。",
    "employee_rework_source_unavailable": "原记录来源不可用、归属未核实或不满足安全读取边界。",
    "employee_rework_source_changed": "核对期间观察到来源变化，请重新核对；未重派。",
}


def _fail(code: str) -> None:
    raise AIEmployeeContractError(code, _MESSAGES[code]) from None


def _request(data: Any) -> dict:
    keys = {"type", "project_id", "order_id", "task_id", "request_id"}
    if (type(data) is not dict or set(data) != keys or data.get("type") != INSPECT_ACTION
            or any(type(data[key]) is not str or not _ID.fullmatch(data[key]) for key in keys - {"type"})):
        _fail("employee_rework_request_invalid")
    return dict(data)


@dataclass(frozen=True, repr=False)
class _Context:
    root: Any
    engine: Any
    store: Any
    backing_db: Any
    login: str
    business_path: Path
    runtime_path: Path


def _context(handler: Any, socket: Any, request: dict) -> _Context:
    # Only already held objects, never lazy getters, ensure_ready or attach.
    root = vars(handler).get("_root_engine")
    if root is None:
        _fail("employee_rework_source_unavailable")
    account_map = vars(handler).get("_client_accounts")
    account = account_map.get(socket) if type(account_map) is dict else None
    login = vars(account).get("login") if account is not None and hasattr(account, "__dict__") else None
    if type(login) is not str or not _LOGIN.fullmatch(login):
        _fail("owner_required")
    home = vars(root).get("ampuly_home")
    if not isinstance(home, (str, Path)) or not Path(home).is_absolute():
        _fail("employee_rework_source_unavailable")
    project = request["project_id"]
    if vars(root).get("project_id") == project:
        engine = root
    else:
        delegates = vars(root).get("_project_engine_delegates")
        engine = delegates.get(project) if type(delegates) is dict else None
    if engine is None or vars(engine).get("project_id") != project:
        _fail("employee_rework_source_unavailable")
    store = vars(engine).get("store")
    # AmpulyStore.is_ready is a property; inspect its actual backing connection
    # without calling an initializer or accepting a look-alike bound method.
    if (store is None or vars(store).get("_db") is None
            or vars(store).get("project_id") != project):
        _fail("employee_rework_source_unavailable")
    path = vars(store).get("db_path")
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        _fail("employee_rework_source_unavailable")
    return _Context(root, engine, store, vars(store)["_db"], login, Path(home) / "business.db", Path(path))


def _same_context(left: _Context, right: _Context) -> bool:
    return (left.root is right.root and left.engine is right.engine and left.store is right.store
            and left.backing_db is right.backing_db
            and left.login == right.login and left.business_path == right.business_path
            and left.runtime_path == right.runtime_path)


def _pin(path: Path) -> tuple:
    if not path.is_absolute() or ".." in path.parts or len(path.parents) > 32:
        _fail("employee_rework_source_unavailable")
    identities = []
    for index, part in enumerate((path, *path.parents)):
        value = part.lstat()
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            _fail("employee_rework_source_unavailable")
        if index == 0:
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                _fail("employee_rework_source_unavailable")
        elif not stat.S_ISDIR(value.st_mode):
            _fail("employee_rework_source_unavailable")
        identities.append((value.st_dev, value.st_ino))
    # This detects observed identity replacement, not a native handle lease.
    return tuple(identities)


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("employee_rework_source_unavailable")
        result[key] = value
    return result


def _json_object(raw: Any) -> dict:
    if type(raw) is not str or len(raw.encode("utf-8")) > _MAX_JSON:
        _fail("employee_rework_source_unavailable")
    value = json.loads(raw, object_pairs_hook=_pairs,
                       parse_constant=lambda _: _fail("employee_rework_source_unavailable"))
    if type(value) is not dict:
        _fail("employee_rework_source_unavailable")
    canonical_json(value)  # existing depth64 / 50k nodes / 2 MiB ceilings
    return value


def _uri_path(path: Path) -> str:
    # opened adds file: and ?mode=ro. Encode #/%/? instead of interpreting
    # them as URI authority/query; no client path enters either connection.
    return quote(str(path).replace("\\", "/"), safe="/:")


def _limited(column: str, ceiling: int) -> str:
    # Only static internal column names/constants below use this helper.
    # SQLite checks byte length before Python materializes the selected TEXT.
    return (f"CASE WHEN typeof({column})='text' AND length(CAST({column} AS BLOB))<={ceiling} "
            f"THEN {column} ELSE NULL END AS {column}")


def _business_observation(context: _Context, request: dict) -> tuple:
    before = _pin(context.business_path)
    with opened(_uri_path(context.business_path), read_only=True, timeout=2.0,
                row_factory=sqlite3.Row) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT " + _limited("login", 64) + ", " + _limited("login_key", 64)
            + ", CASE WHEN role COLLATE BINARY='owner' THEN 'owner' ELSE NULL END AS role, "
            "CASE WHEN typeof(disabled)='integer' AND disabled=0 THEN 0 ELSE 1 END AS disabled, "
            "CASE WHEN customer_ids COLLATE BINARY='' THEN '' ELSE NULL END AS customer_ids "
            "FROM accounts WHERE login_key COLLATE BINARY=? LIMIT 2",
            (context.login.lower(),),
        ).fetchall()
        if (len(rows) != 1 or rows[0]["login"] != context.login
                or rows[0]["login_key"] != context.login.lower() or rows[0]["role"] != "owner"
                or type(rows[0]["disabled"]) is not int or rows[0]["disabled"] != 0
                or rows[0]["customer_ids"] != ""):
            _fail("owner_required")
        order = conn.execute(
            "SELECT " + ", ".join(_limited(name, limit) for name, limit in
                (("order_id", 256), ("status", 64), ("launched_task_id", 256), ("updated_at", 128),
                 ("fields_json", _MAX_JSON))) + " FROM work_orders WHERE order_id COLLATE BINARY=? LIMIT 2",
            (request["order_id"],),
        ).fetchall()
        if (len(order) != 1 or any(value is None for value in order[0])
                or order[0]["order_id"] != request["order_id"]):
            _fail("employee_rework_source_unavailable")
        fields = _json_object(order[0]["fields_json"])
        project = fields["project_id"] if "project_id" in fields else "default"
        if project != request["project_id"] or type(project) is not str:
            _fail("employee_rework_source_unavailable")
        # No guessed metadata/session/Run ancestry and no all-task scan.
        if order[0]["launched_task_id"] != request["task_id"]:
            _fail("employee_rework_source_unavailable")
        lifecycle = conn.execute(
            "SELECT " + ", ".join(_limited(name, limit) for name, limit in
                (("project_id", 256), ("status", 64), ("updated_at", 128)))
            + " FROM project_lifecycle WHERE project_id COLLATE BINARY=? LIMIT 2",
            (project,),
        ).fetchall()
        if (len(lifecycle) > 1 or (lifecycle and (any(value is None for value in lifecycle[0])
                or lifecycle[0]["project_id"] != project or lifecycle[0]["status"] not in {"active", "archived"}))):
            _fail("employee_rework_source_unavailable")
        # Absent lifecycle row is old-format absence, NOT a grant to execute.
        observation = (tuple(rows[0]), tuple(order[0]), tuple(lifecycle[0]) if lifecycle else None)
    if _pin(context.business_path) != before:
        _fail("employee_rework_source_changed")
    return before, observation


def _runtime_observation(context: _Context, request: dict) -> tuple:
    before = _pin(context.runtime_path)
    with opened(_uri_path(context.runtime_path), read_only=True, timeout=2.0,
                row_factory=sqlite3.Row) as conn:
        rows = conn.execute(
            "SELECT " + ", ".join(_limited(name, limit) for name, limit in
                (("id", 256), ("project_id", 256), ("status", 64), ("metadata", _MAX_JSON)))
            + " FROM tasks WHERE id COLLATE BINARY=? AND project_id COLLATE BINARY=? LIMIT 2",
            (request["task_id"], request["project_id"]),
        ).fetchall()
        if (len(rows) != 1 or any(value is None for value in rows[0]) or rows[0]["id"] != request["task_id"]
                or rows[0]["project_id"] != request["project_id"]):
            _fail("employee_rework_source_unavailable")
        metadata = _json_object(rows[0]["metadata"])
        selected = tuple(rows[0])
    if _pin(context.runtime_path) != before:
        _fail("employee_rework_source_changed")
    records = (metadata.get("human_review_rework_to"), metadata.get("gate_harness_rework_request"))
    if any(value is not None and type(value) is not dict for value in records):
        _fail("employee_rework_source_unavailable")
    # Includes private record contents for reread equality; never returned.
    digest = hashlib.sha256(canonical_json(metadata)).hexdigest()
    return before, selected[:3], digest, records


def _inspect_existing(context: _Context, request: dict) -> dict:
    business = _business_observation(context, request)
    runtime = _runtime_observation(context, request)
    result = project_rework_observation(
        project_id=request["project_id"], order_id=request["order_id"], task_id=request["task_id"],
        order_run_task_id=request["task_id"], human_review_rework_to=runtime[3][0],
        gate_harness_rework_request=runtime[3][1],
    )
    if runtime != _runtime_observation(context, request) or business != _business_observation(context, request):
        _fail("employee_rework_source_changed")
    return result


async def inspect_owner_rework(handler: Any, socket: Any, data: Any) -> dict:
    """One bounded RO request; correlation is NOT the record's request id."""
    correlation = None
    try:
        request = _request(data)
        correlation = request["request_id"]
        context = _context(handler, socket, request)
        result = await asyncio.to_thread(_inspect_existing, context, request)
        if not _same_context(context, _context(handler, socket, request)):
            _fail("employee_rework_source_changed")
        return {"type": "ack", "action": INSPECT_ACTION, "request_id": correlation,
                "ok": True, "effects": False, "inspection": result}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code not in _MESSAGES:
            code = "employee_rework_source_unavailable"
        return {"type": "ack", "action": INSPECT_ACTION, "request_id": correlation,
                "ok": False, "effects": False, "error": code, "message": _MESSAGES[code]}
