"""A connector's contract, and the tests a candidate must pass against a local sample.

Plan §10.2 (W07-X): a connector is generated from a formal contract, not
conjured for an arbitrary platform. Before any generation exists there has to
be something to generate *to* -- a shape that says what the connector reads
and writes, how it authenticates without carrying a secret, how it pages,
what a rate limit or a timeout means, and what it does when a write returns
no proof either way. This module is that shape, its validation, its
fingerprint, and the contract tests every candidate runs against a local
sample service before anyone talks about admitting it (W12-X).

Three rules are enforced here rather than described:

- **No secret value in a contract.** Credentials are referenced by the name
  of an environment variable; a literal that looks like a key is refused.
- **A write is a write.** Every operation is classified read or write; a
  write declares idempotency and an unknown-outcome policy, and the runner
  will not call one without both.
- **The sample is the test target.** `run_contract_tests` speaks to whatever
  base URL it is given through an injected client; nothing here reaches a
  production host, reads a production credential, or registers a tool.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

READ = "read"
WRITE = "write"

# Values that look like a secret rather than a reference to one. Refused in
# any string field of a contract, because a generated file that carries a
# key is a leak the moment it is shared.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}"),
    re.compile(r"\bAKIA[A-Z0-9]{12,}"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
    # Deliberately no generic "long base64-ish run" rule: a sha256 fingerprint
    # in `source` would trip it, and a refusal that fires on evidence gets an
    # exemption list. The named shapes above are the ones that leak.
)

UNKNOWN_OUTCOME_POLICIES = frozenset({"hold_and_verify", "retry_if_idempotent", "fail_closed"})
AUTH_TYPES = frozenset({"none", "bearer_env", "header_env", "basic_env"})


class ContractError(ValueError):
    """A contract that cannot be built from is not silently built from."""


@dataclass(frozen=True)
class Operation:
    op_id: str
    kind: str
    method: str
    path: str
    summary: str = ""
    params: tuple[str, ...] = ()
    timeout_sec: float = 10.0
    pagination: str = ""            # "" | "cursor:<param>" | "page:<param>"
    rate_limit_status: int = 429
    error_map: dict[str, str] = field(default_factory=dict)
    idempotency: str = ""           # writes: "key:<header>" | "natural" | ""
    unknown_outcome: str = ""       # writes: one of UNKNOWN_OUTCOME_POLICIES

    def to_payload(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id, "kind": self.kind, "method": self.method, "path": self.path,
            "summary": self.summary, "params": list(self.params), "timeout_sec": self.timeout_sec,
            "pagination": self.pagination, "rate_limit_status": self.rate_limit_status,
            "error_map": dict(self.error_map), "idempotency": self.idempotency,
            "unknown_outcome": self.unknown_outcome,
        }


@dataclass(frozen=True)
class ConnectorContract:
    name: str
    version: str
    base_url_env: str
    auth_type: str
    credential_env: str
    operations: tuple[Operation, ...]
    source: str = ""                # where the protocol came from (doc/schema), never a secret
    provenance: str = "authored"    # authored | generated

    @property
    def reads(self) -> tuple[Operation, ...]:
        return tuple(op for op in self.operations if op.kind == READ)

    @property
    def writes(self) -> tuple[Operation, ...]:
        return tuple(op for op in self.operations if op.kind == WRITE)

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "base_url_env": self.base_url_env,
            "auth": {"type": self.auth_type, "credential_env": self.credential_env},
            "operations": [op.to_payload() for op in self.operations],
            "source": self.source, "provenance": self.provenance,
        }

    def fingerprint(self) -> str:
        """Stable over the contract's content; what admission and version locks bind to."""
        canonical = json.dumps(self.to_payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _refuse_secret_shapes(value: Any, *, where: str) -> None:
    if isinstance(value, str):
        for shape in _SECRET_SHAPES:
            if shape.search(value):
                raise ContractError(f"{where}: looks like a secret value; reference a credential by environment name instead")
    elif isinstance(value, dict):
        for key, item in value.items():
            _refuse_secret_shapes(item, where=f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_secret_shapes(item, where=f"{where}[{index}]")


def from_payload(payload: dict[str, Any]) -> ConnectorContract:
    """Validate an untrusted contract payload. Refuses rather than fills."""
    if not isinstance(payload, dict):
        raise ContractError("contract must be an object")
    _refuse_secret_shapes(payload, where="contract")
    name = str(payload.get("name", "") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        raise ContractError("name must be lowercase identifier-like")
    version = str(payload.get("version", "") or "").strip()
    if not version:
        raise ContractError("version is required")
    base_url_env = str(payload.get("base_url_env", "") or "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", base_url_env):
        raise ContractError("base_url_env must name an environment variable, not carry a URL")
    auth = dict(payload.get("auth", {}) or {})
    auth_type = str(auth.get("type", "") or "").strip()
    if auth_type not in AUTH_TYPES:
        raise ContractError(f"auth.type must be one of {sorted(AUTH_TYPES)}")
    credential_env = str(auth.get("credential_env", "") or "").strip()
    if auth_type != "none" and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", credential_env):
        raise ContractError("auth.credential_env must name an environment variable")
    if auth_type == "none" and credential_env:
        raise ContractError("auth.type none must not name a credential")
    raw_ops = payload.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise ContractError("operations must be a non-empty list")
    ops: list[Operation] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_ops):
        if not isinstance(raw, dict):
            raise ContractError(f"operations[{index}] must be an object")
        op_id = str(raw.get("op_id", "") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", op_id) or op_id in seen:
            raise ContractError(f"operations[{index}]: op_id must be unique and identifier-like")
        seen.add(op_id)
        kind = str(raw.get("kind", "") or "").strip()
        if kind not in {READ, WRITE}:
            raise ContractError(f"{op_id}: kind must be read or write")
        method = str(raw.get("method", "") or "").strip().upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ContractError(f"{op_id}: method must be an HTTP method")
        if kind == READ and method != "GET":
            raise ContractError(f"{op_id}: a read must be GET; a {method} is a write")
        if kind == WRITE and method == "GET":
            raise ContractError(f"{op_id}: a write cannot be GET")
        path = str(raw.get("path", "") or "").strip()
        if not path.startswith("/") or "://" in path:
            raise ContractError(f"{op_id}: path must be relative to the base URL")
        try:
            timeout = float(raw.get("timeout_sec", 10.0) or 10.0)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{op_id}: timeout_sec must be a number") from exc
        if not 0 < timeout <= 120:
            raise ContractError(f"{op_id}: timeout_sec must be within (0, 120]")
        idempotency = str(raw.get("idempotency", "") or "").strip()
        unknown = str(raw.get("unknown_outcome", "") or "").strip()
        if kind == WRITE:
            if not idempotency:
                raise ContractError(f"{op_id}: a write must declare idempotency (key:<header> or natural)")
            if unknown not in UNKNOWN_OUTCOME_POLICIES:
                raise ContractError(f"{op_id}: a write must declare unknown_outcome as one of {sorted(UNKNOWN_OUTCOME_POLICIES)}")
        pagination = str(raw.get("pagination", "") or "").strip()
        if pagination and not re.fullmatch(r"(cursor|page):[a-zA-Z_][a-zA-Z0-9_]*", pagination):
            raise ContractError(f"{op_id}: pagination must be cursor:<param> or page:<param>")
        error_map = {str(k): str(v) for k, v in dict(raw.get("error_map", {}) or {}).items()}
        ops.append(Operation(
            op_id=op_id, kind=kind, method=method, path=path,
            summary=str(raw.get("summary", "") or "").strip(),
            params=tuple(str(p) for p in list(raw.get("params", []) or [])),
            timeout_sec=timeout, pagination=pagination,
            rate_limit_status=int(raw.get("rate_limit_status", 429) or 429),
            error_map=error_map, idempotency=idempotency, unknown_outcome=unknown,
        ))
    return ConnectorContract(
        name=name, version=version, base_url_env=base_url_env, auth_type=auth_type,
        credential_env=credential_env, operations=tuple(ops),
        source=str(payload.get("source", "") or "").strip(),
        provenance=str(payload.get("provenance", "authored") or "authored").strip(),
    )


# ── contract tests against a local sample ──────────────────────────────────
#
# The runner never builds an HTTP client of its own: it is handed one, so a
# test can hand it a local server and a product can hand it a fenced one.
# The client contract is small: call(method, url, *, headers, timeout) ->
# (status, body_text). A timeout is reported by raising TimeoutError.

HttpClient = Callable[..., tuple[int, str]]


@dataclass
class ContractTestResult:
    op_id: str
    check: str
    passed: bool
    detail: str = ""


def run_contract_tests(
    contract: ConnectorContract,
    *,
    base_url: str,
    client: HttpClient,
    credential: str = "",
    write_authorized: bool = False,
) -> list[ContractTestResult]:
    """The five checks the plan names, per operation, against the sample.

    normal, auth failure, rate limit, timeout, and -- for writes -- the
    unknown-receipt policy. A write is exercised only when the caller says
    it is authorized; otherwise its checks are reported as skipped, which is
    a fail: the plan wants an approval-gated write verified, not assumed.
    """
    results: list[ContractTestResult] = []
    good_headers = _auth_headers(contract, credential)
    bad_headers = _auth_headers(contract, "WRONG") if contract.auth_type != "none" else {}
    for op in contract.operations:
        url = base_url.rstrip("/") + op.path
        if op.kind == WRITE and not write_authorized:
            results.append(ContractTestResult(op.op_id, "write_authorized", False, "write not authorized for this run; not exercised"))
            continue
        # normal
        try:
            status, body = client(op.method, url, headers=good_headers, timeout=op.timeout_sec)
            ok = 200 <= status < 300
            results.append(ContractTestResult(op.op_id, "normal", ok, f"status {status}"))
        except TimeoutError:
            results.append(ContractTestResult(op.op_id, "normal", False, "timed out on the normal call"))
        except Exception as exc:  # noqa: BLE001 - a test result, not a raise
            results.append(ContractTestResult(op.op_id, "normal", False, f"{type(exc).__name__}: {exc}"))
        # auth failure must be refused, not served
        if contract.auth_type != "none":
            try:
                status, _ = client(op.method, url, headers=bad_headers, timeout=op.timeout_sec)
                results.append(ContractTestResult(op.op_id, "auth_failure", status in (401, 403), f"status {status}"))
            except Exception as exc:  # noqa: BLE001
                results.append(ContractTestResult(op.op_id, "auth_failure", False, f"{type(exc).__name__}: {exc}"))
        # rate limit is recognised, not mistaken for success
        try:
            status, _ = client(op.method, url, headers={**good_headers, "X-Sample-Scenario": "rate_limit"}, timeout=op.timeout_sec)
            results.append(ContractTestResult(op.op_id, "rate_limit", status == op.rate_limit_status, f"status {status}"))
        except Exception as exc:  # noqa: BLE001
            results.append(ContractTestResult(op.op_id, "rate_limit", False, f"{type(exc).__name__}: {exc}"))
        # timeout is a timeout, surfaced as one
        try:
            client(op.method, url, headers={**good_headers, "X-Sample-Scenario": "hang"}, timeout=min(op.timeout_sec, 0.5))
            results.append(ContractTestResult(op.op_id, "timeout", False, "the call returned instead of timing out"))
        except TimeoutError:
            results.append(ContractTestResult(op.op_id, "timeout", True, "TimeoutError surfaced"))
        except Exception as exc:  # noqa: BLE001
            results.append(ContractTestResult(op.op_id, "timeout", False, f"{type(exc).__name__}: {exc}"))
        # writes: a lost receipt is handled by the declared policy, never retried blindly
        if op.kind == WRITE:
            try:
                status, body = client(op.method, url, headers={**good_headers, "X-Sample-Scenario": "unknown_receipt"}, timeout=op.timeout_sec)
                handled = status >= 500 or not body.strip()
                results.append(ContractTestResult(
                    op.op_id, "unknown_receipt",
                    handled and op.unknown_outcome in UNKNOWN_OUTCOME_POLICIES,
                    f"status {status}; policy {op.unknown_outcome or 'undeclared'}",
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(ContractTestResult(op.op_id, "unknown_receipt", op.unknown_outcome in UNKNOWN_OUTCOME_POLICIES, f"{type(exc).__name__} treated by policy {op.unknown_outcome}"))
    return results


def _auth_headers(contract: ConnectorContract, credential: str) -> dict[str, str]:
    if contract.auth_type == "none":
        return {}
    if contract.auth_type == "bearer_env":
        return {"Authorization": f"Bearer {credential}"}
    if contract.auth_type == "header_env":
        return {"X-Api-Key": credential}
    return {"Authorization": f"Basic {credential}"}


def summarize(results: list[ContractTestResult]) -> dict[str, Any]:
    failed = [r for r in results if not r.passed]
    return {
        "checks": len(results),
        "passed": len(results) - len(failed),
        "failed": [{"op_id": r.op_id, "check": r.check, "detail": r.detail} for r in failed],
        "ok": not failed,
    }


# ── the local sample every candidate is tested against ─────────────────────


def sample_client(*, credential: str, hang_seconds: float = 2.0) -> HttpClient:
    """An in-process stand-in for a sample HTTP service.

    Honours the same scenario header a real local sample server would, so a
    contract test written against this passes against the server and vice
    versa. No sockets: the plan wants the candidate exercised, not the
    network stack.
    """

    def call(method: str, url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> tuple[int, str]:
        headers = dict(headers or {})
        scenario = headers.get("X-Sample-Scenario", "")
        auth = headers.get("Authorization", "") or headers.get("X-Api-Key", "")
        if scenario == "hang":
            if hang_seconds > timeout:
                raise TimeoutError(f"no reply within {timeout}s")
            time.sleep(min(hang_seconds, 0.01))
        if auth and credential not in auth:
            return 401, json.dumps({"error": "unauthorized"})
        if scenario == "rate_limit":
            return 429, json.dumps({"error": "slow down"})
        if scenario == "unknown_receipt":
            return 502, ""
        if method == "GET":
            return 200, json.dumps({"items": [{"id": "1"}], "next": None})
        return 201, json.dumps({"id": "new-1", "receipt": "r-1"})

    return call
