"""Playwright BrowserBackend: wraps BrowserRuntime for the hybrid built-in leg.

Implements BrowserBackend (snapshot / act / capture / close) so run_batch and
the same Ampuly permission / Effect / receipt path used by EgoBackend work on
new machines without Ego. Does not vendor Chromium; launch still goes through
BrowserRuntime + staged PLAYWRIGHT_BROWSERS_PATH (D1).

B1 leaf: adapter + unit tests with a fake runtime. B2 sessions/provider hybrid selection uses open_selected_browser_backend → this factory when Playwright wins.
C1: optional SpaceArtifactLayout binds captures/ + receipt-NNNN.json under a BrowserSpaces Task Space.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypeVar

from ampuly.layer4_tools.browser_backend import ALLOWED_KINDS, Action
from ampuly.layer4_tools.space_artifacts import SpaceArtifactLayout

T = TypeVar("T")


class PlaywrightBackendError(RuntimeError):
    """Adapter-level failure (closed backend, bad action, unsupported kind)."""

    def __init__(self, reason: str, *, detail: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.detail = dict(detail or {})
        super().__init__(f"playwright backend: {reason}")


class RuntimeSurface(Protocol):
    """Minimal async surface PlaywrightBackend needs from BrowserRuntime (or a fake)."""

    async def snapshot(self, filename: str | None = None, max_chars: int = 12_000) -> dict[str, Any]: ...

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]: ...

    async def click(self, selector: str) -> dict[str, Any]: ...

    async def type(
        self,
        selector: str,
        text: str,
        press_enter: bool = False,
        clear_existing: bool = True,
    ) -> dict[str, Any]: ...

    async def press_enter(self, selector: str) -> dict[str, Any]: ...

    async def wait_for(
        self,
        selector: str | None = None,
        timeout_seconds: float = 10.0,
        state: str = "visible",
    ) -> dict[str, Any]: ...

    async def scroll(
        self,
        amount: int = 800,
        direction: str = "down",
        to_bottom: bool = False,
    ) -> dict[str, Any]: ...

    async def select_option(
        self,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]: ...

    async def take_screenshot(
        self,
        filename: str | None = None,
        full_page: bool = True,
        mask_selectors: Any = None,
    ) -> dict[str, Any]: ...

    async def close(self) -> dict[str, Any]: ...


def _run_sync(factory: Callable[[], Awaitable[T]]) -> T:
    """Run an async factory on a dedicated loop (safe if caller already has one)."""

    def _call() -> T:
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _call()

    box: list[T] = []
    err: list[BaseException] = []

    def worker() -> None:
        try:
            box.append(_call())
        except BaseException as exc:  # noqa: BLE001 — re-raise on caller thread
            err.append(exc)

    thread = threading.Thread(target=worker, name="playwright-backend-sync", daemon=True)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return box[0]


class PlaywrightBackend:
    """BrowserBackend backed by BrowserRuntime (built-in Chromium / Playwright)."""

    def __init__(
        self,
        runtime: RuntimeSurface | None = None,
        *,
        artifact_dir: str | Path | None = None,
        runtime_factory: Callable[[], RuntimeSurface] | None = None,
        space_artifacts: SpaceArtifactLayout | None = None,
        close_space_on_close: bool = True,
    ) -> None:
        if runtime is None:
            if runtime_factory is not None:
                runtime = runtime_factory()
            else:
                from ampuly.layer4_tools.browser import BrowserRuntime

                runtime = BrowserRuntime()
        self._runtime: RuntimeSurface = runtime
        self._space_artifacts = space_artifacts
        self._close_space_on_close = bool(close_space_on_close)
        if space_artifacts is not None:
            space_artifacts.ensure_dirs()
            effective = Path(artifact_dir) if artifact_dir else space_artifacts.directory
        else:
            effective = Path(artifact_dir) if artifact_dir else None
        self._artifact_dir = effective
        if self._artifact_dir is not None:
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            setter = getattr(self._runtime, "set_output_root", None)
            if callable(setter):
                setter(self._artifact_dir)
        self._closed = False

    @property
    def runtime(self) -> RuntimeSurface:
        return self._runtime

    @property
    def space(self):
        """Bound BrowserSpaces Task Space when C1 space_artifacts is set."""
        if self._space_artifacts is None:
            return None
        return self._space_artifacts.space

    @property
    def space_artifacts(self) -> SpaceArtifactLayout | None:
        return self._space_artifacts

    def _ensure_open(self) -> None:
        if self._closed:
            raise PlaywrightBackendError("backend_closed")

    def snapshot(self) -> dict[str, Any]:
        self._ensure_open()
        detail = _run_sync(lambda: self._runtime.snapshot())
        return {"ok": True, "snapshot": detail}

    def act(self, action: Action) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(action, Action):
            return {"ok": False, "error": "not_an_action"}
        if action.kind not in ALLOWED_KINDS:
            return {"ok": False, "error": f"kind_not_allowed:{action.kind}"}
        try:
            detail = _run_sync(lambda: self._dispatch(action))
        except PlaywrightBackendError as exc:
            out = {"ok": False, "error": exc.reason, "detail": exc.detail}
            self._record_space_receipt(action, out)
            return out
        except Exception as exc:  # noqa: BLE001 — single-step failure for run_batch
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._record_space_receipt(action, out)
            return out
        out: dict[str, Any] = {"ok": True, "result": detail}
        if action.kind == "capture" and isinstance(detail, dict):
            path = detail.get("saved_to") or detail.get("path")
            if path:
                out["path"] = str(path)
        self._record_space_receipt(action, out)
        return out

    def _record_space_receipt(self, action: Action, result: dict[str, Any]) -> None:
        layout = self._space_artifacts
        if layout is None:
            return
        payload = {
            "ok": bool(result.get("ok")),
            "kind": action.kind,
            "target": action.target,
            "error": result.get("error") or "",
        }
        if result.get("path"):
            payload["path"] = str(result["path"])
        try:
            layout.write_receipt(payload)
        except OSError:
            pass

    async def _dispatch(self, action: Action) -> dict[str, Any]:
        kind = action.kind
        meta = action.meta if isinstance(action.meta, dict) else {}

        if kind == "navigate":
            return await self._runtime.navigate(action.target)
        if kind == "click":
            return await self._runtime.click(action.target)
        if kind == "fill":
            return await self._runtime.type(action.target, action.value)
        if kind == "press":
            return await self._press(action)
        if kind == "wait":
            if action.target:
                timeout = float(meta.get("timeout_seconds", meta.get("seconds", 10.0)))
                state = str(meta.get("state", "visible") or "visible")
                return await self._runtime.wait_for(
                    selector=action.target,
                    timeout_seconds=timeout,
                    state=state,
                )
            ms = int(meta.get("ms", 500))
            await asyncio.sleep(max(0, ms) / 1000.0)
            return {"ok": True, "waited_ms": max(0, ms)}
        if kind == "scroll":
            amount = int(meta.get("amount", meta.get("dy", 600)))
            direction = str(meta.get("direction", "down") or "down")
            if amount < 0:
                amount = abs(amount)
                direction = "up"
            to_bottom = bool(meta.get("to_bottom", False))
            return await self._runtime.scroll(
                amount=amount,
                direction=direction,
                to_bottom=to_bottom,
            )
        if kind == "select":
            # Match EgoBackend: value is treated as option label when present.
            if "index" in meta:
                return await self._runtime.select_option(
                    action.target,
                    index=int(meta["index"]),
                )
            if meta.get("value") is not None and not action.value:
                return await self._runtime.select_option(
                    action.target,
                    value=str(meta["value"]),
                )
            return await self._runtime.select_option(action.target, label=action.value)
        if kind == "capture":
            path = self._capture_path(action.target)
            return await self._runtime.take_screenshot(filename=path, full_page=True)
        raise PlaywrightBackendError(f"unsupported_kind:{kind}")

    async def _press(self, action: Action) -> dict[str, Any]:
        key = (action.value or "").strip()
        selector = (action.target or "").strip()
        # Ego page.press(key) has no selector; BrowserRuntime exposes press_enter(selector).
        # Map Enter/Return on a field; other keys stay fail-closed until a public hotkey API exists.
        effective_key = key or ("Enter" if selector else "")
        if not selector:
            raise PlaywrightBackendError(
                "press_needs_target",
                detail={"hint": "PlaywrightBackend press uses BrowserRuntime.press_enter(selector)"},
            )
        if effective_key.casefold() not in {"enter", "return", ""}:
            raise PlaywrightBackendError(
                f"press_key_unsupported:{effective_key}",
                detail={"supported": ["Enter", "Return"]},
            )
        return await self._runtime.press_enter(selector)

    def _capture_path(self, requested: str) -> str:
        if self._space_artifacts is not None:
            return str(self._space_artifacts.resolve_capture_path(requested or ""))
        if requested and Path(requested).suffix:
            path = Path(requested)
            if not path.is_absolute() and self._artifact_dir is not None:
                path = self._artifact_dir / path
            path.parent.mkdir(parents=True, exist_ok=True)
            return str(path)
        base = self._artifact_dir or Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        captures = base / "captures"
        captures.mkdir(parents=True, exist_ok=True)
        return str(captures / f"{int(time.time() * 1000)}.png")

    def capture(self, path: str) -> str:
        self._ensure_open()
        target = str(Path(path))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        result = self.act(Action("capture", target))
        if not result.get("ok"):
            raise PlaywrightBackendError(
                str(result.get("error") or "capture_failed"),
                detail=result if isinstance(result, dict) else {},
            )
        return str(result.get("path") or target)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _run_sync(lambda: self._runtime.close())
        except Exception:  # noqa: BLE001 — close must not raise past tool settlement
            pass
        layout = self._space_artifacts
        if layout is not None and self._close_space_on_close:
            try:
                from ampuly.layer4_tools.browser_spaces import BrowserSpaces

                # space.directory = <root>/spaces/<space_id>
                store = BrowserSpaces(Path(layout.space.directory).parent.parent)
                store.close_space(layout.space.space_id)
                layout.space.state = "closed"
            except Exception:  # noqa: BLE001 — close must not raise
                try:
                    layout.space.state = "closed"
                except Exception:
                    pass


def open_playwright_backend(
    *,
    runtime: RuntimeSurface | None = None,
    artifact_dir: str | Path | None = None,
    runtime_factory: Callable[[], RuntimeSurface] | None = None,
    space_artifacts: SpaceArtifactLayout | None = None,
    spaces=None,
    profile_id: str | None = None,
    task_id: str | None = None,
    close_space_on_close: bool = True,
) -> PlaywrightBackend:
    """Factory for the built-in leg (B1+B2 hybrid fallback; C1 Space bind).

    When space_artifacts is omitted but spaces+profile_id+task_id are given,
    opens (or reuses) a Task Space and routes capture/receipt there.
    """
    layout = space_artifacts
    if layout is None and spaces is not None and profile_id and task_id:
        from ampuly.layer4_tools.space_artifacts import open_space_artifacts

        layout = open_space_artifacts(spaces, profile_id, task_id)
    return PlaywrightBackend(
        runtime,
        artifact_dir=artifact_dir,
        runtime_factory=runtime_factory,
        space_artifacts=layout,
        close_space_on_close=close_space_on_close,
    )
