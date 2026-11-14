"""Pure machine validation of employee output, shared by execution entries.

No publication, authorization, file write or acceptance takes place here. A
valid result is only a list of named contents ready for the existing delivery
writer. Schema references cannot retrieve external resources.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any


class EmployeeOutputContractError(RuntimeError):
    """Stable public code without leaking model output or schema internals."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise EmployeeOutputContractError(code)


def output_validator(manifest: dict[str, Any]):
    """Return a detached output schema and an offline Draft 2020-12 validator."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    if type(manifest) is not dict or type(manifest.get("output_schema", {})) is not dict:
        _refuse("employee_output_contract_invalid")
    source = manifest.get("output_schema", {})
    pending, count = [(source, 0)], 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 32 or count > 10000:
            _refuse("employee_output_contract_invalid")
        if type(value) is dict:
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in value and (type(value[key]) is not str or not value[key].startswith("#")):
                    _refuse("employee_output_contract_invalid")
            pending.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            pending.extend((child, depth + 1) for child in value)

    def no_remote(uri):
        raise NoSuchResource(ref=uri)

    try:
        schema = deepcopy(source)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=Registry(retrieve=no_remote))
    except Exception:
        _refuse("employee_output_contract_invalid")
    return schema, validator


def _canonical_body(value: Any) -> str:
    """Keep the previous employee JSON bounds without importing its plugin."""
    active: set[int] = set()
    remaining, characters = 50000, 2 * 1024 * 1024

    def walk(node, depth):
        nonlocal remaining, characters
        remaining -= 1
        if depth > 64 or remaining < 0:
            _refuse("employee_output_contract_failed")
        if node is None or type(node) in (str, bool, int):
            if type(node) is str:
                characters -= len(node)
                if characters < 0:
                    _refuse("employee_output_contract_failed")
            if type(node) is int and node.bit_length() > 256:
                _refuse("employee_output_contract_failed")
            return
        if type(node) is float:
            if not math.isfinite(node):
                _refuse("employee_output_contract_failed")
            return
        if type(node) not in (dict, list) or id(node) in active:
            _refuse("employee_output_contract_failed")
        active.add(id(node))
        try:
            if type(node) is dict:
                for key, child in node.items():
                    if type(key) is not str:
                        _refuse("employee_output_contract_failed")
                    characters -= len(key)
                    if characters < 0:
                        _refuse("employee_output_contract_failed")
                    walk(child, depth + 1)
            else:
                for child in node:
                    walk(child, depth + 1)
        finally:
            active.remove(id(node))

    walk(value, 0)
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(body.encode("utf-8")) > 2 * 1024 * 1024:
            _refuse("employee_output_contract_failed")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _refuse("employee_output_contract_failed")
    return body


def checked_output(manifest: dict[str, Any], content: str, validator) -> list[dict[str, str]]:
    """Check schema and every required named output; this is not human acceptance."""
    if type(manifest) is not dict:
        _refuse("employee_output_contract_invalid")
    if type(content) is not str or not content.strip():
        _refuse("employee_output_contract_failed")
    try:
        if len(content.encode("utf-8")) > 1024 * 1024:
            _refuse("employee_output_contract_failed")
    except UnicodeError:
        _refuse("employee_output_contract_failed")
    try:
        value = json.loads(content, parse_constant=lambda _: _refuse("employee_output_contract_failed"))
    except (ValueError, RecursionError):
        value = content
    try:
        valid = validator.is_valid(value)
    except Exception:
        _refuse("employee_output_contract_invalid")
    if valid is not True:
        _refuse("employee_output_contract_failed")
    contract = manifest.get("artifact_contract", {})
    rows = contract.get("deliverables", []) if type(contract) is dict else None
    if type(rows) is not list or len(rows) > 32 or any(type(row) is not dict for row in rows):
        _refuse("employee_output_contract_invalid")
    required = [row for row in rows if row.get("required") is True]
    if not required:
        return [{"name": "employee-output.txt", "format": "text", "content": content}]
    result, names = [], set()
    for delivery in required:
        name, format_ = delivery.get("name"), str(delivery.get("format", "")).lower()
        if type(name) is not str or not name.strip() or len(name) > 256 or name in names:
            _refuse("employee_output_contract_invalid")
        names.add(name)
        if format_ not in {"text", "txt", "markdown", "md", "json", "text/plain", "text/markdown", "application/json"}:
            _refuse("employee_output_format_unsupported")
        item = value if len(required) == 1 else value.get(name) if type(value) is dict else None
        if item is None:
            _refuse("employee_output_contract_failed")
        if format_ in {"json", "application/json"}:
            try:
                structured = json.loads(item) if type(item) is str else item
                body = _canonical_body(structured)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                _refuse("employee_output_contract_failed")
        else:
            body = item if type(item) is str else _canonical_body(item)
        if not body.strip():
            _refuse("employee_output_contract_failed")
        result.append({"name": name, "format": format_, "content": body})
    return result
