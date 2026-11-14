"""Private HTTP transport for owner-opted-in task-window observations (FE-EB-22).

An authenticated owner click is the UI-purpose consent, independent of the
model's Effect approval. No bootstrap fallback, model tool, image in WS ACKs,
new capture loop, runtime creation, durable image or account token in a URL.
"""
from __future__ import annotations

import hashlib
import re
from aiohttp import web

from ampuly.computer_use.desktop_ui_frames import DesktopViewRefused

CONSENT = 'task-window-readonly-v1'
VIEW_HEADER = 'X-Ampuly-Desktop-View'
ID_HEADER = 'X-Ampuly-Desktop-View-Id'
NO_STORE = {'Cache-Control': 'no-store, private, max-age=0', 'Pragma': 'no-cache',
            'X-Content-Type-Options': 'nosniff', 'Cross-Origin-Resource-Policy': 'same-origin',
            'Vary': 'Authorization, Cookie'}


def _scope_text(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def _token(request):
    from ampuly.plugins.office_ui.server import SESSION_COOKIE
    header = request.headers.get('Authorization', '')
    value = header[7:].strip() if header.lower().startswith('bearer ') else ''
    value = value or request.cookies.get(SESSION_COOKIE, '')
    return value if type(value) is str and 0 < len(value) <= 8192 else ''


def _owner(account):
    return (account is not None and getattr(account, 'role', None) == 'owner'
            and getattr(account, 'disabled', None) is False
            and type(getattr(account, 'login', None)) is str and bool(account.login))


def _ready(store):
    value = getattr(store, 'is_ready', False)
    return (value() if callable(value) else value) is True


async def _context(request, handler):
    from ampuly.plugins.office_ui.server import _account_store_for
    from ampuly.computer_use.desktop_runtime_binding import DesktopRuntimeBinding

    if request.headers.get(VIEW_HEADER) != '1':
        raise DesktopViewRefused('desktop_view_explicit_request_required')
    origin = request.headers.get('Origin')
    if origin and origin != f'{request.scheme}://{request.host}':
        raise DesktopViewRefused('desktop_view_origin_refused')
    project, task_id = request.match_info['project_id'], request.match_info['task_id']
    if not _scope_text(project) or not _scope_text(task_id):
        raise DesktopViewRefused('desktop_view_scope_invalid')
    root = getattr(handler, '_root_engine', None) or getattr(handler, 'engine', None)
    accounts = _account_store_for(root)
    token = _token(request)
    account = accounts.resolve_token(token) if token else None
    if not _owner(account):
        raise DesktopViewRefused('desktop_view_owner_required')
    identity = (account.login, tuple(account.customer_ids), account.proxy_for)
    viewer = (account.login, hashlib.sha256(token.encode()).hexdigest())
    projects = handler._project_engines()
    engine = projects.peek(project)
    store = getattr(engine, 'store', None)
    connection = getattr(store, '_db', None)
    manager = getattr(engine, '_ampuly_desktop_runtime_binding', None)

    def fresh():
        current = accounts.resolve_token(token)
        if ((getattr(handler, '_root_engine', None) or getattr(handler, 'engine', None)) is not root
                or _account_store_for(root) is not accounts
                or not _owner(current) or (current.login, tuple(current.customer_ids), current.proxy_for) != identity
                or projects.peek(project) is not engine or getattr(engine, 'project_id', None) != project
                or getattr(engine, 'store', None) is not store or getattr(store, '_db', None) is not connection
                or getattr(store, 'project_id', None) != project or not _ready(store)
                or type(manager) is not DesktopRuntimeBinding or manager.engine is not engine
                or getattr(engine, '_ampuly_desktop_runtime_binding', None) is not manager):
            return False
        services = getattr(handler, 'services_context', None)
        destroyed = getattr(services, 'project_is_destroyed', None)
        if callable(destroyed) and destroyed(project):
            return False
        if current.customer_ids:
            bindings = accounts.business.project_customers()
            if str(bindings.get(project, '') or '') not in current.customer_ids:
                return False
        return True

    getter = getattr(store, 'get_task', None)
    if connection is None or not callable(getter) or not fresh():
        raise DesktopViewRefused('desktop_view_source_unavailable')
    task = await getter(task_id)
    # Lookup yielded: never trust the old account, store or project source afterward.
    if not fresh():
        raise DesktopViewRefused('desktop_view_source_changed')
    if task is None or getattr(task, 'id', None) != task_id or getattr(task, 'project_id', None) != project:
        raise DesktopViewRefused('desktop_view_task_unavailable')
    return manager.ui_frames, task, viewer, fresh


def register_desktop_view_http(app, handler):
    async def handle(request):
        try:
            projection, task, viewer, fresh = await _context(request, handler)
            if request.method == 'POST':
                if request.content_length is None or request.content_length > 512:
                    raise DesktopViewRefused('desktop_view_consent_invalid')
                data = await request.json()
                if type(data) is not dict or data != {'consent': CONSENT}:
                    raise DesktopViewRefused('desktop_view_consent_invalid')
                result = projection.open(task, viewer, fresh)
                return web.json_response(result, headers=NO_STORE)
            view_id = request.headers.get(ID_HEADER, '')
            if re.fullmatch(r'[0-9a-f]{32}', view_id) is None:
                raise DesktopViewRefused('desktop_view_unavailable')
            if request.method == 'DELETE':
                projection.close(view_id, viewer)
                return web.Response(status=204, headers=NO_STORE)
            frame = projection.read(task, view_id, viewer)
            if frame is None:
                return web.Response(status=204, headers=NO_STORE)
            png, metadata = frame
            # A finite PNG response on its own transport, never the WS control FIFO.
            return web.Response(body=png, content_type='image/png', headers={**NO_STORE,
                'X-Ampuly-Frame-Sequence': str(metadata['sequence']),
                'X-Ampuly-Frame-At': str(metadata['produced_at']),
                'X-Ampuly-Frame-Remaining-Ms': str(metadata['remaining_ms']),
                'X-Ampuly-View-Expires': str(metadata['expires_at'])})
        except DesktopViewRefused as exc:
            return web.json_response({'ok': False, 'error': exc.code}, status=409, headers=NO_STORE)
        except Exception:
            # Never return tokens, paths, account values or native exception text.
            return web.json_response({'ok': False, 'error': 'desktop_view_unavailable'}, status=409, headers=NO_STORE)

    path = '/api/desktop-view/{project_id}/{task_id}'
    app.router.add_post(path, handle)
    app.router.add_get(path, handle, allow_head=False)
    app.router.add_delete(path, handle)
