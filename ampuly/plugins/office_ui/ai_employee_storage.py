"""Bounded file transactions for AE00 objects, not a runtime database.

Read paths never create directories, locks, journals or recovery records. Writes
use a process lock and a durable intent before replacing an object. A crash is
reconciled only on an explicit write; an uncertain or externally changed object
is never silently overwritten. No caller-supplied path may escape the two AE00
object families. This module does not grant permission: adapters must authenticate
and authorize before using it, including before replaying an old request.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_read_budget import AIEmployeeReadBudget
from .ai_employee_safe_files import AIEmployeeSafeFiles
from .ai_employee_safe_write import atomic_write, open_locked

MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_INTENT_BYTES = 8 * 1024 * 1024
MAX_CHANGES = 3
MAX_PENDING_TARGETS = 4096
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def _hash(raw: bytes | None) -> str | None:
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class AIEmployeeFileStore:
    # Subclasses may opt in to a bounded, server-owned resource transaction.
    max_changes = MAX_CHANGES

    def __init__(self, ampuly_home: Path) -> None:
        # Do not resolve a link and then trust the resolved location.
        self.root = Path(os.path.abspath(ampuly_home))
        self._check(self.root)
        self.files = AIEmployeeSafeFiles(self.root)
        self._root_guard = None

    def _check(self, path: Path) -> None:
        if not path.is_relative_to(self.root):
            raise AIEmployeeContractError("storage_path_invalid", "对象路径超出岗位资产范围。")
        for part in (path, *path.parents):
            if _linked(part):
                raise AIEmployeeContractError("storage_path_invalid", "岗位资产路径不能包含链接。")

    def object_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or "\\" in relative or ":" in relative:
            raise AIEmployeeContractError("storage_path_invalid", "岗位对象路径无效。")
        parts = PurePosixPath(relative).parts
        if relative != "/".join(parts) or any(p in {"", ".", ".."} for p in parts):
            raise AIEmployeeContractError("storage_path_invalid", "岗位对象路径无效。")
        package = len(parts) == 3 and parts[0] == "employee_packages" and bool(_PACKAGE.fullmatch(parts[1])) and parts[2] == "manifest.yaml"
        instance = len(parts) == 4 and parts[0] == "projects" and bool(_ID.fullmatch(parts[1])) and parts[2] == "employee_instances" and bool(re.fullmatch(r"ei-[a-f0-9]{16}(?:\.revisions\.jsonl|\.json)", parts[3]))
        bindings = len(parts) == 3 and parts[0] == "projects" and bool(_ID.fullmatch(parts[1])) and parts[2] == "employee_bindings.json"
        if not (package or instance or bindings):
            raise AIEmployeeContractError("storage_path_invalid", "不支持写入这种岗位对象。")
        path = self.root.joinpath(*parts)
        self._check(path)
        return path

    def _read(self, path: Path, limit: int = MAX_RECORD_BYTES) -> bytes | None:
        self._check(path)
        return self.files.read(path.relative_to(self.root).as_posix(), limit=limit)

    def read(self, relative: str, *, read_budget: AIEmployeeReadBudget | None = None) -> bytes | None:
        if read_budget is None:
            return self._read(self.object_path(relative))
        limit = read_budget.begin_read(MAX_RECORD_BYTES)
        raw = self._read(self.object_path(relative), limit)
        if raw is not None:
            read_budget.consume_bytes(len(raw), limit=limit)
        return raw

    def list_entries(self, relative: str, *, limit: int = 1001,
                     read_budget: AIEmployeeReadBudget | None = None) -> list[dict[str, str]]:
        """Inert directory enumeration through the same pinned-root boundary."""
        if read_budget is None:
            return self.files.list_entries(relative, limit=limit)
        bounded = read_budget.begin_enumeration(limit)
        rows = self.files.list_entries(relative, limit=bounded)
        read_budget.consume_entries(len(rows), limit=bounded)
        return rows

    def _atomic_write(self, path: Path, raw: bytes) -> None:
        self._check(path)
        atomic_write(self.root, path.relative_to(self.root).as_posix(), raw, root_guard=self._root_guard)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with open_locked(self.root, "employee_packages/.write.lock") as stream:
            guard = stream.ae_assert_root
            previous = self._root_guard
            self._root_guard = guard
            self.files.root_guard = guard
            try:
                guard()
                yield
                guard()
            finally:
                self._root_guard = previous
                self.files.root_guard = previous

    def _request_path(self, action: str, project_id: str, request_id: str) -> Path:
        if not all(isinstance(x, str) and 0 < len(x) <= 128 for x in (action, project_id, request_id)):
            raise AIEmployeeContractError("request_invalid", "动作、项目和请求标识必须明确。")
        key = hashlib.sha256(canonical_json([action, project_id, request_id])).hexdigest()
        return self.root / "employee_packages" / ".requests" / f"{key}.json"

    def request_status(self, *, action: str, project_id: str, request_id: str, actor: str, payload: Any,
                       require_committed: bool = False) -> dict[str, Any] | None:
        if type(require_committed) is not bool:
            raise AIEmployeeContractError("request_invalid", "请求核实模式无效。")
        path = self._request_path(action, project_id, request_id)
        raw = self._read(path, MAX_INTENT_BYTES)
        if raw is None:
            return None
        intent = self._decode_intent(raw)
        self._verify_request(intent, actor, payload)
        if intent["state"] == "committed":
            return {**intent["response"], "idempotent": True}
        # Inspect only. No repair, cleanup or acknowledgement write on a read.
        if not require_committed and all(_hash(self.read(row["path"])) == row["after_sha256"] for row in intent["changes"]):
            return {**intent["response"], "idempotent": True}
        effects = any(_hash(self.read(row["path"])) == row["after_sha256"] for row in intent["changes"])
        return {"ok": False, "result": "outcome_unknown", "error": "outcome_unknown",
                "effects_produced": effects if effects or not require_committed else None, "request_id": request_id}

    def _decode_intent(self, raw: bytes) -> dict[str, Any]:
        try:
            obj = json.loads(raw)
            canonical_json(obj)
            if (not isinstance(obj, dict) or obj.get("state") not in {"prepared", "committed"}
                    or not isinstance(obj.get("actor"), str) or not 0 < len(obj["actor"].strip()) <= 128
                    or not isinstance(obj.get("payload_sha256"), str) or not _SHA.fullmatch(obj["payload_sha256"])
                    or not isinstance(obj.get("response"), dict) or not isinstance(obj.get("changes"), list)
                    or not 0 < len(obj["changes"]) <= self.max_changes):
                raise ValueError
            paths = set()
            for row in obj["changes"]:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str) or row["path"].casefold() in paths:
                    raise ValueError
                self.object_path(row["path"])
                paths.add(row["path"].casefold())
                before = row.get("before_sha256")
                if "before_sha256" not in row or (before is not None and (not isinstance(before, str) or not _SHA.fullmatch(before))):
                    raise ValueError
                if not isinstance(row.get("after_text"), str) or not isinstance(row.get("after_sha256"), str):
                    raise ValueError
                after = row["after_text"].encode("utf-8")
                if len(after) > MAX_RECORD_BYTES or _hash(after) != row["after_sha256"]:
                    raise ValueError
            return obj
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
            raise AIEmployeeContractError("storage_record_invalid", "请求记录损坏，请核实原请求。") from exc

    def _pending_index(self) -> tuple[Path, dict[str, str]]:
        path = self.root / "employee_packages" / ".pending.json"
        raw = self._read(path)
        if raw is None:
            return path, {}
        try:
            index = json.loads(raw)
            canonical_json(index)
            if not isinstance(index, dict) or len(index) > MAX_PENDING_TARGETS:
                raise ValueError
            for target, request_key in index.items():
                self.object_path(target)
                if not isinstance(request_key, str) or not _SHA.fullmatch(request_key):
                    raise ValueError
            return path, index
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise AIEmployeeContractError("storage_record_invalid", "未决保存记录损坏，不能继续覆盖对象。") from exc

    def _reserve(self, intent: dict[str, Any], request: Path) -> None:
        """Reserve all targets in one atomic index before the first replacement.

        A new request cannot leap over a partially written revision/history pair.
        Completed receipts can release stale reservations on an explicit write;
        unresolved receipts must be reconciled using the original request ID.
        """
        index_path, index = self._pending_index()
        request_key = request.stem
        for row in intent["changes"]:
            previous_key = index.get(row["path"].casefold())
            if previous_key is None or previous_key == request_key:
                continue
            previous_raw = self._read(request.parent / f"{previous_key}.json", MAX_INTENT_BYTES)
            if previous_raw is None or self._decode_intent(previous_raw)["state"] != "committed":
                raise AIEmployeeContractError("outcome_unknown", "这个对象还有未决保存，请先核实原请求。")
            index = {target: key for target, key in index.items() if key != previous_key}
        index.update({row["path"].casefold(): request_key for row in intent["changes"]})
        if len(index) > MAX_PENDING_TARGETS:
            raise AIEmployeeContractError("storage_busy", "未决保存数量达到范围，请先处理已有请求。")
        self._atomic_write(index_path, canonical_json(index))

    def _release_reservation(self, request: Path) -> None:
        path, index = self._pending_index()
        remaining = {target: key for target, key in index.items() if key != request.stem}
        self._atomic_write(path, canonical_json(remaining))

    @staticmethod
    def _verify_request(intent: dict[str, Any], actor: str, payload: Any) -> None:
        if intent["actor"] != actor or intent["payload_sha256"] != hashlib.sha256(canonical_json(payload)).hexdigest():
            raise AIEmployeeContractError("request_id_reused", "同一请求标识不能换人或换内容。")

    @staticmethod
    def _before_commit(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        operation = callback if inspect.isfunction(callback) else getattr(callback, "__call__", callback)
        if (not callable(callback) or inspect.iscoroutinefunction(operation)
                or inspect.isasyncgenfunction(operation)):
            raise AIEmployeeContractError("request_invalid", "保存围栏必须是服务端同步检查。")
        result = callback()
        if result is not None:
            if inspect.iscoroutine(result):
                result.close()
            raise AIEmployeeContractError("request_invalid", "保存围栏未完成同步检查。")

    def commit(self, *, action: str, project_id: str, request_id: str, actor: str, payload: Any, changes: list[dict[str, Any]], response: dict[str, Any],
               before_commit: Callable[[], None] | None = None) -> dict[str, Any]:
        """CAS each target; a durable request identity survives response timeouts.

        changes: path / before_sha256 (None means absent) / after_text. A pending
        Legacy retry rolls forward only targets still equal to before/intended
        after. Guarded calls reject prepared intents instead of repairing them.
        No external action is performed, and no request is expired into a re-run.
        """
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise AIEmployeeContractError("request_invalid", "保存需要经过认证的操作者。")
        request = self._request_path(action, project_id, request_id)
        with self._lock():
            previous = self._read(request, MAX_INTENT_BYTES)
            if previous is not None:
                intent = self._decode_intent(previous)
                self._verify_request(intent, actor, payload)
                if intent["state"] == "committed":
                    try:
                        self._before_commit(before_commit)
                    except Exception:
                        raise AIEmployeeContractError("outcome_unknown", "原提交回执的当前依据待核实。") from None
                    return {**intent["response"], "idempotent": True}
                if before_commit is not None:
                    raise AIEmployeeContractError("outcome_unknown", "原保存仍未决；这里只核实，不继续写入。")
                repeated = True
            else:
                if (not isinstance(changes, list) or not 0 < len(changes) <= self.max_changes
                        or any(not isinstance(r, dict) or not isinstance(r.get("path"), str) for r in changes)
                        or len({r["path"].casefold() for r in changes}) != len(changes)):
                    raise AIEmployeeContractError("request_invalid", "保存对象数量或标识无效。")
                rows = []
                for row in changes:
                    path = self.object_path(row["path"])
                    if (not isinstance(row.get("after_text"), str) or "before_sha256" not in row
                            or (row["before_sha256"] is not None and (not isinstance(row["before_sha256"], str) or not _SHA.fullmatch(row["before_sha256"])))):
                        raise AIEmployeeContractError("request_invalid", "保存正文或原始修订标识无效。")
                    raw = row["after_text"].encode("utf-8")
                    if len(raw) > MAX_RECORD_BYTES or _hash(self._read(path)) != row["before_sha256"]:
                        raise AIEmployeeContractError("revision_conflict", "对象已变化，保留草稿后重新读取。")
                    rows.append({**row, "after_sha256": _hash(raw)})
                intent = {"state": "prepared", "actor": actor, "payload_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(), "changes": rows, "response": response}
                encoded = canonical_json(intent)
                # Preflight the larger committed form before any object write.
                canonical_json({**intent, "state": "committed"})
                if len(encoded) > MAX_INTENT_BYTES:
                    raise AIEmployeeContractError("request_invalid", "请求记录过大。")
                # Under the original file lock, after CAS/preflight and before
                # the first durable intent or business replacement. New guarded
                # writes never resume an existing prepared request.
                self._before_commit(before_commit)
                try:
                    self._atomic_write(request, encoded)
                except OSError as exc:
                    raise AIEmployeeContractError("outcome_unknown", "保存意图回执待核实，请查回原请求。") from exc
                repeated = False
            # Check *all* target hashes before any roll-forward. Foreign changes
            # leave a visible uncertain result; never overwrite somebody's work.
            for row in intent["changes"]:
                current = _hash(self.read(row["path"]))
                if current not in {row["before_sha256"], row["after_sha256"]}:
                    raise AIEmployeeContractError("outcome_unknown", "保存过程遇到其他修改，请核实原请求。")
            try:
                self._reserve(intent, request)
            except OSError as exc:
                raise AIEmployeeContractError("outcome_unknown", "保存准备回执待核实，请查回原请求。") from exc
            try:
                for row in intent["changes"]:
                    if _hash(self.read(row["path"])) != row["after_sha256"]:
                        self._atomic_write(self.object_path(row["path"]), row["after_text"].encode("utf-8"))
                intent["state"] = "committed"
                self._atomic_write(request, canonical_json(intent))
                self._release_reservation(request)
            except (OSError, AIEmployeeContractError) as exc:
                raise AIEmployeeContractError("outcome_unknown", "保存回执待核实；不要更换请求标识重做。") from exc
            return {**intent["response"], **({"idempotent": True} if repeated else {})}
