"""Trusted, bounded employee access to two original native read tools.

Registry names and package declarations are not evidence. This adapter checks
the actual registered callable and metadata against the shipped factory, and
binds the loaded code, on-disk implementation and dependency versions. It does
not grant access: live project allowlist rules and the original task workspace
remain separate, current requirements. No registry-global mutation is used.
"""
from __future__ import annotations

import ast
import copy
from contextvars import ContextVar
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from types import CodeType, FunctionType

from ampuly.layer4_tools.registry import ToolDefinition, ToolRegistry
from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager
from ampuly.plugins.office_ui.ai_employee_contract import canonical_json
from ampuly.plugins.office_ui.employee_tool_fingerprint import (
    local_implementation_fingerprint, schema_fingerprint, tool_record_fingerprint,
)


READ_TOOLS = frozenset({"file_read", "list_dir"})
MAX_READ_BYTES = 256 * 1024
MAX_TRIAL_BYTES = 64 * 1024
_CURRENT: ContextVar["ReadScope | None"] = ContextVar("employee_read_tools", default=None)
_MODULES = (
    "ampuly.layer4_tools.file_ops", "ampuly.layer4_tools.workspace_fs",
    "ampuly.layer4_tools.workspace_fs_windows", "ampuly.layer4_tools.output_budget",
    "ampuly.layer4_tools.registry", "ampuly.layer5_memory.approval_allowlist",
    "ampuly.layer4_tools.employee_read_schemas",
)


def _fail(code):
    from ampuly.llm.budget import BudgetStopSurfaced
    raise BudgetStopSurfaced(code, code + ": 员工只读工具已停止；未扩大权限或改用其他工具。") from None


def _requirements(manifest):
    rows = manifest.get("capability_requirements") if type(manifest) is dict else None
    if type(rows) is not list or len(rows) > 2:
        _fail("employee_tool_implementation_unverified")
    names = []
    for row in rows:
        if (type(row) is not dict or row.get("tool") not in READ_TOOLS
                or row.get("actions") != ["execute"] or row["tool"] in names):
            _fail("employee_tool_implementation_unverified")
        names.append(row["tool"])
    return sorted(names)


def _code_bytes(code):
    # Remove installation path/line metadata from the loaded-code projection,
    # not instructions or constants. The full original source is hashed too.
    constants = tuple(_normalized_code(item) if type(item) is CodeType else item for item in code.co_consts)
    return marshal.dumps(code.replace(co_filename="", co_firstlineno=1, co_consts=constants))


def _normalized_code(code):
    return code.replace(co_filename="", co_firstlineno=1,
        co_consts=tuple(_normalized_code(item) if type(item) is CodeType else item for item in code.co_consts))


def _callables(module):
    result = {}
    for name, value in vars(module).items():
        if type(value) is FunctionType and value.__module__ == module.__name__:
            result[name] = value
        elif inspect.isclass(value) and value.__module__ == module.__name__:
            for member, method in vars(value).items():
                if type(method) in (classmethod, staticmethod):
                    method = method.__func__
                if type(method) is property:
                    method = method.fget
                if type(method) is FunctionType:
                    result[name + "." + member] = method
    return result


def _source_inventory():
    """Bounded inventory, no package tree search and no business/config reads.

    Both loaded code and original bytes participate. A stale loaded module is
    not represented merely by the newer file on disk. Source/loaded drift
    invalidates the previously evaluated fingerprint even before a restart.
    Python/stdlib and installed logging/model libraries are the declared TCB;
    this is not a claim to attest the operating system or arbitrary plugins.
    """
    modules = [importlib.import_module(name) for name in _MODULES]
    modules.append(sys.modules[__name__])
    files, loaded = {}, {}
    total = 0
    for module in modules:
        path = Path(module.__file__ or "")
        if path.suffix != ".py" or not path.is_absolute():
            _fail("employee_tool_implementation_unverified")
        # Do not follow source substitutions through symlinks or junctions.
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                _fail("employee_tool_implementation_unverified")
        size = path.stat().st_size
        if not 0 < size <= 1024 * 1024:
            _fail("employee_tool_implementation_unverified")
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        total += len(raw)
        if len(raw) != size or total > 4 * 1024 * 1024:
            _fail("employee_tool_implementation_unverified")
        ref = module.__name__.replace(".", "/") + ".py"
        files[ref] = raw
        methods = _callables(module)
        loaded[module.__name__] = {
            name: hashlib.sha256(_code_bytes(function.__code__)).hexdigest()
            for name, function in sorted(methods.items())
        }
        # Include literal source globals that are actually used as runtime
        # ceilings; a changed in-memory constant must not keep a green mark.
        tree = ast.parse(raw, filename=str(path))
        literals = {}
        for node in tree.body:
            if type(node) is not ast.Assign or len(node.targets) != 1 or type(node.targets[0]) is not ast.Name:
                continue
            try:
                declared = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            name = node.targets[0].id
            current = vars(module).get(name)
            if type(declared) in (str, int, bool, float, type(None)):
                if type(current) is not type(declared) or current != declared:
                    _fail("employee_tool_implementation_unverified")
                literals[name] = current
        loaded[module.__name__]["literals"] = literals
    # Imported implementation dependencies must be the observed module's
    # objects, not another callable installed under a familiar name.
    file_ops, workspace, _, output, registry, allowlist, schemas, this = modules
    if (file_ops.SecureWorkspace is not workspace.SecureWorkspace
            or file_ops.clip_text is not output.clip_text
            or registry.budget_tool_output is not output.budget_tool_output
            or file_ops.read_tool_return_schema is not schemas.read_tool_return_schema
            or this.ApprovalAllowlistManager is not allowlist.ApprovalAllowlistManager):
        _fail("employee_tool_implementation_unverified")
    files["loaded-code.json"] = canonical_json(loaded)
    versions = {"python": sys.version, "platform": sys.platform,
                "profile": "employee-builtin-read-v1"}
    for distribution in ("loguru", "pydantic", "PyYAML"):
        versions[distribution] = importlib.metadata.version(distribution)
    return files, versions


_META = ("name", "description", "parameters", "return_schema", "category",
         "requires_confirmation", "concurrency_safe", "read_only", "runtime_managed",
         "max_result_chars", "persist_large_results", "self_bounded_output", "preview_chars",
         "company_effect_kind", "permission_effects")


class BuiltinReadTools:
    def __init__(self, *, registry, ampuly_home, project_id):
        if type(registry) is not ToolRegistry or type(project_id) is not str or not project_id:
            _fail("employee_tool_implementation_unverified")
        self.registry, self.ampuly_home, self.project_id = registry, Path(ampuly_home), project_id

    def _definitions(self, names):
        from ampuly.layer4_tools.file_ops import create_file_tools
        shipped = {tool.name: tool for tool in create_file_tools() if tool.name in READ_TOOLS}
        found = {}
        for name in names:
            tool, expected = self.registry.get(name), shipped[name]
            if (type(tool) is not ToolDefinition or tool.func is not expected.func
                    or any(getattr(tool, key) != getattr(expected, key) for key in _META)
                    or tool.return_schema is None):
                _fail("employee_tool_implementation_unverified")
            # This is a local per-invocation view, never registry mutation.
            view = copy.copy(tool)
            view.persist_large_results = False
            view.self_bounded_output = True
            found[name] = view
        return found

    def observe(self, manifest):
        names = _requirements(manifest)
        if not names:
            return {"capability_impls": {}, "tool_inventory": {}, "tool_grants": {}, "authorization_valid": True}
        definitions = self._definitions(names)
        files, versions = _source_inventory()
        implementation = local_implementation_fingerprint(files, versions).fingerprint
        if implementation is None:
            _fail("employee_tool_implementation_unverified")
        capability_impls = {}
        grants = {}
        # Fresh manager avoids an mtime-only permission cache across calls.
        authority = ApprovalAllowlistManager(self.ampuly_home)
        for name, tool in definitions.items():
            schema = schema_fingerprint(tool.parameters, tool.return_schema).fingerprint
            record = {"tool": name, "impl": "builtin", "action": "execute",
                "schema_fingerprint": schema, "implementation_fingerprint": implementation,
                "remote_revision": None, "verifiable": True, "dependency_versions": versions}
            fingerprint = tool_record_fingerprint(record).fingerprint
            if fingerprint is None:
                _fail("employee_tool_implementation_unverified")
            capability_impls[name] = {"impl": "builtin", "version_or_fingerprint": fingerprint}
            if authority.list_patterns("tool", name, project_id=self.project_id):
                grants[name] = ["execute"]
        return {"capability_impls": capability_impls,
                "tool_inventory": {name: ["execute"] for name in names}, "tool_grants": grants,
                "authorization_valid": set(grants) == set(names)}

    def scope(self, *, task, manifest, checkpoint, trial_root=None):
        return ReadScope(self, task, manifest, checkpoint, trial_root=trial_root)

    def trial_scope(self, *, task, manifest, sample, checkpoint):
        names = _requirements(manifest)
        rows = sample.get("workspace_files") if type(sample) is dict else None
        if not names or type(rows) is not dict or not 1 <= len(rows) <= 8:
            _fail("evaluation_workspace_invalid")
        payload = {}
        total = 0
        for path, value in rows.items():
            relative = _relative(path, allow_dot=False)
            if type(value) is not str:
                _fail("evaluation_workspace_invalid")
            data = value.encode("utf-8")
            total += len(data)
            if total > MAX_TRIAL_BYTES or relative.casefold() in {key.casefold() for key in payload}:
                _fail("evaluation_workspace_invalid")
            payload[relative] = data
        # Parent exists and is an engine-owned home. Never accept a client
        # output path. Retained synthetic files are not real private knowledge.
        base = self.ampuly_home / "employee-trial-workspaces"
        if not self.ampuly_home.is_dir():
            _fail("evaluation_workspace_invalid")
        for part in (self.ampuly_home, *self.ampuly_home.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                _fail("evaluation_workspace_invalid")
        if base.exists():
            info = base.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                _fail("evaluation_workspace_invalid")
            # Bounded retained inventory; require explicit housekeeping when full.
            with os.scandir(base) as entries:
                if sum(1 for _, _entry in zip(range(257), entries)) > 256:
                    _fail("evaluation_workspace_retention_limit")
        else:
            base.mkdir()
        root = Path(tempfile.mkdtemp(prefix="case-", dir=base))
        from ampuly.layer4_tools.workspace_fs import SecureWorkspace
        with SecureWorkspace.for_roots(str(root), str(root)) as workspace:
            if type(workspace).__name__ == "LexicalWorkspace":
                _fail("employee_workspace_atomic_boundary_required")
            for relative, data in payload.items():
                workspace.write_text(workspace.resolve(relative), data.decode("utf-8"), create_dirs=True)
        task.metadata = {**task.metadata, "workspace_root": str(root), "output_root": str(root)}
        return self.scope(task=task, manifest=manifest, checkpoint=checkpoint, trial_root=root)


def _relative(value, *, allow_dot=True):
    if type(value) is not str or not 1 <= len(value) <= 512 or "\\" in value or ":" in value or "\x00" in value:
        _fail("employee_tool_path_denied")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("~"):
        _fail("employee_tool_path_denied")
    normalized = str(path)
    if (not allow_dot and normalized == ".") or any(
            part.startswith(".") or part.endswith((" ", ".")) for part in path.parts):
        _fail("employee_tool_path_denied")
    return normalized


class ReadScope:
    def __init__(self, host, task, manifest, checkpoint, *, trial_root=None):
        if not callable(checkpoint) or getattr(task, "project_id", None) != host.project_id:
            _fail("employee_scope_invalid")
        self.host, self.task, self.manifest = host, task, copy.deepcopy(manifest)
        self.checkpoint, self.trial_root = checkpoint, trial_root
        self.observation = host.observe(self.manifest)
        self.definitions = host._definitions(_requirements(manifest))
        self._token = None
        from ampuly.layer4_tools.workspace_fs import workspace_roots_for_task
        self.roots = workspace_roots_for_task(task)
        if not self.roots[0] or not self.roots[1]:
            # The original Harness fills a Company task's workspace before
            # its first tool. Never invent a fallback root here.
            self.roots = None

    @property
    def names(self):
        return list(self.definitions)

    def __enter__(self):
        if _CURRENT.get() is not None or self._token is not None:
            _fail("employee_scope_invalid")
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, *_):
        _CURRENT.reset(self._token)

    async def prepare(self, registry, tool, arguments, task):
        if registry is not self.host.registry or task is not self.task or tool.name not in self.definitions:
            _fail("employee_tool_denied")
        await self.checkpoint()
        current = self.host.observe(self.manifest)
        if current["capability_impls"] != self.observation["capability_impls"]:
            _fail("employee_tool_implementation_changed")
        from ampuly.layer4_tools.workspace_fs import SecureWorkspace, workspace_roots_for_task
        if self.roots is None:
            self.roots = workspace_roots_for_task(task)
            if not self.roots[0] or not self.roots[1]:
                _fail("employee_workspace_unavailable")
        if workspace_roots_for_task(task) != self.roots:
            _fail("employee_workspace_changed")
        if type(arguments) is not dict:
            _fail("employee_tool_arguments_invalid")
        allowed = {"path", "offset", "limit", "include_line_numbers"} if tool.name == "file_read" else {"path", "recursive", "max_depth"}
        if set(arguments) - allowed:
            _fail("employee_tool_arguments_invalid")
        path = _relative(arguments.get("path", "."), allow_dot=tool.name == "list_dir")
        if tool.name == "file_read":
            if (type(arguments.get("offset", 0)) is not int or not 0 <= arguments.get("offset", 0) <= 100000
                    or arguments.get("limit") is not None and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 1000)
                    or type(arguments.get("include_line_numbers", False)) is not bool):
                _fail("employee_tool_arguments_invalid")
        elif (type(arguments.get("recursive", False)) is not bool
                or type(arguments.get("max_depth", 3)) is not int or not 1 <= arguments.get("max_depth", 3) <= 3):
            _fail("employee_tool_arguments_invalid")
        if self.trial_root is None:
            authority = ApprovalAllowlistManager(self.host.ampuly_home)
            if not authority.is_allowed("tool", tool.name, [path], project_id=self.host.project_id)[0]:
                _fail("employee_tool_denied")
        elif self.roots != (str(self.trial_root), str(self.trial_root)):
            _fail("employee_workspace_changed")
        with SecureWorkspace.for_task(task) as workspace:
            if type(workspace).__name__ == "LexicalWorkspace":
                _fail("employee_workspace_atomic_boundary_required")
            workspace.resolve(path)  # Also refuses runtime-internal namespaces.
        await self.checkpoint()
        # There is no await between return and the original callable's I/O.
        return self.definitions[tool.name]


async def prepare_employee_read_call(registry, tool, arguments, task):
    scope = _CURRENT.get()
    if scope is None:
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        if current_employee_turn(task) is not None:
            _fail("employee_tool_scope_unavailable")
        return tool
    return await scope.prepare(registry, tool, arguments, task)


def employee_read_byte_limit(task):
    scope = _CURRENT.get()
    if scope is None:
        return None
    if task is not scope.task:
        _fail("employee_scope_invalid")
    return MAX_READ_BYTES


def current_read_tool_schemas():
    scope = _CURRENT.get()
    return [] if scope is None else [scope.definitions[name].to_schema() for name in scope.names]
