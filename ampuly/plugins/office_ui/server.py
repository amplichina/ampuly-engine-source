"""Office-UI Server — aiohttp server that bridges AMPULY Engine with the React frontend.

Initializes AmpulyEngine, opens ui_state.db for agent/chat persistence,
sets up the event adapter pipeline, and serves static files + WebSocket.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from ampuly.core.windows_ssl import sanitize_windows_sslkeylogfile

sanitize_windows_sslkeylogfile()

import aiohttp.web
import aiosqlite
from loguru import logger

from ampuly.core.config import AmpulyConfig, get_ampuly_home, get_project_workplace
from ampuly.core.file_lock import lock_file_descriptor
from ampuly.core.workspace_trust import WorkspaceTrustRequired
from ampuly.engine import AmpulyEngine
from ampuly.plugins.office_ui.agent_store import AgentStore
from ampuly.plugins.office_ui.chat_store import ChatStore
from ampuly.plugins.office_ui.event_adapter import EventAdapter
from ampuly.plugins.office_ui.terminal import server_banner
from ampuly.plugins.office_ui.terminal import status as terminal_status
from ampuly.plugins.office_ui.ws_handler import WSHandler


# ── Static file paths ────────────────────────────────────────────────────

# Pre-built frontend lives alongside this file
_STATIC_DIR = Path(__file__).parent / "frontend_dist"
_FRONTEND_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def health_response(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Explicit readiness schema; never mistake the SPA fallback for health.

    Successful app construction owns all three resources. This bounded local
    observation neither creates a task nor probes a model/channel provider.
    """
    engine = request.app.get("engine")
    handler = request.app.get("ws_handler")
    ready = bool(
        engine is not None and getattr(engine, "_initialized", False)
        and request.app.get("db") is not None and handler is not None
        and not getattr(handler, "_shutting_down", False)
    )
    return aiohttp.web.json_response(
        {"schema_version": 1, "service": "ampuly-office-ui", "ok": ready,
         "status": "ready" if ready else "not_ready"},
        status=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


def _is_under_path(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _acquire_single_instance_lock(ampuly_home: Path) -> Any | None:
    """Prevent two office-UI servers from sharing one AMPULY home.

    Two server processes writing the same ui_state.db contend for the sqlite
    write lock and surface as 'database is locked' failures mid-run. The lock
    is advisory (flock on POSIX, LockFileEx on Windows), scoped to this AMPULY
    home, and released automatically when the process exits — including on a
    crash, so a stale lock file can never block a fresh start.
    """
    lock_path = ampuly_home / "office_ui.lock"
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        lock_file_descriptor(lock_file.fileno(), blocking=False)
    except OSError:
        holder_pid = "unknown"
        if os.name != "nt":
            # POSIX locks are advisory, so the holder metadata remains
            # readable. Windows byte-range locks are mandatory and reading
            # the locked range would replace the intended SystemExit with a
            # PermissionError.
            lock_file.seek(0)
            holder_pid = lock_file.read().strip() or holder_pid
        lock_file.close()
        raise SystemExit(
            f"Another office-UI server (pid {holder_pid}) is already running against "
            f"{ampuly_home}. Two instances sharing one ui_state.db cause 'database is "
            "locked' failures that can crash in-flight agent runs. Stop the other "
            "instance first, or point this one at a different AMPULY home."
        )
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


# ── Application factory ──────────────────────────────────────────────────

def _mark_orphaned_holds(ampuly_home: Path) -> None:
    """Model-routing phase 0, open point 10 (owner ruling): a paid call a stopped console held and never finished is
    marked unknown before anything resumes, so its order waits for a person instead of sending again."""
    from datetime import datetime

    from ampuly.i18n_runtime import is_zh
    from ampuly.plugins.office_ui.business_store import BusinessStore

    # The reason is what the order row shows the owner beside 「等你确认」 (AN): in the owner's words. The console maps the
    # English one, which rows written before this change still carry.
    reason = (
        "控制台上次停下时，这次付费调用还没结束，不知道到底发出去没有；请说明它发出去了没有"
        if is_zh()
        else "held by a console that stopped before the call finished; say what happened to it"
    )
    try:
        marked = BusinessStore(ampuly_home).mark_orphaned_holds(
            before=datetime.now().isoformat(), actor="console start", reason=reason,
        )
    except Exception:  # noqa: BLE001 - the console still starts; the holds stay as they were
        logger.opt(exception=True).warning("held reservations left by an earlier console could not be checked")
        return
    if marked:
        logger.warning("{} held paid call(s) left by an earlier console are now waiting for a person to settle", len(marked))


# How long console start waits for the tier pass below; past it the console starts and the pass finishes on its own.
_TIER_PASS_WAIT_SECONDS = 30.0


async def _reproject_installed_tiers(ampuly_home: Path) -> None:
    """LP-B P-e : team configs installed before a step's tier was projected get the tier, once per start.

    Off the event loop (it reads every workflow package and team config), before the engine resumes any run, and
    never in the way of the start: a failure is logged and the console starts; past the wait the console starts and
    the pass finishes in its thread (each file it writes is replaced whole)."""

    def _pass() -> dict[str, Any]:
        from ampuly.plugins.office_ui.workflow_packages import discover_packages, reproject_installed_tiers

        return reproject_installed_tiers(ampuly_home, discover_packages(Path(__file__).resolve().parents[3], ampuly_home))

    try:
        report = await asyncio.wait_for(asyncio.to_thread(_pass), timeout=_TIER_PASS_WAIT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("给老团队配置补步骤档位还没做完，控制台先启动，补档位在后台接着做")
        return
    except Exception:  # noqa: BLE001 - the console still starts; the team configs stay as they were
        logger.opt(exception=True).warning("给老团队配置补步骤档位没有做成，团队配置保持原样")
        return
    if report.get("steps"):
        logger.info(
            "给 {} 个团队配置补上了 {} 个步骤的档位（它们装在档位写进团队配置之前）。之后派出的步骤按档位选模型；"
            "已经派出、正在跑的步骤仍按标准档跑完。",
            len(report.get("files") or []), report["steps"],
        )
    if report.get("problems"):
        logger.warning("有 {} 个团队配置读不了或写不进，没有补档位", len(report["problems"]))


async def create_app(
    config: AmpulyConfig | None = None,
    project_id: str | None = None,
) -> aiohttp.web.Application:
    """Build and return a fully-wired aiohttp Application."""

    from ampuly.plugins.office_ui.traffic_lanes import build_aiohttp_middleware

    # Slow-request log always on (threshold via AMPULY_SLOW_REQUEST_MS). Soft shed
    # default off (AMPULY_TRAFFIC_SOFT_SHED=1). Control lanes never shed.
    app = aiohttp.web.Application(middlewares=[build_aiohttp_middleware()])

    # ── Load config from standard location if not provided ─────────
    if config is None:
        config_dir = get_ampuly_home() / "config"
        if config_dir.is_dir():
            try:
                config = AmpulyConfig.load(config_dir)
                logger.info(f"Loaded config from {config_dir}")
            except WorkspaceTrustRequired:
                raise
            except Exception as e:
                # Starting with defaults after a persisted production config
                # fails to parse is unsafe: the UI looks healthy while its
                # provider, approval, and isolation policy may be different.
                logger.error(f"Failed to load config from {config_dir}: {e}")
                raise

    # ── AMPULY Engine ────────────────────────────────────────────────────
    # The console's orders are the kernel's order port (runtime contract), given before
    # initialize(): its startup reconcile already asks whether a run's order has ended.
    from ampuly.plugins.office_ui.order_liveness import BusinessOrderLiveness

    # A console always runs inside one project. ``ui`` passes no --project, and
    # the employee runtime host refuses an engine with no concrete project
    # (BuiltinReadTools validates it), so bind the default here rather than
    # crashing every launch without --project.
    engine = AmpulyEngine(
        config=config, project_id=str(project_id or "default"),
        order_liveness=BusinessOrderLiveness(get_ampuly_home()),
    )

    # ── UI-state database (agents + chat) ─────────────────────────────
    ampuly_home = engine.ampuly_home
    # A clean installation has no AMPULY home yet. The UI database is opened
    # before engine.initialize() (which normally creates the directory), so
    # materialize the scoped home here to make first startup deterministic.
    ampuly_home.mkdir(parents=True, exist_ok=True)
    instance_lock = _acquire_single_instance_lock(ampuly_home)
    local_demo_only = bool((os.environ.get("AMPULY_LOCAL_DEMO_PROJECT_ONLY") or "").strip())
    if local_demo_only:
        logger.info("Local demo process: shared-home hold reconciliation and installed-tier reprojection skipped")
    else:
        _mark_orphaned_holds(ampuly_home)
    db: Any | None = None
    ws_handler: WSHandler | None = None
    engine_started = False

    async def _cleanup_partial_startup() -> None:
        """Release startup resources when wiring fails before app ownership exists."""
        nonlocal db, ws_handler, engine_started
        if ws_handler is not None:
            try:
                await ws_handler.shutdown()
                await ws_handler.flush_all_progress()
            except Exception:
                logger.exception("Failed to clean up partial WS handler startup")
        if engine_started:
            try:
                await engine.shutdown()
            except Exception:
                logger.exception("Failed to clean up partial engine startup")
        if db is not None:
            try:
                await db.close()
            except Exception:
                logger.exception("Failed to close partial UI database startup")
        try:
            instance_lock.close()
        except Exception:
            pass

    try:
        # Beside _mark_orphaned_holds, inside the cleanup so a cancelled start still releases the instance lock, and
        # before engine.initialize() so a run it resumes reads the tier.
        if not local_demo_only:
            await _reproject_installed_tiers(ampuly_home)
        db_path = ampuly_home / "ui_state.db"
        db = await aiosqlite.connect(str(db_path))
        # Wait for a concurrent writer (CLI, tooling) instead of failing after
        # sqlite's 5s default with 'database is locked'.
        await db.execute("PRAGMA busy_timeout=30000")

        agent_store = AgentStore(db)
        await agent_store.initialize()

        chat_store = ChatStore(db)
        await chat_store.initialize()

        event_adapter = EventAdapter()

        # ── Initialize engine (this starts all AMPULY layers) ────────────────
        # Mark ownership before entering initialize: a partially initialized
        # engine may already have opened resources before raising.
        engine_started = True
        await engine.initialize()
        from ampuly.plugins.office_ui.employee_runtime_host import build_employee_runtime_host
        engine.set_employee_runtime_host_factory(build_employee_runtime_host)
        engine.attach_employee_runtime_host(build_employee_runtime_host(engine))
        from ampuly.plugins.office_ui.runtime_projection_source import RuntimeProjectionLoader
        engine._runtime_input_loader = RuntimeProjectionLoader(engine.ampuly_home)

        # ── WSHandler ─────────────────────────────────────────────────────
        # The handler attaches the root engine once, in its constructor (project_engines.py): callbacks bound to the
        # engine's own project, so a project switch does not retarget in-flight progress/runtime events to the view.
        ws_handler = WSHandler(engine, agent_store, chat_store, event_adapter)
        # The built-in console extensions (extensions.py), once: their ws actions join the table (a no-op when the
        # import of ws_handler already did it). One switched off or not importable adds nothing.
        from ampuly.plugins.office_ui import extensions

        loaded = extensions.load_builtin()
        extensions.install_ws_actions(WSHandler._HANDLERS)
        logger.info("console extensions loaded: {}", ", ".join(loaded) or "none")

        # Q05 B3: PULY (JEV) as the browser decider for connector steps; usage filed under each task's project.
        from ampuly.plugins.office_ui import browser_decider

        browser_decider.bind(ws_handler)
        from ampuly.plugins.office_ui import browser_connector

        browser_connector.bind_order_inputs(ws_handler)  # bulk P1: a lookup's key, from the order's own record

    # Wire EventBus → ws_handler.on_ampuly_event (subscribe to ALL events),
    # preserving the root project context even when the active UI view changes.
        async def _root_engine_event(event: Any) -> None:
            await ws_handler.on_ampuly_event(
                event,
                runtime_engine=engine,
                project_id=engine.project_id or "default",
            )

        engine.event_bus.subscribe_all(_root_engine_event)

    # ── Restore persisted mode and load matching agents on startup ───
        await ws_handler.restore_persisted_mode()
        startup_preset = ws_handler._resolve_preset_name()
        agents = await agent_store.load_preset(startup_preset, engine.org_engine)
        logger.info(f"Loaded {len(agents)} preset agents (mode={ws_handler._exec_mode}, preset={startup_preset})")

    # ── Ensure activity + secretary channels (session channels are created on demand)
        await chat_store.ensure_activity_channel()
        await chat_store.ensure_secretary_channel()
    except BaseException:
        await _cleanup_partial_startup()
        raise

    assert db is not None
    assert ws_handler is not None

    # ── Store references for cleanup ──────────────────────────────────
    app["engine"] = engine
    app["db"] = db
    app["ws_handler"] = ws_handler
    app["instance_lock"] = instance_lock

    # ── Routes ────────────────────────────────────────────────────────
    app.router.add_get("/api/health", health_response)
    app.router.add_get("/ws", ws_handler.handle_ws)
    from ampuly.plugins.office_ui.external_agent_http import register_external_agent_http
    register_external_agent_http(app, ws_handler)
    from ampuly.plugins.office_ui.desktop_view_http import register_desktop_view_http
    register_desktop_view_http(app, ws_handler)

    # Attachment download (must be registered before the SPA catch-all). An attachment lives in its own project's
    # folder (projects/<project>/attachments/); the three-part route names the project, the two-part one is the
    # default project's (links made Previously). Data phase 0.
    app.router.add_get(
        "/api/attachments/{attachment_id}/{filename}",
        _make_attachment_handler(engine),
    )
    app.router.add_get(
        "/api/attachments/{project_id}/{attachment_id}/{filename}",
        _make_attachment_handler(engine),
    )
    # 资料 downloads (data phase 2): one file of a project's own work area, by the path the console listed.
    app.router.add_get(
        "/api/data/{project_id}/download",
        _make_data_download_handler(engine),
        allow_head=False,
    )
    app.router.add_post("/api/auth/session", _make_session_cookie_handler(engine))

    # SPA: serve static files, fallback to index.html
    if _STATIC_DIR.is_dir():
        app.router.add_get("/", _serve_index)
        app.router.add_get("/assets/{path:.*}", _serve_asset)
        # Catch-all for SPA client-side routing
        app.router.add_get("/{path:.*}", _serve_spa_fallback)
    else:
        app.router.add_get("/", _serve_no_build)
        logger.warning(f"Frontend not built: {_STATIC_DIR} does not exist")

    # ── Duty watch from the start, not from the first browser ────────
    app.on_startup.append(_on_startup)

    # ── Cleanup on shutdown ───────────────────────────────────────────
    app.on_shutdown.append(_on_shutdown)

    return app


# ── Route handlers ────────────────────────────────────────────────────

async def _serve_index(request: aiohttp.web.Request) -> aiohttp.web.FileResponse:
    return aiohttp.web.FileResponse(_STATIC_DIR / "index.html", headers=_FRONTEND_NO_STORE_HEADERS)


async def _serve_asset(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Serve built frontend assets without allowing stale UI protocol bundles."""
    path = request.match_info.get("path", "")
    file_path = (_STATIC_DIR / "assets" / path).resolve()
    assets_dir = (_STATIC_DIR / "assets").resolve()
    if not _is_under_path(file_path, assets_dir):
        return aiohttp.web.Response(status=403, text="Forbidden")
    if not file_path.is_file():
        return aiohttp.web.Response(status=404, text="Not found")
    return aiohttp.web.FileResponse(file_path, headers=_FRONTEND_NO_STORE_HEADERS)


async def _serve_spa_fallback(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Serve static file if exists, otherwise fall back to index.html for SPA routing."""
    path = request.match_info.get("path", "")
    file_path = (_STATIC_DIR / path).resolve()
    if not _is_under_path(file_path, _STATIC_DIR.resolve()):
        return aiohttp.web.Response(status=403, text="Forbidden")
    if file_path.is_file():
        return aiohttp.web.FileResponse(file_path, headers=_FRONTEND_NO_STORE_HEADERS)
    # SPA fallback
    return aiohttp.web.FileResponse(_STATIC_DIR / "index.html", headers=_FRONTEND_NO_STORE_HEADERS)


SESSION_COOKIE = "ampuly_session"


def _account_store_for(engine: AmpulyEngine):
    """The same store the WebSocket side logs people into; a second instance would know none of their sessions."""
    from ampuly.plugins.office_ui.accounts import shared_account_store

    home = getattr(engine, "ampuly_home", None) or (Path.cwd() / ".ampuly")
    return shared_account_store(Path(home))


def _request_account(engine: AmpulyEngine, request: aiohttp.web.Request) -> Any:
    """The logged-in account a request carries (bearer header or session cookie), or None."""
    try:
        store = _account_store_for(engine)
    except Exception:
        return None
    header = str(request.headers.get("Authorization", "") or "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    token = token or str(request.cookies.get(SESSION_COOKIE, "") or "")
    return store.resolve_token(token) if token else None


def _project_in_request_scope(engine: AmpulyEngine, request: aiohttp.web.Request, project_id: str) -> bool:
    """Whether the request's account may see this project: unrestricted accounts (and the console before any account
    exists) may; an account bound to customers only when the project is bound to one of them. Fails closed."""
    account = _request_account(engine, request)
    customers = tuple(str(c).strip() for c in (getattr(account, "customer_ids", ()) or ()) if str(c).strip()) if account else ()
    if not customers:
        return True
    try:
        handler = request.app.get("ws_handler")
        bindings = handler._accounts().business.project_customers() if handler is not None else {}
    except Exception:
        return False
    return str(bindings.get(project_id, "") or "") in customers


def _request_is_authorized(engine: AmpulyEngine, request: aiohttp.web.Request) -> bool:
    """A file request is served only to a logged-in session.

    Before any account exists the console has no login at all, so a request
    from this machine is as trusted as the console window itself; anything
    from the network is refused ( audit: attachments carried the
    customer's brief and drafts with no check at all).
    """
    try:
        store = _account_store_for(engine)
    except Exception:
        return False
    token = ""
    header = str(request.headers.get("Authorization", "") or "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    token = token or str(request.cookies.get(SESSION_COOKIE, "") or "")
    if token and store.resolve_token(token) is not None:
        return True
    if store.bootstrap_needed():
        peer = str(getattr(request, "remote", "") or "")
        return peer in {"127.0.0.1", "::1", "localhost"}
    return False


def _make_session_cookie_handler(engine: AmpulyEngine):
    """POST a login token, get an HttpOnly cookie the browser sends with <img>/<a>."""

    async def _handle(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = str((body or {}).get("token", "") or "")
        if not token:
            header = str(request.headers.get("Authorization", "") or "")
            if header.lower().startswith("bearer "):
                token = header[7:].strip()
        try:
            store = _account_store_for(engine)
            account = store.resolve_token(token) if token else None
        except Exception:
            # A temporary source failure does not prove cookie invalidity.
            # Refuse authentication, preserve the cookie, expose no private cause.
            return aiohttp.web.json_response(
                {"ok": False, "error": "authentication_unavailable"}, status=503,
            )
        response = aiohttp.web.json_response({"ok": bool(account)}, status=200 if account else 401)
        if account is not None:
            response.set_cookie(
                SESSION_COOKIE, token, httponly=True, samesite="Strict", path="/", max_age=12 * 3600,
            )
        else:
            response.del_cookie(SESSION_COOKIE, path="/")
        return response

    return _handle


def _loopback_host(host: str) -> bool:
    # The rule itself lives in file_serving, next to the Host and Origin checks that use it.
    from ampuly.plugins.office_ui.file_serving import loopback_host

    return loopback_host(host)


def _record_file_read(engine: AmpulyEngine, request: aiohttp.web.Request, *, project_id: str, action: str,
                      object_type: str, object_id: str, customer_id: str = "", outcome: str = "success",
                      metadata: dict[str, Any] | None = None) -> None:
    """One audit row for a file that was handed over an HTTP route, or refused.

    The WebSocket reads record theirs already; a download that bypasses them would be the one read nobody sees.
    """
    try:
        handler = request.app.get("ws_handler")
        account = _request_account(engine, request)
        actor = str(getattr(account, "login", "") or "") or "owner"
        handler._console_api().record_observation(
            project_id=project_id or "default", actor=actor, action=action, object_type=object_type,
            object_id=object_id, customer_id=customer_id, reason=f"console_{action}", outcome=outcome,
            metadata=metadata or {},
        )
    except Exception as exc:  # noqa: BLE001 - an audit row must never break the reply
        logger.warning(f"file read observation failed: {type(exc).__name__}")


def _may_download(engine: AmpulyEngine, request: aiohttp.web.Request) -> bool:
    """Whether this request may take a file away, by the console's own role table (``data_download``)."""
    from ampuly.plugins.office_ui.api import role_allows

    account = _request_account(engine, request)
    role = str(getattr(account, "role", "") or "").strip().lower() if account is not None else None
    try:
        auth_required = bool(_account_store_for(engine).auth_required())
    except Exception:
        auth_required = True
    return bool(role_allows(role, "data_download", auth_required=auth_required))


def _refuse(status: int, code: str) -> aiohttp.web.Response:
    """A refusal a browser can show and a person can read: one code, never a path and never an OS message."""
    return aiohttp.web.Response(status=status, text=code, content_type="text/plain",
                                headers={"Cache-Control": "private, no-store"})


def _make_data_download_handler(engine: AmpulyEngine):
    """GET /api/data/{project_id}/download?path=<project-relative> -- the 资料 entry's own download.

    Every check the WebSocket side makes is made here too, in the same order and from the same modules: a listing
    can never show what this route would refuse, and this route can never hand over what the listing hides.
    """
    from ampuly.plugins.office_ui import data_actions, data_browser, file_serving

    at_a_time = asyncio.Semaphore(4)

    async def _handle(request: aiohttp.web.Request) -> Any:
        from ampuly.plugins.office_ui.data_scope import Viewer, decide, hidden, path_parts, project_gate
        from ampuly.project_id import is_valid_project_id

        project_id = str(request.match_info.get("project_id", "") or "").strip()
        relative = str(request.query.get("path", "") or "")
        if not file_serving.bootstrap_request_allowed(request, bootstrap=_bootstrap_needed(engine)):
            return _refuse(403, "forbidden")
        if not _request_is_authorized(engine, request):
            return _refuse(401, "unauthorized")
        account = _request_account(engine, request)
        role = str(getattr(account, "role", "") or "").strip().lower() if account is not None else None
        if not _may_download(engine, request):
            return _refuse(403, "owner_only")
        home = Path(getattr(engine, "ampuly_home", None) or get_ampuly_home())
        if not is_valid_project_id(project_id) or not (home / "projects" / project_id).is_dir():
            return _refuse(404, "project_not_found")
        customers = tuple(str(c).strip() for c in (getattr(account, "customer_ids", ()) or ()) if str(c).strip()) if account else ()
        viewer = Viewer(owner=role == "owner" or account is None, customers=customers)
        parts = path_parts(relative)
        if not parts or hidden(parts):
            # Decided from the path alone, before the disk or the store is asked: the answer says nothing about
            # what exists.
            return _refuse(404, "not_found")

        def _decide() -> tuple[str, Any, Any]:
            """The store reads and the scope answer, off the event loop.

            Opening the business store replays its schema and the order query reads 500 rows; doing that on the loop
            freezes every socket, every push and every other file request until it finishes.
            """
            from ampuly.plugins.office_ui.business_store import BusinessStore

            store_out = BusinessStore(home)
            try:
                destroyed = str(store_out.project_lifecycle(project_id).status or "") == "destroyed"
            except Exception:
                destroyed = False  # a lifecycle that cannot be read does not make a live project unreadable
            if destroyed:
                return "project_destroyed", None, None
            try:
                binding = str(store_out.project_customers().get(project_id, "") or "").strip() or None
            except Exception:
                # Fail closed for a bound account, open for the owner, who is bound to nothing: refusing the owner
                # here would make the download refuse exactly what the listing had just shown.
                binding = None
            if project_gate(viewer, binding):
                return "customer_scope_denied", binding, None
            found = data_actions.OrderLookup(store_out, project_id)
            return "", binding, (found, decide(viewer, "/".join(parts), found.customer))

        refusal, project_customer, decided = await asyncio.to_thread(_decide)
        if refusal == "project_destroyed":
            return _refuse(404, "project_destroyed")
        if refusal:
            # An attempt on another customer's delivery space belongs in that space's own trail.
            _record_file_read(engine, request, project_id=project_id, action="data_download",
                              object_type="project_file", object_id="*", customer_id=project_customer or "",
                              outcome="denied", metadata={"error": refusal})
            return _refuse(403, refusal)
        lookup, answer = decided
        object_id = data_actions.object_id_for(answer.area, answer.order_id, "/".join(parts),
                                               known_order=bool(answer.order_id) and lookup.known(answer.order_id))
        if not answer.allowed:
            status, code = (404, "not_found") if answer.reason == "invalid_path" else (403, answer.reason)
            _record_file_read(engine, request, project_id=project_id, action="data_download",
                              object_type="project_file", object_id=object_id or "*",
                              customer_id=answer.customer_id, outcome="denied", metadata={"error": code})
            return _refuse(status, code)
        root = Path(get_project_workplace(project_id))
        try:
            fd, workspace, info = await asyncio.to_thread(data_browser.open_download, root, "/".join(parts))
        except data_browser.DataRefused as refused:
            status = 404 if refused.code in {"not_found", "invalid_path", "refused_link", "multiply_linked"} else 403
            _record_file_read(engine, request, project_id=project_id, action="data_download",
                              object_type="project_file", object_id=object_id, customer_id=answer.customer_id,
                              outcome="not_found" if status == 404 else "error", metadata={"error": refused.code})
            return _refuse(status if refused.code != "too_large" else 413, refused.code)
        except Exception as exc:  # noqa: BLE001 - the body says "error"; the type goes to the log
            logger.warning(f"data download failed: {type(exc).__name__}")
            _record_file_read(engine, request, project_id=project_id, action="data_download",
                              object_type="project_file", object_id=object_id, customer_id=answer.customer_id,
                              outcome="error", metadata={"error_type": type(exc).__name__})
            return _refuse(500, "error")
        if at_a_time.locked():
            # Four at a time: a download holds a thread and a descriptor for as long as the browser reads it. A
            # fifth is told to try again rather than queued behind three large files.
            data_browser.close_download(fd, workspace)
            return _refuse(503, "busy")
        async with at_a_time:
            size = int(info.get("size", 0) or 0)
            _record_file_read(engine, request, project_id=project_id, action="data_download",
                              object_type="project_file", object_id=object_id, customer_id=answer.customer_id,
                              metadata={"size": size})
            try:
                return await file_serving.stream_fd(
                    request, fd, size=size, content_type=file_serving.OCTET_STREAM,
                    disposition="attachment", filename=str(info.get("name", "") or parts[-1]),
                )
            finally:
                # ``stream_fd`` closes the descriptor; the capability it was opened through is exited here.
                workspace.__exit__(None, None, None)

    return _handle


def _bootstrap_needed(engine: AmpulyEngine) -> bool:
    """True while no owner account exists (so remote access must wait)."""
    try:
        # Keep the process's existing store, not a cached permission result.
        # Reconstructing it here replays the BusinessStore schema on every
        # image/file GET and blocks ready control work on the event loop.
        # bootstrap_needed still makes its fresh, fail-closed RO observation.
        return _account_store_for(engine).bootstrap_needed()
    except Exception:
        return True  # fail closed: unknown account state is not a reason to open the port


def _open_attachment(base_dir: Path, attachment_id: str, filename: str) -> tuple[int, int, Any]:
    """A read-only descriptor for one stored attachment, opened under the name that was asked for.

    The capability checks the name against the folder (data phase 2, D2): a file that is also reachable under
    another name, or through a link, is not opened at all.
    """
    from ampuly.layer4_tools.workspace_fs import SecureWorkspace

    workspace = SecureWorkspace.for_roots(str(base_dir), str(base_dir))
    entered = workspace.__enter__()
    try:
        fd = entered.open_read(entered.resolve(f"{attachment_id}/{filename}", use_output_root=False))
    except BaseException:
        entered.__exit__(None, None, None)
        raise
    try:
        return fd, int(os.fstat(fd).st_size), entered
    except BaseException:
        os.close(fd)
        entered.__exit__(None, None, None)
        raise


def _make_attachment_handler(engine: AmpulyEngine):
    """Factory that returns an HTTP handler for serving stored attachments."""

    async def _handle(request: aiohttp.web.Request) -> Any:
        from ampuly.plugins.office_ui import file_serving
        from ampuly.project_id import is_valid_project_id

        # aiohttp starts request tasks eagerly on Python 3.12. Yield before
        # synchronous DATA auth work so ready control frames can run first.
        # No identity, permission or file descriptor has been captured yet.
        await asyncio.sleep(0)
        attachment_id = request.match_info["attachment_id"]
        filename = request.match_info["filename"]
        project_id = str(request.match_info.get("project_id", "") or "default").strip() or "default"
        att_store = getattr(engine, "attachment_store", None)
        if not att_store:
            return aiohttp.web.Response(status=503, text="Attachment store not available")
        # While no account exists a request from this machine is the console itself. A page in the owner's own
        # browser is also on this machine, so the name it asked for must be this machine too (data phase 2, D7).
        if not file_serving.bootstrap_request_allowed(request, bootstrap=_bootstrap_needed(engine)):
            return _refuse(403, "forbidden")
        if not _request_is_authorized(engine, request):
            return aiohttp.web.Response(status=401, text="Unauthorized")
        if not is_valid_project_id(project_id):
            return aiohttp.web.Response(status=404, text="Not found")
        # An account bound to customers sees only its customers' projects -- here as on the WebSocket side.
        if not _project_in_request_scope(engine, request, project_id):
            return aiohttp.web.Response(status=403, text="Forbidden")
        # An attachment id is 16 hex characters (attachment_store); a name is one path segment. Anything else -- and a
        # path that leaves the attachments folder -- is "not found", checked before the disk is asked whether it exists.
        if not re.fullmatch(r"[0-9a-f]{16}", attachment_id) or not filename or filename == ".." or any(
                mark in filename for mark in ("/", "\\", ":", "\x00")):
            return aiohttp.web.Response(status=404, text="Not found")
        base_dir = Path(getattr(att_store, "ampuly_home", None) or getattr(engine, "ampuly_home", None) or get_ampuly_home()) / "projects" / project_id / "attachments"
        as_download = str(request.query.get("download", "") or "").strip().lower() in {"1", "true", "yes"}
        if as_download and not _may_download(engine, request):
            # The 资料 link records a ``data_download``; it has to pass that action's role check, or the audit row
            # would claim an authorization nobody made. A chat's own <img> keeps the rule it always had.
            return _refuse(403, "owner_only")
        try:
            fd, size, workspace = await asyncio.to_thread(_open_attachment, base_dir, attachment_id, filename)
        except Exception:
            # Missing, a link, a second name for the same bytes, or outside the folder: all "not found", so the
            # reply says nothing about which.
            if as_download:
                _record_file_read(engine, request, project_id=project_id, action="data_download",
                                  object_type="attachment", object_id=f"attachments/{attachment_id}",
                                  outcome="not_found")
            return aiohttp.web.Response(status=404, text="Not found")
        # A picture or a video is what the chat renders; anything else is handed over instead of opened in a tab,
        # where a same-origin .html or .svg would read the login token out of localStorage.
        content_type, disposition = file_serving.content_type_for(filename, force_download=as_download)
        if as_download:
            _record_file_read(engine, request, project_id=project_id, action="data_download",
                              object_type="attachment", object_id=f"attachments/{attachment_id}",
                              metadata={"size": size})
        try:
            return await file_serving.stream_fd(request, fd, size=size, content_type=content_type,
                                                disposition=disposition, filename=filename)
        finally:
            workspace.__exit__(None, None, None)

    return _handle


async def _serve_no_build(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=(
            "<h1>Ampuly Office UI</h1>"
            "<p>Frontend not built. Run <code>cd ampuly/plugins/office_ui/frontend_src && npm install && npm run build</code></p>"
        ),
        content_type="text/html",
    )


# The Feishu SDK takes about two seconds to import. The first card push imported it on the event loop and held it
# there (B5 finding 41: "Event loop held for 2.0s ... _duty_watch_loop", importlib frames), so the console imports it
# once at start, on a worker thread (SM-1, 2026-09-27).
_FEISHU_SDK_MODULE = "lark_oapi"
# The startup hook's background tasks, held so they are not collected while they run; each drops out when done.
_startup_tasks: set[asyncio.Task[None]] = set()


async def _preimport_off_loop(module: str) -> None:
    """Import ``module`` on a worker thread. Never raises: a package that is not installed is nothing to do, and an
    import that fails is logged and left to fail again, and be reported, where the module is used."""
    if module in sys.modules:
        return
    try:
        await asyncio.to_thread(importlib.import_module, module)
    except ModuleNotFoundError:
        logger.debug("{} is not installed; nothing to import ahead", module)
    except Exception:
        logger.opt(exception=True).warning("{} could not be imported ahead; its first use will import it", module)


def _start_preimport(module: str) -> None:
    """Schedule ``_preimport_off_loop`` without waiting for it: the console starts serving at once either way."""
    try:
        task = asyncio.get_running_loop().create_task(_preimport_off_loop(module))
    except Exception:
        logger.opt(exception=True).debug("{} will be imported by its first use", module)
        return
    _startup_tasks.add(task)
    task.add_done_callback(_startup_tasks.discard)


async def _on_startup(app: aiohttp.web.Application) -> None:
    """Start the duty watch as soon as the console serves (B5).

    It used to start with the first browser connection, so after a restart with nobody at the console nothing
    resumed the interrupted runs, closed what ended orders left waiting or reconciled runs nobody controls. A browser
    that connects later finds the watch already running and does not start a second one; shutdown stops it as before.

    Before that, the Feishu SDK starts importing on a worker thread (SM-1): the first card the watch pushes no longer
    imports it on the loop. Startup does not wait for it.
    """
    _start_preimport(_FEISHU_SDK_MODULE)
    ws_handler = app.get("ws_handler")
    if ws_handler is not None:
        ws_handler._ensure_duty_watch_loop()
        # The event-loop watchdog starts with the console too (AV): a stall before the first browser
        # connects -- the duty watch resuming runs after a restart -- used to go unrecorded. It never blocks startup.
        try:
            ws_handler._ensure_loop_watchdog()
        except Exception:
            logger.opt(exception=True).warning("Event-loop watchdog did not start with the console")


async def _on_shutdown(app: aiohttp.web.Application) -> None:
    """Graceful cleanup."""
    ws_handler = app.get("ws_handler")
    if ws_handler:
        await ws_handler.shutdown()
        await ws_handler.flush_all_progress()
    engine = app.get("engine")
    db = app.get("db")
    if engine:
        await engine.shutdown()
    if db:
        await db.close()
    instance_lock = app.get("instance_lock")
    if instance_lock is not None:
        try:
            instance_lock.close()
        except Exception:
            pass
    logger.info("Office-UI server shut down")


# ── Entry point ───────────────────────────────────────────────────────

def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    config: AmpulyConfig | None = None,
    project_id: str | None = None,
) -> None:
    """Create and run the office-UI server (blocking)."""

    async def _start() -> None:
        app = await create_app(config=config, project_id=project_id)
        # The handler writes links (Feishu cards, duty notes) from where this console listens.  Not told, it fell
        # back to 8765 while the launcher served on 8766, and the owner's click got ERR_CONNECTION_REFUSED.
        handler = app.get("ws_handler")
        if handler is not None:
            handler._public_host, handler._public_port = host, port
        # `create_app` owns engine construction; retrieve the exact instance
        # wired into the application before applying the remote-listener guard.
        # Referencing a local `engine` variable here would fail only on the
        # non-loopback path, leaving LAN startup with an avoidable NameError.
        engine = app["engine"]
        if not _loopback_host(host) and _bootstrap_needed(engine):
            # Nobody has claimed the owner account yet: a LAN listener would
            # let any device on the network claim it first ( audit).
            # Refused before the runner's setup, whose startup hook starts the
            # duty watch: a console that will not serve starts nothing.
            raise SystemExit(
                f"refusing to listen on {host}: no owner account exists yet.{chr(10)}"
                "Start once on 127.0.0.1, create the owner account, then use --host."
            )
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Office-UI running at http://{host}:{port}")
        server_banner(host=host, port=port, project_id=project_id)
        # Say where this home's console listens, so a command typed in a terminal (which gets no AMPULY_UI_PORT) finds
        # this console and not another home's (runtime contract).
        from ampuly.cli.ui_bridge import forget_console_address, publish_console_address

        home = get_ampuly_home()
        try:
            publish_console_address(home, host=host, port=port)
        except OSError as exc:
            logger.warning(f"could not record where this console listens ({type(exc).__name__}); "
                           "terminal commands will look for it on AMPULY_UI_PORT or the default port")
        # Keep running until interrupted
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            forget_console_address(home)
            await runner.cleanup()

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        terminal_status("Shutting down Office UI", kind="warning")
    except SystemExit as exc:
        if exc.code and not isinstance(exc.code, int):
            terminal_status(str(exc.code), kind="error")
            raise SystemExit(1) from None
        raise
