"""Host-bound recipient NDJSON reads over an actual bounded HTTP transport.

No send, retry, grant loading, account lookup or task creation happens here.
The trusted host registers the returned existing ReadbackAdapter. HTTPS and a
host-supplied bearer are required; explicit loopback test mode permits only
literal loopback addresses for a synthetic recipient, never arbitrary HTTP.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import time
from urllib.parse import urlsplit

import httpx

from ampuly.core.external_agent_readback import (
    ExternalAgentReadbackError, ReadbackAdapter, ReadbackBatch, ReadbackPolicy, ReadbackRequest,
)


def _refuse(code="invalid_readback_adapter"):
    raise ExternalAgentReadbackError(code)


@dataclass(frozen=True)
class RecipientHTTPScope:
    principal_id: str
    project_id: str
    channel: str
    user_id: str
    session_id: str

    def __post_init__(self):
        for value in vars(self).values():
            if (type(value) is not str or not value or value != value.strip()
                    or len(value.encode("utf-8")) > 512 or any(ord(c) < 32 for c in value)):
                _refuse()


def http_recipient_readback(origin: str, scope: RecipientHTTPScope, *, bearer_token: str,
                            policy: ReadbackPolicy | None = None,
                            allow_loopback_test: bool = False) -> ReadbackAdapter:
    """Build a host-owned reader for ``/recipient-readback/v1``.

    Query scope and limits must be applied by the trusted recipient endpoint
    before enumeration. Each UTF-8 NDJSON line is exactly {"row": <record>};
    the final line is {"complete": true|false}. A prefix or missing footer is
    never proof. The client additionally bounds the raw stream before JSON
    materialization and the existing readback contract validates each record.
    """
    if type(scope) is not RecipientHTTPScope or type(origin) is not str or type(allow_loopback_test) is not bool:
        _refuse()
    try:
        url = urlsplit(origin)
        _ = url.port
        loopback = bool(url.hostname and ipaddress.ip_address(url.hostname).is_loopback)
    except ValueError:
        loopback = False
        try:
            url = urlsplit(origin)
            _ = url.port
        except ValueError:
            _refuse()
    if (origin != origin.strip() or url.username is not None or url.password is not None
            or not url.hostname or url.path not in ("", "/") or url.query or url.fragment
            or any(ord(c) < 32 for c in origin)
            or (url.scheme != "https" and not (url.scheme == "http" and loopback and allow_loopback_test))):
        _refuse()
    if (type(bearer_token) is not str or bearer_token != bearer_token.strip()
            or len(bearer_token.encode("utf-8")) > 4096 or any(ord(c) < 32 for c in bearer_token)
            or (not bearer_token and not (loopback and allow_loopback_test))):
        _refuse()
    endpoint = origin.rstrip("/") + "/recipient-readback/v1"
    selected_policy = policy if policy is not None else ReadbackPolicy()
    if type(selected_policy) is not ReadbackPolicy:
        _refuse("invalid_readback_policy")
    selected_scope = tuple(vars(scope).items())

    async def read(request: ReadbackRequest) -> ReadbackBatch:
        if (type(request) is not ReadbackRequest or request.policy != selected_policy
                or any(getattr(request, name) != value for name, value in selected_scope)):
            _refuse("invalid_readback_request")
        deadline = min(request.deadline_monotonic, time.monotonic() + selected_policy.deadline_s)
        def seconds_left():
            return max(0.0, deadline - time.monotonic())
        remaining = seconds_left()
        if remaining <= 0:
            _refuse("external_agent_readback_timeout")
        params = {name: getattr(request, name) for name in (
            "effect_id", "principal_id", "project_id", "channel", "user_id", "session_id",
            "task_ref", "artifact_sha256", "binding_sha256", "protocol")}
        params.update(max_rows=str(request.policy.max_rows), max_row_bytes=str(request.policy.max_row_bytes),
                      max_total_bytes=str(request.policy.max_total_bytes), deadline_ms=str(max(1, int(remaining * 1000))))
        headers = {"Accept": "application/x-ndjson", "Accept-Encoding": "identity"}
        if bearer_token:
            headers["Authorization"] = "Bearer " + bearer_token
        rows, buffer, consumed, footer = [], bytearray(), 0, None

        def unique(pairs):
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError("duplicate JSON key")
            return value

        def line(raw):
            nonlocal footer
            if seconds_left() <= 0:
                _refuse("external_agent_readback_timeout")
            if footer is not None or not raw:
                _refuse("invalid_readback_batch")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                               parse_constant=lambda _: _refuse("invalid_readback_batch"))
            if type(value) is not dict:
                _refuse("invalid_readback_batch")
            if set(value) == {"complete"} and type(value["complete"]) is bool:
                footer = value["complete"]
            elif set(value) == {"row"} and type(value["row"]) is dict:
                if len(rows) >= request.policy.max_rows:
                    _refuse("external_agent_readback_limit")
                rows.append(value["row"])
            else:
                _refuse("invalid_readback_batch")

        async def transfer():
            nonlocal consumed
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(remaining),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
                async with client.stream("GET", endpoint, params=params, headers=headers) as response:
                    if (response.status_code != 200
                            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-ndjson"
                            or response.headers.get("content-encoding", "identity").lower() != "identity"):
                        _refuse("external_agent_readback_unavailable")
                    async for chunk in response.aiter_raw(chunk_size=min(16384, request.policy.max_row_bytes + 1)):
                        if seconds_left() <= 0:
                            _refuse("external_agent_readback_timeout")
                        consumed += len(chunk)
                        if consumed > request.policy.max_total_bytes:
                            _refuse("external_agent_readback_limit")
                        for piece in chunk.splitlines(keepends=True):
                            buffer.extend(piece)
                            if len(buffer) > request.policy.max_row_bytes:
                                _refuse("external_agent_readback_limit")
                            if buffer.endswith(b"\n"):
                                line(bytes(buffer[:-1])); buffer.clear()
                    if buffer:
                        line(bytes(buffer))
        try:
            # Absolute timeout also bounds a slow drip before aiter_raw yields a full chunk.
            # wait_for drains cancellation of this owned HTTP operation on Python 3.10+.
            await asyncio.wait_for(transfer(), timeout=remaining)
            if seconds_left() <= 0:
                _refuse("external_agent_readback_timeout")
            if footer is not True:
                _refuse("external_agent_readback_incomplete")
            return ReadbackBatch(rows, complete=True)
        except ExternalAgentReadbackError:
            raise
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError):
            _refuse("external_agent_readback_timeout")
        except httpx.HTTPError:
            _refuse("external_agent_readback_unavailable")
        except (ValueError, UnicodeError, RecursionError):
            _refuse("invalid_readback_batch")

    return ReadbackAdapter(read, mode="async_io", policy=selected_policy)
