"""HTTP traffic lanes for office_ui (8766): slow-request log + soft shed.

Independent Ampuly implementation. Idea only: classify routes into Control /
Data / Best-effort; log slow requests; under overload, Best-effort may 503
while Control never sheds. No new Runtime, no execution loop.

Defaults are safe: soft-shed off until ``AMPULY_TRAFFIC_SOFT_SHED=1``.
Slow-log threshold from ``AMPULY_SLOW_REQUEST_MS`` (default 2000; ``0`` turns the
slow log off).

Slow lines go through loguru, i.e. the engine's ``ampuly.log`` file sink, which
already rotates at 50 MB and keeps 30 days. A per-minute cap keeps a storm of
slow requests from flooding that file. WebSocket upgrades are long-lived by
design: they are neither counted as in-flight nor reported as slow (live check
2026-11-01 found both).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from loguru import logger


class TrafficLane(str, Enum):
    CONTROL = "control"
    DATA = "data"
    BEST_EFFORT = "best_effort"


_CONTROL_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/api/ready",
    "/api/live",
    "/health",
    "/ready",
    "/live",
)
_CONTROL_TOKENS = ("/approval", "/cancel", "/stop", "/interrupt")


def classify_path(path: str) -> TrafficLane:
    """Map a URL path to a traffic lane. Unknown API defaults to DATA."""
    raw = str(path or "") or "/"
    p = raw.split("?", 1)[0]
    if not p.startswith("/"):
        p = "/" + p
    lowered = p.lower()
    if lowered == "/ws" or lowered.startswith("/ws/"):
        return TrafficLane.CONTROL
    for prefix in _CONTROL_PREFIXES:
        if lowered == prefix.rstrip("/") or lowered.startswith(prefix):
            return TrafficLane.CONTROL
    for token in _CONTROL_TOKENS:
        if token in lowered:
            return TrafficLane.CONTROL
    if lowered.startswith("/api/"):
        return TrafficLane.DATA
    return TrafficLane.BEST_EFFORT


def is_websocket_upgrade(path: str, headers: Any = None) -> bool:
    """Long-lived WS connections: never in-flight, never 'slow'."""
    p = str(path or "").split("?", 1)[0].lower()
    if p == "/ws" or p.startswith("/ws/"):
        return True
    try:
        upgrade = str((headers or {}).get("Upgrade", "") or "").strip().lower()
    except Exception:
        upgrade = ""
    return upgrade == "websocket"


def slow_request_threshold_ms(default: int = 2000) -> int:
    raw = str(os.environ.get("AMPULY_SLOW_REQUEST_MS", "") or "").strip()
    if not raw:
        return int(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(default)


def soft_shed_enabled_from_env(default: bool = False) -> bool:
    raw = str(os.environ.get("AMPULY_TRAFFIC_SOFT_SHED", "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def soft_shed_limit_from_env(default: int = 64) -> int:
    raw = str(os.environ.get("AMPULY_TRAFFIC_INFLIGHT_LIMIT", "") or "").strip()
    if not raw:
        return int(default)
    try:
        return max(1, int(raw))
    except ValueError:
        return int(default)


@dataclass
class SoftShedState:
    """In-flight counter for soft overload decisions. Process-local only."""

    limit: int = 64
    enabled: bool = False
    inflight: int = 0

    def enter(self) -> None:
        self.inflight += 1

    def leave(self) -> None:
        self.inflight = max(0, self.inflight - 1)

    def should_shed(self, lane: TrafficLane) -> bool:
        if not self.enabled:
            return False
        if lane is TrafficLane.CONTROL:
            return False
        if lane is TrafficLane.DATA:
            return self.inflight >= self.limit
        return self.inflight >= max(1, self.limit // 2)


@dataclass
class SlowRequestLog:
    """Slow-request lines into the engine log (loguru -> ampuly.log, rotated).

    ``threshold_ms <= 0`` disables it. At most ``max_per_minute`` lines per
    minute; the rest are counted and summarized in the next allowed line.
    """

    threshold_ms: int = 2000
    max_per_minute: int = 60
    clock: Callable[[], float] = time.monotonic
    _window_start: float = 0.0
    _window_count: int = 0
    _suppressed: int = 0

    def maybe_log(self, *, path: str, method: str, lane: TrafficLane, elapsed_ms: float, status: int) -> bool:
        if self.threshold_ms <= 0 or elapsed_ms < self.threshold_ms:
            return False
        now = float(self.clock())
        if now - self._window_start >= 60.0:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= max(1, int(self.max_per_minute)):
            self._suppressed += 1
            return False
        self._window_count += 1
        suppressed, self._suppressed = self._suppressed, 0
        logger.warning(
            "slow_request method={} path={} lane={} elapsed_ms={:.1f} status={} threshold_ms={} suppressed_before={}",
            method,
            path.split("?", 1)[0],
            lane.value,
            elapsed_ms,
            status,
            self.threshold_ms,
            suppressed,
        )
        return True


def decide_admission(*, lane: TrafficLane, state: SoftShedState) -> tuple[bool, str]:
    """Pure admission: True means allow. Control always allowed."""
    if lane is TrafficLane.CONTROL:
        return True, "control_never_shed"
    if state.should_shed(lane):
        return False, f"soft_shed_{lane.value}"
    return True, "admit"


def build_aiohttp_middleware(
    *,
    shed: SoftShedState | None = None,
    slow: SlowRequestLog | None = None,
) -> Callable[[Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]:
    """aiohttp middleware: slow log + optional soft shed. Safe defaults."""
    from aiohttp import web

    shed_state = shed or SoftShedState(
        limit=soft_shed_limit_from_env(),
        enabled=soft_shed_enabled_from_env(False),
    )
    slow_log = slow or SlowRequestLog(threshold_ms=slow_request_threshold_ms())

    @web.middleware
    async def middleware(request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        path = str(getattr(request, "path", "") or "/")
        method = str(getattr(request, "method", "") or "GET").upper()
        if is_websocket_upgrade(path, getattr(request, "headers", None)):
            # Control lane, never shed; a WS lives for hours, so counting it
            # would fill the in-flight budget and log every disconnect as slow.
            return await handler(request)
        started = time.perf_counter()
        # Opt-in probe only: AMPULY_TRAFFIC_PROBE_DELAY=1 + X-Ampuly-Probe-Delay-Ms.
        # Used to prove the middleware chain on a live instance without a real
        # heavy handler. Default off; never set in normal production starts.
        # Timer starts above so probe delay is visible in the slow log.
        if str(os.environ.get("AMPULY_TRAFFIC_PROBE_DELAY", "") or "").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                raw_delay = str((request.headers or {}).get("X-Ampuly-Probe-Delay-Ms", "") or "").strip()
                delay_ms = max(0, min(10000, int(raw_delay or "0")))
            except Exception:
                delay_ms = 0
            if delay_ms > 0:
                import asyncio as _asyncio
                await _asyncio.sleep(delay_ms / 1000.0)

        lane = classify_path(path)
        allowed, reason = decide_admission(lane=lane, state=shed_state)
        if not allowed:
            return web.json_response(
                {"ok": False, "error": "overloaded", "lane": lane.value, "reason": reason},
                status=503,
                headers={"Retry-After": "1"},
            )
        shed_state.enter()
        status = 500
        try:
            response = await handler(request)
            status = int(getattr(response, "status", 200) or 200)
            return response
        except Exception:
            status = 500
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            shed_state.leave()
            try:
                slow_log.maybe_log(path=path, method=method, lane=lane, elapsed_ms=elapsed_ms, status=status)
            except Exception:
                pass

    return middleware