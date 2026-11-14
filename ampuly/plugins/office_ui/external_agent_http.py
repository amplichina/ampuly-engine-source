"""External principal HTTP admission; owner execution and approval stay separate."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import time

from aiohttp import web

from ampuly.channels.external_agent_ingress import ExternalAgentEnvelope
from ampuly.core.external_agent_service import ExternalAgentRefused
from ampuly.plugins.office_ui.external_agent_ws import (
    _ENVELOPE_FIELDS, _PRINCIPAL, _error_code, _host_home, _load_grant, _service,
)

PROTOCOL = "ampuly.external-agent.http.v1"
MAX_BODY_BYTES = 64 * 1024


def authentication_bytes(principal: str, operation: str, issued_at: int, body: bytes) -> bytes:
    """Domain separated, operation/path/principal/body-bound authentication."""
    return json.dumps([PROTOCOL, principal, operation, issued_at,
                       hashlib.sha256(body).hexdigest()], separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def register_external_agent_http(app: web.Application, handler) -> None:
    """Mount on the existing host, without creating an Engine or granting access.

    Only submit/status are exposed. Remote peers require real TLS; forwarded
    headers never establish transport trust. Host-provisioned grants remain
    mandatory even on loopback. No console login is accepted as external auth.
    """
    active = 0

    async def handle(request):
        nonlocal active
        if active >= 8:
            return web.json_response({"ok": False, "error": "external_agent_busy"}, status=429,
                                     headers={"Cache-Control": "no-store"})
        active += 1
        try:
            try:
                local = ipaddress.ip_address(request.remote or "").is_loopback
            except ValueError:
                local = False
            if not request.secure and not local:
                raise ExternalAgentRefused("external_agent_tls_required")
            if getattr(handler, "_shutting_down", False):
                raise ExternalAgentRefused("external_agent_stopped")
            principal = request.match_info["principal"]
            operation = request.match_info["operation"]
            if not _PRINCIPAL.fullmatch(principal) or operation not in {"submit", "status"}:
                raise ExternalAgentRefused("invalid_external_agent_request")
            if (request.content_type != "application/json" or request.query_string
                    or request.headers.get("Content-Encoding", "identity") != "identity"):
                raise ExternalAgentRefused("invalid_external_agent_request")
            grant = _load_grant(_host_home(handler), principal)
            offered_time = request.headers.get("X-Ampuly-Issued-At", "")
            if not offered_time.isascii() or not offered_time.isdecimal() or len(offered_time) > 12:
                raise ExternalAgentRefused("external_agent_auth_required")
            issued_at = int(offered_time)
            if abs(time.time() - issued_at) > 60:
                raise ExternalAgentRefused("external_agent_auth_expired")
            signature = request.headers.get("X-Ampuly-Signature", "")
            if len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
                raise ExternalAgentRefused("external_agent_auth_required")
            if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
                raise ExternalAgentRefused("external_agent_request_too_large")
            body = bytearray()
            async with asyncio.timeout(5):
                async for chunk in request.content.iter_chunked(8192):
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise ExternalAgentRefused("external_agent_request_too_large")
                    body.extend(chunk)
            expected = hmac.new(grant.secret, authentication_bytes(
                principal, operation, issued_at, bytes(body)), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ExternalAgentRefused("external_agent_auth_required")
            raw = json.loads(body, object_pairs_hook=_unique_object)
            if type(raw) is not dict:
                raise ExternalAgentRefused("invalid_external_agent_request")
            # Reload after the bounded network read, before constructing a service.
            current = _load_grant(_host_home(handler), principal)
            if current.document != grant.document or not hmac.compare_digest(current.secret, grant.secret):
                raise ExternalAgentRefused("external_agent_grant_changed")
            if operation == "submit":
                if set(raw) != {"envelope"} or type(raw["envelope"]) is not dict or set(raw["envelope"]) - _ENVELOPE_FIELDS:
                    raise ExternalAgentRefused("invalid_external_agent_envelope")
                argument = ExternalAgentEnvelope(**raw["envelope"])
            else:
                if set(raw) != {"message_id"}:
                    raise ExternalAgentRefused("invalid_external_agent_request")
                argument = raw["message_id"]
                if (type(argument) is not str or not argument or argument != argument.strip()
                        or len(argument.encode("utf-8")) > 256 or any(ord(c) < 32 for c in argument)):
                    raise ExternalAgentRefused("invalid_message_id")
            result = await getattr(_service(handler, principal, expected_grant=grant), operation)(argument)
            return web.json_response({"ok": True, "protocol": PROTOCOL, "result": result},
                                     headers={"Cache-Control": "no-store"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = _error_code(exc)
            if isinstance(exc, TimeoutError):
                code = "external_agent_request_timeout"
            return web.json_response({"ok": False, "error": code}, status=403,
                                     headers={"Cache-Control": "no-store"})
        finally:
            active -= 1

    app.router.add_post("/api/external-agent/v1/{principal}/{operation:submit|status}", handle)
