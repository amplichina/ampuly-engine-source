"""A bounded interpreter for generated declarative connector adapters.

Generated candidates never contribute Python code.  This runtime turns one
validated operation into a request description, resolves only the declared
environment references, and hands the request to an injected transport.
Writes require an explicit grant and never retry an ambiguous result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .connector_contract import ConnectorContract, WRITE


class ConnectorRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectorRequest:
    method: str
    url: str
    headers: dict[str, str]
    timeout_sec: float
    body: dict[str, Any] | None
    op_id: str
    effect: str


@dataclass(frozen=True)
class ConnectorResponse:
    status: str
    status_code: int | None = None
    body: Any = None
    detail: str = ""


Transport = Callable[[ConnectorRequest], tuple[int, str]]


class DeclarativeConnectorAdapter:
    def __init__(self, contract: ConnectorContract) -> None:
        self.contract = contract
        self._operations = {op.op_id: op for op in contract.operations}

    def prepare(
        self,
        op_id: str,
        params: Mapping[str, Any],
        *,
        environment: Mapping[str, str],
        write_authorized: bool = False,
        idempotency_key: str = "",
    ) -> ConnectorRequest:
        op = self._operations.get(str(op_id or "").strip())
        if op is None:
            raise ConnectorRuntimeError("unknown connector operation")
        if op.kind == WRITE and not write_authorized:
            raise ConnectorRuntimeError("write operation requires explicit authorization")
        base = str(environment.get(self.contract.base_url_env, "") or "").strip()
        split = urlsplit(base)
        if split.scheme not in {"http", "https"} or not split.netloc:
            raise ConnectorRuntimeError(f"missing or invalid environment reference {self.contract.base_url_env}")
        if split.username or split.password or split.query or split.fragment:
            raise ConnectorRuntimeError("base URL must not contain credentials, query, or fragment")
        values = {str(key): value for key, value in dict(params or {}).items()}
        path = op.path
        consumed: set[str] = set()
        for name in op.params:
            marker = "{" + name + "}"
            if marker in path:
                if name not in values:
                    raise ConnectorRuntimeError(f"missing path parameter {name}")
                path = path.replace(marker, quote(str(values[name]), safe=""))
                consumed.add(name)
        unknown = set(values) - set(op.params)
        if unknown:
            raise ConnectorRuntimeError(f"undeclared parameters: {', '.join(sorted(unknown))}")
        remainder = {name: values[name] for name in op.params if name in values and name not in consumed}
        base_path = split.path.rstrip("/")
        url = urlunsplit((split.scheme, split.netloc, base_path + path, "", ""))
        body: dict[str, Any] | None = None
        if op.method == "GET" and remainder:
            url += "?" + urlencode(remainder, doseq=True)
        elif remainder:
            body = remainder
        headers = self._auth_headers(environment)
        if op.kind == WRITE:
            if op.idempotency.startswith("key:"):
                header = op.idempotency.split(":", 1)[1].strip()
                if not header or not str(idempotency_key or "").strip():
                    raise ConnectorRuntimeError("write requires an idempotency key")
                headers[header] = str(idempotency_key).strip()
            if body is not None:
                headers["Content-Type"] = "application/json"
        return ConnectorRequest(op.method, url, headers, op.timeout_sec, body, op.op_id, op.kind)

    def execute(
        self,
        op_id: str,
        params: Mapping[str, Any],
        *,
        environment: Mapping[str, str],
        transport: Transport,
        write_authorized: bool = False,
        idempotency_key: str = "",
    ) -> ConnectorResponse:
        request = self.prepare(
            op_id, params, environment=environment,
            write_authorized=write_authorized, idempotency_key=idempotency_key,
        )
        op = self._operations[request.op_id]
        try:
            status_code, raw = transport(request)
        except Exception as exc:  # noqa: BLE001 - ambiguity is part of the result contract
            if op.kind == WRITE:
                return ConnectorResponse(
                    status="outcome_unknown",
                    detail=f"{type(exc).__name__}; policy={op.unknown_outcome}",
                )
            raise ConnectorRuntimeError(f"read transport failed: {type(exc).__name__}") from exc
        body = _decode_body(raw)
        if 200 <= int(status_code) < 300:
            return ConnectorResponse("succeeded", int(status_code), body)
        if int(status_code) == op.rate_limit_status:
            return ConnectorResponse("rate_limited", int(status_code), body)
        mapped = op.error_map.get(str(status_code), "")
        return ConnectorResponse("failed", int(status_code), body, mapped or f"HTTP {status_code}")

    def _auth_headers(self, environment: Mapping[str, str]) -> dict[str, str]:
        if self.contract.auth_type == "none":
            return {}
        value = str(environment.get(self.contract.credential_env, "") or "")
        if not value:
            raise ConnectorRuntimeError(f"missing credential reference {self.contract.credential_env}")
        if self.contract.auth_type == "bearer_env":
            return {"Authorization": f"Bearer {value}"}
        if self.contract.auth_type == "header_env":
            return {"X-Api-Key": value}
        return {"Authorization": f"Basic {value}"}


def _decode_body(raw: Any) -> Any:
    if not isinstance(raw, str) or not raw.strip():
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
