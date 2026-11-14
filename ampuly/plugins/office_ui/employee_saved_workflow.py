"""Bounded, native-handle reads of saved workflow bindings, never an admission.

Only the host supplies the two roots. No registry discovery, package loader,
organization, tests, skills, engine or filesystem writer is invoked. Directory
names are not workflow IDs. Runtime manifests override distribution manifests
in the same order as the existing registry, before project scope is checked.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_safe_files import AIEmployeeSafeFiles, WorkspaceBoundaryError, validate_relative
from .employee_packages import _UniqueSafeLoader
from .workflow_packages import _canonical_bytes, node_payload

MAX_DIRECTORY_ENTRIES = 256
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_YAML_NODES = 50_000
MAX_YAML_EVENTS = 150_000
MAX_WORKFLOW_NODES = 1000
_FLOW = re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def _fail(code: str = "storage_record_invalid") -> None:
    messages = {"binding_unresolved": "没有找到本项目保存节点的岗位绑定。",
                "workflow_revision_conflict": "流程保存修订已变化，请重新读取保存版本。",
                "storage_path_invalid": "保存流程的链接或路径换位被拒绝。"}
    raise AIEmployeeContractError(code, messages.get(code, "保存流程依据暂不能安全核实。"))


class _WorkflowYamlLoader(_UniqueSafeLoader):
    def get_event(self) -> Any:
        event = super().get_event()
        self._workflow_events = getattr(self, "_workflow_events", 0) + (event is not None)
        if self._workflow_events > MAX_YAML_EVENTS:
            raise ValueError("workflow YAML event limit")
        return event

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        self._workflow_nodes = getattr(self, "_workflow_nodes", 0) + 1
        if self._workflow_nodes > MAX_YAML_NODES:
            raise ValueError("workflow YAML node limit")
        return super().compose_node(parent, index)


@dataclass(frozen=True, repr=False)
class _SavedWorkflowNodeSnapshot:
    """Private immutable current-source observation, never an admission."""
    project_id: str
    workflow_id: str
    node_id: str
    binding_slot: str
    workflow_source: str
    manifest_raw_sha256: str
    manifest_bytes: bytes
    node_canonical_bytes: bytes
    node_canonical_sha256: str
    workflow_version_present: bool
    workflow_version_raw: str | None


def _capture_node_snapshot(
    *, project_id: str, workflow_id: str, node_id: str, binding_slot: str,
    workflow_source: str, manifest_raw_sha256: str, manifest_bytes: bytes,
    node_canonical_bytes: bytes, workflow_version_present: bool,
    workflow_version_raw: str | None,
) -> _SavedWorkflowNodeSnapshot:
    """Freeze the original loader observations; no second read/decode/canonical."""
    result = None
    failed = False
    try:
        if any(type(value) is not str or pattern.fullmatch(value) is None
               for value, pattern in ((project_id, _ID), (workflow_id, _FLOW),
                                      (node_id, _ID), (manifest_raw_sha256, _SHA))):
            raise ValueError
        if (type(binding_slot) is not str or binding_slot != "executor"
                or type(workflow_source) is not str
                or workflow_source not in {"distribution", "runtime"}):
            raise ValueError
        if (type(manifest_bytes) is not bytes or len(manifest_bytes) > MAX_MANIFEST_BYTES
                or type(node_canonical_bytes) is not bytes or not node_canonical_bytes
                or len(node_canonical_bytes) > MAX_MANIFEST_BYTES):
            raise ValueError
        if type(workflow_version_present) is not bool:
            raise ValueError
        if not workflow_version_present and workflow_version_raw is not None:
            raise ValueError
        if workflow_version_raw is not None and (
                type(workflow_version_raw) is not str or len(workflow_version_raw) > 128
                or workflow_version_raw.strip() != workflow_version_raw
                or any(ord(char) < 32 or ord(char) == 127 for char in workflow_version_raw)):
            raise ValueError
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_raw_sha256:
            raise ValueError
        node_digest = hashlib.sha256(node_canonical_bytes).hexdigest()
        if type(node_digest) is not str or _SHA.fullmatch(node_digest) is None:
            raise ValueError
        result = _SavedWorkflowNodeSnapshot(
            project_id, workflow_id, node_id, binding_slot, workflow_source,
            manifest_raw_sha256, manifest_bytes, node_canonical_bytes,
            node_digest, workflow_version_present, workflow_version_raw)
    except Exception:
        failed = True
    if failed:
        # Own handler has ended. Original loader/DTO/active caller exceptions
        # and traceback locals are not globally sanitized; cancellation passes.
        _fail()
    return result


@dataclass(frozen=True)
class SavedBindingSource:
    """Host-observed saved data, not proof of ownership or an execution grant.

    binding=None means this exact saved node has no portable employee binding.
    The context manager must finish its source checks before a receipt is sent.
    """
    project_id: str
    workflow_id: str
    node_id: str
    binding_slot: str
    binding: dict | None
    workflow_revision: str
    workflow_source: str
    workflow_version: str | None
    # Never projected or supplied by a browser. None preserves old preview
    # fixtures; the new selection actions require this host capability.
    verify: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    node_snapshot: _SavedWorkflowNodeSnapshot | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _Manifest:
    relative: str
    raw: bytes | None
    identity: tuple | None
    directory_identity: tuple
    value: dict | None


def _directory_identity(workspace: Any, relative: str) -> tuple:
    target = workspace.resolve(relative, use_output_root=False)
    if os.name == "nt":
        handle = workspace._open_directory_handle(target)
        try:
            return tuple(workspace._identity(handle))
        finally:
            workspace._close_handle(handle)
    fd = workspace.open_directory(target)
    try:
        info = os.fstat(fd)
        return int(info.st_dev), int(info.st_ino)
    finally:
        os.close(fd)


def _entries(workspace: Any, relative: str) -> tuple[tuple | None, list[dict]]:
    target = workspace.resolve(relative, use_output_root=False)
    try:
        before = _directory_identity(workspace, relative)
    except FileNotFoundError:
        return None, []
    # Reuse the existing native no-link enumeration, not its AE write-family
    # path validator. This private caller forms only the two fixed prefixes.
    reader = AIEmployeeSafeFiles._list_windows if os.name == "nt" else AIEmployeeSafeFiles._list_posix
    rows = sorted(reader(workspace, target, MAX_DIRECTORY_ENTRIES), key=lambda row: row["name"])
    if _directory_identity(workspace, relative) != before:
        _fail()
    return before, rows


def _read(workspace: Any, relative: str) -> tuple[bytes | None, tuple | None]:
    target = workspace.resolve(relative, use_output_root=False)
    try:
        fd = workspace.open_read(target)
    except FileNotFoundError:
        return None, None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_MANIFEST_BYTES:
            _fail()
        chunks = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        identity = (int(before.st_dev), int(before.st_ino), before.st_size, before.st_mtime_ns)
        if len(raw) > MAX_MANIFEST_BYTES or identity != (int(after.st_dev), int(after.st_ino), after.st_size, after.st_mtime_ns):
            _fail()
        current = workspace.open_read(target)
        try:
            check = os.fstat(current)
            if identity != (int(check.st_dev), int(check.st_ino), check.st_size, check.st_mtime_ns):
                _fail()
        finally:
            os.close(current)
        return raw, identity
    finally:
        os.close(fd)


def _decode(raw: bytes) -> dict:
    try:
        obj = yaml.load(raw.decode("utf-8"), Loader=_WorkflowYamlLoader)
        if (type(obj) is not dict or type(obj.get("schema_version")) is not int or obj["schema_version"] != 1
                or type(obj.get("id")) is not str or not _FLOW.fullmatch(obj["id"].strip())
                or obj.get("status") not in {"draft", "testing", "published", "retired"}):
            raise ValueError
        owner = obj.get("project_id")
        if owner is not None and (type(owner) is not str or (owner.strip() and not _ID.fullmatch(owner.strip()))):
            raise ValueError
        version = obj.get("version")
        if version is not None and (type(version) is not str or len(version) > 128 or version.strip() != version
                                    or any(ord(char) < 32 or ord(char) == 127 for char in version)):
            raise ValueError
        nodes = obj.get("nodes", [])
        if type(nodes) is not list or len(nodes) > MAX_WORKFLOW_NODES:
            raise ValueError
        ids = set()
        for node in nodes:
            if type(node) is not dict or type(node.get("id")) is not str or not _ID.fullmatch(node["id"]) or node["id"] in ids:
                raise ValueError
            ids.add(node["id"])
        return obj
    except (ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError):
        _fail()


class _RootSnapshot:
    def __init__(self, files: AIEmployeeSafeFiles, workspace: Any | None, prefix: str, source: str) -> None:
        self.files, self.workspace, self.prefix, self.source = files, workspace, prefix, source
        self.directory_identity, self.rows = (None, []) if workspace is None else _entries(workspace, prefix)
        self.manifests: list[_Manifest] = []

    def scan(self, budget: list[int]) -> None:
        if self.workspace is None:
            return
        seen_ids = set()
        for row in self.rows:
            if row["kind"] != "directory":
                continue
            name = row["name"]
            if validate_relative("projects/" + name) != ("projects", name):
                _fail("storage_path_invalid")
            directory = self.prefix + "/" + name
            directory_identity = _directory_identity(self.workspace, directory)
            relative = directory + "/workflow.yaml"
            raw, identity = _read(self.workspace, relative)
            budget[0] += len(raw) if raw is not None else 0
            if budget[0] > MAX_TOTAL_BYTES:
                _fail()
            value = _decode(raw) if raw is not None else None
            if _directory_identity(self.workspace, directory) != directory_identity:
                _fail()
            if value is not None:
                workflow_id = value["id"].strip()
                if workflow_id in seen_ids:
                    _fail()  # Same-root ambiguity is never resolved by a filename.
                seen_ids.add(workflow_id)
            self.manifests.append(_Manifest(relative, raw, identity, directory_identity, value))

    def verify(self) -> None:
        if self.workspace is None:
            try:
                with self.files.workspace():
                    _fail()
            except FileNotFoundError:
                return
        current_identity, rows = _entries(self.workspace, self.prefix)
        if current_identity != self.directory_identity or rows != self.rows:
            _fail()
        for observed in self.manifests:
            if _directory_identity(self.workspace, observed.relative.rsplit("/", 1)[0]) != observed.directory_identity:
                _fail()
            raw, identity = _read(self.workspace, observed.relative)
            if raw != observed.raw or identity != observed.identity:
                _fail()


class SavedWorkflowBindingLoader:
    """Read only; constructors and missing reads never create any directories."""
    def __init__(self, *, repository_root: Path, ampuly_home: Path) -> None:
        if any(not isinstance(path, (Path, str)) or not Path(path).is_absolute() for path in (repository_root, ampuly_home)):
            _fail("storage_path_invalid")
        self.repository_root = Path(os.path.abspath(repository_root))
        self.ampuly_home = Path(os.path.abspath(ampuly_home))

    @contextmanager
    def load(self, project_id: str, workflow_id: str, node_id: str, *, expected_workflow_revision: str,
             capture_node_source: bool = False, expected_revision_kind: str = "raw") -> Iterator[SavedBindingSource]:
        if type(capture_node_source) is not bool:
            _fail("request_invalid")
        if type(expected_revision_kind) is not str or expected_revision_kind not in {"raw", "release"}:
            _fail("request_invalid")
        if expected_revision_kind == "release" and not capture_node_source:
            _fail("request_invalid")
        if any(type(value) is not str or not pattern.fullmatch(value) for value, pattern in (
                (project_id, _ID), (workflow_id, _FLOW), (node_id, _ID), (expected_workflow_revision, _SHA))):
            _fail("request_invalid")
        stack = ExitStack()
        snapshots = []
        source_active = False
        primary: BaseException | None = None
        try:
            for root, prefix, source in ((self.repository_root, "distribution/workflows", "distribution"),
                                         (self.ampuly_home, "workflows", "runtime")):
                files = AIEmployeeSafeFiles(root)
                try:
                    workspace = stack.enter_context(files.workspace())
                except FileNotFoundError:
                    workspace = None
                snapshots.append(_RootSnapshot(files, workspace, prefix, source))
            budget = [0]
            selected = None
            for snapshot in snapshots:
                snapshot.scan(budget)
                for manifest in snapshot.manifests:
                    if manifest.value is not None and manifest.value["id"].strip() == workflow_id:
                        selected = snapshot.source, manifest
            if selected is None:
                _fail("binding_unresolved")
            source, selected_manifest = selected
            manifest = selected_manifest.value
            # Apply registry precedence BEFORE scope; a private runtime override
            # must not expose the distribution version through a fallback.
            owner = (manifest.get("project_id") or "").strip()
            if owner and owner != project_id:
                _fail("binding_unresolved")
            revision = hashlib.sha256(selected_manifest.raw).hexdigest()
            if (revision if expected_revision_kind == "raw" else
                    hashlib.sha256(_canonical_bytes(selected_manifest.raw)).hexdigest()) != expected_workflow_revision:
                _fail("workflow_revision_conflict")
            node = next((row for row in manifest.get("nodes", []) if row["id"] == node_id), None)
            if node is None:
                _fail("binding_unresolved")
            if capture_node_source:
                node_canonical_bytes = canonical_json(node)
                node_snapshot = _capture_node_snapshot(
                    project_id=project_id, workflow_id=workflow_id, node_id=node_id,
                    binding_slot="executor", workflow_source=source,
                    manifest_raw_sha256=revision, manifest_bytes=selected_manifest.raw,
                    node_canonical_bytes=node_canonical_bytes,
                    workflow_version_present="version" in manifest,
                    workflow_version_raw=manifest.get("version"))
            else:
                canonical_json(node)
            try:
                payload = node_payload(node)
            except (ValueError, TypeError, KeyError, OverflowError):
                _fail()
            def verify_source() -> None:
                if not source_active:
                    _fail()
                for snapshot in snapshots:
                    snapshot.verify()

            source_active = True
            saved = SavedBindingSource(project_id, workflow_id, node_id, "executor",
                deepcopy(payload.get("employee_binding")), revision, source, manifest.get("version") or None,
                verify=verify_source,
                **({"node_snapshot": node_snapshot} if capture_node_source else {}))
            try:
                yield saved
            except BaseException as exc:
                primary = exc
                if isinstance(exc, Exception):
                    verify_source()
                raise
            else:
                verify_source()
        except WorkspaceBoundaryError:
            _fail("storage_path_invalid")
        except OSError:
            _fail()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            source_active = False
            # Close with a normal exit so each existing workspace context also
            # performs its configured-root identity check, even on a refusal.
            try:
                stack.close()
            except WorkspaceBoundaryError:
                if primary is None or isinstance(primary, Exception):
                    _fail("storage_path_invalid")
            except OSError:
                if primary is None or isinstance(primary, Exception):
                    _fail()
            except Exception:
                if primary is None or isinstance(primary, Exception):
                    _fail()
