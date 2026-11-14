"""Optional loopback HTTP projection; importing/building it opens no listener.

The engine must supply a ManagedModelRoute and mint grants itself. There is no
HTTP endpoint for minting tokens, changing models, choosing scope or budgets.
No global app registration: production lifecycle wiring is a separate step.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ampuly.llm.managed_model_route import ManagedModelRoute, ModelRouteRefused
from ampuly.llm.responses_stream import buffered_sse

BODY_LIMIT = 512_000
RESULT_LIMIT = 8_000_000
ERROR_STATUS = {
    "model_route_unauthorized": 401, "model_route_expired_or_revoked": 401,
    "model_route_local_only": 403, "model_route_payload_too_large": 413,
    "model_route_busy": 409, "model_request_already_claimed": 409,
    "model_route_turn_changed": 409, "model_route_scope_changed": 409, "model_route_changed": 409,
    "model_route_unsupported_request": 422, "model_route_invalid_body": 422,
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _no_constant(value):
    raise ValueError("nonfinite JSON number")


async def _body(request: Any) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > BODY_LIMIT:
            raise ModelRouteRefused("model_route_payload_too_large")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_no_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def response_handler(route: ManagedModelRoute):
    """Return an aiohttp-compatible handler, without owning servers/processes."""
    from aiohttp import web

    async def handle(request):
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        try:
            if request.remote not in {"127.0.0.1", "::1"} or request.headers.get("Origin"):
                raise ModelRouteRefused("model_route_local_only")
            authorization = request.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                raise ModelRouteRefused("model_route_unauthorized")
            token = authorization[7:]
            route.authorize(token)  # Refuse unauthorized requests before reading a body.
            if request.method != "POST" or request.query_string or request.headers.get("Content-Encoding"):
                raise ModelRouteRefused("model_route_unsupported_request")
            if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ModelRouteRefused("model_route_unsupported_request")
            try:
                body = await asyncio.wait_for(_body(request), timeout=15)
            except (ValueError, UnicodeError, asyncio.TimeoutError, RecursionError):
                raise ModelRouteRefused("model_route_invalid_body") from None
            streaming = body.pop("stream", False)
            if type(streaming) is not bool:
                raise ModelRouteRefused("model_route_unsupported_request")
            result = await route.complete(token, body)
            if streaming:
                payload = b"".join(buffered_sse(result))
                headers["X-SuperOnes-Stream-Mode"] = "buffered"
                content_type = "text/event-stream"
            else:
                payload = json.dumps(result.response, ensure_ascii=False, allow_nan=False).encode()
                content_type = "application/json"
            if len(payload) > RESULT_LIMIT:
                raise ModelRouteRefused("model_result_unavailable_verify_before_retry")
            route.authorize(token)
            return web.Response(body=payload, headers=headers, content_type=content_type, charset="utf-8")
        except ModelRouteRefused as exc:
            code = exc.code
            status = ERROR_STATUS.get(code, 503)
            return web.json_response({"error": {"code": code, "message": code, "automatic_retry": False}},
                                     status=status, headers=headers)
        except Exception:
            # No traceback/transport body in HTTP response or application log.
            return web.json_response({"error": {"code": "model_route_internal_error", "automatic_retry": False}},
                                     status=503, headers=headers)

    return handle


class ManagedModelGateway:
    """Explicit, single-owner listener lifecycle; construction opens nothing.

    Not registered in the engine automatically. The engine must separately
    approve startup and connect scoped grants to its managed execution runtime.
    Closing this object owns revocation of its entire route, not another route.
    """
    def __init__(self, registry: Any, *, max_grants: int = 32):
        self.route = ManagedModelRoute(registry, max_grants=max_grants)
        self._runner = None
        self._base_url = ""
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        if self._closed or not self._base_url:
            raise ModelRouteRefused("model_gateway_not_running")
        return self._base_url

    async def __aenter__(self) -> ManagedModelGateway:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def _cleanup(self) -> None:
        runner = self._runner
        if runner is None:
            return
        # One cleanup operation, not retries or a detached background reaper.
        # aiohttp also has its own shorter graceful-shutdown bound.
        worker = asyncio.create_task(asyncio.wait_for(runner.cleanup(), timeout=10))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                # Retrieve the completed task below and expose only our code.
                break
        try:
            worker.result()
        except Exception:
            # Keep the runner reference for explicit operator reconciliation.
            raise ModelRouteRefused("model_gateway_cleanup_failed") from None
        self._runner = None
        if cancelled:
            raise asyncio.CancelledError

    async def start(self) -> str:
        from aiohttp import web

        async with self._lock:
            if self._closed:
                raise ModelRouteRefused("model_gateway_closed")
            if self._base_url:
                return self._base_url
            app = web.Application(client_max_size=BODY_LIMIT)
            app.router.add_post('/v1/responses', response_handler(self.route))
            runner = web.AppRunner(app, access_log=None, handle_signals=False,
                                   shutdown_timeout=5, keepalive_timeout=5)
            self._runner = runner
            try:
                await runner.setup()
                site = web.TCPSite(runner, host='127.0.0.1', port=0,
                                   backlog=16, reuse_address=False, reuse_port=False)
                await site.start()
                addresses = runner.addresses
                if (len(addresses) != 1 or not isinstance(addresses[0], tuple)
                        or len(addresses[0]) != 2 or addresses[0][0] != '127.0.0.1'
                        or type(addresses[0][1]) is not int or not 0 < addresses[0][1] <= 65535):
                    raise ModelRouteRefused("model_gateway_invalid_listener")
                self._base_url = f'http://127.0.0.1:{addresses[0][1]}/v1'
                return self._base_url
            except BaseException as exc:
                self._closed = True
                self.route.close()
                await self._cleanup()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ModelRouteRefused("model_gateway_start_failed") from None

    async def close(self) -> None:
        async with self._lock:
            # Revoke before waiting for active HTTP handlers and socket cleanup.
            self._closed = True
            self._base_url = ""
            self.route.close()
            await self._cleanup()
