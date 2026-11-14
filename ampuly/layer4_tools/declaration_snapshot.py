"""Private bounded observation of an EXISTING registry's declarations.

Standard library only; no I/O, engine imports, tool calls or implementation
inspection. This is not an execution-output validator, fingerprint/final row,
permission, initialization proof or EmployeeRun consumer. The two reads do
not provide an atomic snapshot/lease, detect ABA or prove subsequent loading.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

_MAX_TOOLS = 500
_MAX_SCHEMA_NODES = 50_000
_MAX_SCHEMA_BYTES = 2 * 1024 * 1024
_MAX_SCHEMA_DEPTH = 64
_MAX_INT_BITS = 4096
_MAX_FIELD_SCAN = 500  # Corrupt object metadata is not an unbounded read source.
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_MESSAGES = {
    "registry_declaration_source_invalid": "Registered declaration source is invalid.",
    "registry_declaration_names_invalid": "Explicit declaration names are invalid.",
    "registry_declaration_not_found": "An explicit declaration is unavailable.",
    "registry_declaration_limit": "Registered declaration observation exceeds its resource limit.",
    "registry_declaration_changed": "Registered declarations changed during observation.",
    "registry_declaration_observation_failed": "Registered declaration observation is unavailable.",
}


class RegistryDeclarationObservationError(ValueError):
    """Fixed, input-free diagnostics; no schema/default/path/callable echo."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "registry_declaration_observation_failed"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, slots=True)
class RegisteredToolDeclaration:
    name: str
    parameters_json: bytes = field(repr=False)
    return_schema_json: bytes | None = field(repr=False)
    gaps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegisteredDeclarationsSnapshot:
    epoch: int
    declarations: tuple[RegisteredToolDeclaration, ...]
    source: str = "registered_declarations"
    implementation_source: str = "unverified"
    dependency_source: str = "unverified"
    loaded_source: str = "unverified"


def _fail(code: str) -> None:
    raise RegistryDeclarationObservationError(code)


def _name(value: Any, code: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        _fail(code)
    return value


def _allowed(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) is not list:
        _fail("registry_declaration_names_invalid")
    initial_length = len(value)
    if initial_length > _MAX_TOOLS:
        _fail("registry_declaration_limit")
    names: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        # A live exact list can grow while iterated. Freeze its initial bound
        # and check EACH step before decoding/copying another name.
        if index >= _MAX_TOOLS:
            _fail("registry_declaration_limit")
        if index >= initial_length:
            _fail("registry_declaration_changed")
        name = _name(item, "registry_declaration_names_invalid")
        if name in seen:
            _fail("registry_declaration_names_invalid")
        seen.add(name)
        names.append(name)
    if len(value) != initial_length or len(names) != initial_length:
        _fail("registry_declaration_changed")
    return tuple(names)


def _fixed_fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """Read vars, not attributes/properties; never hash an unknown dict key."""
    values = vars(obj)
    if type(values) is not dict:
        _fail("registry_declaration_source_invalid")
    result: dict[str, Any] = {}
    try:
        for index, (key, value) in enumerate(values.items()):
            if index >= _MAX_FIELD_SCAN:
                _fail("registry_declaration_limit")
            if type(key) is not str:
                _fail("registry_declaration_source_invalid")
            if key in names:
                result[key] = value
                if len(result) == len(names):
                    return result
    except RuntimeError:
        _fail("registry_declaration_changed")
    _fail("registry_declaration_source_invalid")


@dataclass(slots=True)
class _Budget:
    nodes: int = 0
    encoded_bytes: int = 0

    def node(self, depth: int) -> None:
        if depth > _MAX_SCHEMA_DEPTH or self.nodes >= _MAX_SCHEMA_NODES:
            _fail("registry_declaration_limit")
        self.nodes += 1

    def bytes(self, count: int) -> None:
        if count > _MAX_SCHEMA_BYTES - self.encoded_bytes:
            _fail("registry_declaration_limit")
        self.encoded_bytes += count


def _string(value: str, budget: _Budget) -> str:
    # Account escaped UTF-8 BEFORE creating an encoded/escaped whole string.
    # Keys and values use the same JSON spelling as ensure_ascii=False dumps.
    if len(value) > _MAX_SCHEMA_BYTES - budget.encoded_bytes:
        _fail("registry_declaration_limit")
    budget.bytes(2)
    for char in value:
        point = ord(char)
        if point in (34, 92) or point in (8, 9, 10, 12, 13):
            size = 2
        elif point < 32:
            size = 6
        elif point < 128:
            size = 1
        elif point < 2048:
            size = 2
        elif 0xD800 <= point <= 0xDFFF:
            _fail("registry_declaration_source_invalid")
        elif point < 65536:
            size = 3
        else:
            size = 4
        budget.bytes(size)
    return value


def _clone(value: Any, budget: _Budget, depth: int, active: set[int], refs: list[bool]) -> Any:
    budget.node(depth)
    kind = type(value)
    if value is None:
        budget.bytes(4)
        return None
    if kind is bool:
        budget.bytes(4 if value else 5)
        return value
    if kind is str:
        return _string(value, budget)
    if kind is int:
        if value.bit_length() > _MAX_INT_BITS:
            _fail("registry_declaration_limit")
        budget.bytes(len(str(value)))
        return value
    if kind is float:
        if not math.isfinite(value):
            _fail("registry_declaration_source_invalid")
        budget.bytes(len(json.dumps(value, allow_nan=False)))
        return value
    if kind not in (dict, list):
        _fail("registry_declaration_source_invalid")
    identity = id(value)
    if identity in active:
        _fail("registry_declaration_source_invalid")
    active.add(identity)
    try:
        size_before = len(value)
        budget.bytes(2)
        if kind is list:
            cloned: Any = []
            for index, item in enumerate(value):
                if index:
                    budget.bytes(1)
                cloned.append(_clone(item, budget, depth + 1, active, refs))
        else:
            cloned = {}
            try:
                for index, (key, item) in enumerate(value.items()):
                    if type(key) is not str:
                        _fail("registry_declaration_source_invalid")
                    if index:
                        budget.bytes(1)
                    budget.node(depth + 1)  # Dictionary keys count as nodes.
                    _string(key, budget)
                    budget.bytes(1)
                    if key in ("$ref", "$dynamicRef", "$recursiveRef"):
                        refs[0] = True
                    cloned[key] = _clone(item, budget, depth + 1, active, refs)
            except RuntimeError:
                _fail("registry_declaration_changed")
        if len(value) != size_before or len(cloned) != size_before:
            _fail("registry_declaration_changed")
        return cloned
    finally:
        active.remove(identity)


def _schema(value: Any, budget: _Budget) -> tuple[bytes, bool]:
    if type(value) not in (dict, bool):
        _fail("registry_declaration_source_invalid")
    before = budget.encoded_bytes
    refs = [False]
    cloned = _clone(value, budget, 0, set(), refs)
    encoded = json.dumps(
        cloned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(encoded) != budget.encoded_bytes - before:
        _fail("registry_declaration_observation_failed")
    return encoded, refs[0]


def _head(registry: Any) -> tuple[dict, int]:
    fields = _fixed_fields(registry, ("_tools", "_declaration_epoch"))
    table, epoch = fields["_tools"], fields["_declaration_epoch"]
    if type(table) is not dict or type(epoch) is not int or epoch < 0:
        _fail("registry_declaration_source_invalid")
    return table, epoch


def _check_head(registry: Any, table: dict, epoch: int) -> None:
    current, revision = _head(registry)
    if current is not table or revision != epoch:
        _fail("registry_declaration_changed")


def _read(registry: Any, table: dict, epoch: int, names: tuple[str, ...] | None,
          tool_type: type, budget: _Budget) -> tuple[tuple[str, ...], tuple]:
    _check_head(registry, table, epoch)
    if names == ():
        _check_head(registry, table, epoch)
        return (), ()
    if len(table) > _MAX_TOOLS:
        _fail("registry_declaration_limit")
    selected: dict[str, Any] = {}
    keys: list[str] = []
    needed = None if names is None else set(names)
    try:
        for index, (key, definition) in enumerate(table.items()):
            if index >= _MAX_TOOLS:
                _fail("registry_declaration_limit")
            key = _name(key, "registry_declaration_source_invalid")
            keys.append(key)
            if needed is None or key in needed:
                selected[key] = definition
    except RuntimeError:
        _fail("registry_declaration_changed")
    if len(keys) != len(table):
        _fail("registry_declaration_changed")
    ordered = tuple(keys) if names is None else names
    rows: list[tuple[Any, Any, RegisteredToolDeclaration]] = []
    for name in ordered:
        if name not in selected:
            _fail("registry_declaration_not_found")
        definition = selected[name]
        if type(definition) is not tool_type:
            _fail("registry_declaration_source_invalid")
        fields = _fixed_fields(definition, ("name", "parameters", "return_schema", "func"))
        if _name(fields["name"], "registry_declaration_source_invalid") != name or not callable(fields["func"]):
            _fail("registry_declaration_source_invalid")
        parameters, p_refs = _schema(fields["parameters"], budget)
        declared_return = fields["return_schema"]
        gaps: list[str] = []
        if declared_return is None:
            returned, r_refs = None, False
            gaps.append("return_schema_unavailable")
        else:
            returned, r_refs = _schema(declared_return, budget)
        if p_refs or r_refs:
            gaps.append("schema_source_incomplete")
        row = RegisteredToolDeclaration(name, parameters, returned, tuple(gaps))
        rows.append((definition, fields["func"], row))
    _check_head(registry, table, epoch)
    return tuple(keys), tuple(rows)


def _observe_registered_declarations(registry: Any, *, registry_type: type,
                                     tool_type: type, allowed: Any = None) -> RegisteredDeclarationsSnapshot:
    """Called only by the existing registry's method, with its exact classes."""
    try:
        if type(registry) is not registry_type:
            _fail("registry_declaration_source_invalid")
        names = _allowed(allowed)
        table, epoch = _head(registry)
        budget = _Budget()  # Shared by all P/R and BOTH fixed reads.
        keys, first = _read(registry, table, epoch, names, tool_type, budget)
        other_keys, second = _read(registry, table, epoch, names, tool_type, budget)
        if keys != other_keys or len(first) != len(second):
            _fail("registry_declaration_changed")
        for left, right in zip(first, second):
            if left[0] is not right[0] or left[1] is not right[1] or left[2] != right[2]:
                _fail("registry_declaration_changed")
        return RegisteredDeclarationsSnapshot(epoch, tuple(row[2] for row in second))
    except RegistryDeclarationObservationError as error:
        code = error.code
    except Exception:
        code = "registry_declaration_observation_failed"
    # Outside the except suite: no original exception context or private error
    # is attached. Never stringify sources. BaseException propagates unchanged.
    raise RegistryDeclarationObservationError(code)
