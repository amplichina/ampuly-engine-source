"""Server-private, current socket identity ONLY; never an execution grant.

The server supplies its already-owned exact AccountStore. No constructor,
configuration, disk path, bootstrap owner, action predicate or worker is invoked
here. Token resolution uses the original store method and its current getter;
the repository I/O remains that original method's responsibility.

Returned Account objects are mutable current observations, NOT frozen facts or
durable permission. The future WS route must still enforce its original action,
project/customer scope and bootstrap rules. This leaf is not yet wired there.
"""
from __future__ import annotations

from functools import wraps
import math
from threading import RLock
from typing import Any

from .accounts import Account, AccountStore, ROLES, _LOGIN_RE, time as _account_time


MAX_SOCKET_BINDINGS = 256
MAX_TOKEN_BYTES = 512
MAX_SCOPE_BYTES = 256
MAX_CUSTOMERS = 256
MAX_GENERATION = 2**63 - 1
_CODES = ("invalid_source", "unavailable", "binding_changed", "capacity_exceeded", "invalid_token")
_MISSING = object()
_LOCK = RLock()
# One module/process-local cap across all managers. Strong socket references
# prevent id reuse while registered; no hash/repr/bool of a socket is called.
_BINDINGS: dict[int, _SocketSlot] = {}


class SocketPrincipalBindingError(ValueError):
    """Only a fixed code/message; no token, account, path or private cause."""

    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _CODES else "unavailable"
        super().__init__(self.code)


def _socket_key(ws: Any) -> Any:
    """The socket an internal recorder stands in for, never the recorder itself."""
    try:
        from .ws_handler import _socket_behind
        return _socket_behind(ws)
    except Exception:
        return ws


def scope_with_store_less_fallback(resolve: Any, handler: Any, ws: Any) -> tuple[str, ...]:
    """Customer scope for the socket, tolerating a handler with no principal store.

    A real store's binding fault still raises (callers fail closed); only a
    store-less console double falls back to its cached account, and an unknown
    socket carries no binding.
    """
    try:
        account = resolve(handler, ws)
    except SocketPrincipalBindingError as exc:
        if exc.code != "unavailable" or getattr(handler, "_account_store", None) is not None:
            raise
        account = (getattr(handler, "_client_accounts", None) or {}).get(_socket_key(ws))
    return tuple(getattr(account, "customer_ids", ()) or ())


def principal_with_store_less_fallback(handler: Any, ws: Any, resolve: Any, *,
                                         auth_required: Any) -> tuple[Any, bool]:
    """Routing's account plus whether the principal could not be established.

    A real store's fault fails closed. Only a handler with no principal store
    (the CLI or a console double, where auth is not required) keeps the old
    local-owner rule instead of being rejected before its action runs.
    """
    try:
        return resolve(handler, ws), False
    except SocketPrincipalBindingError as exc:
        if exc.code == "unavailable" and getattr(handler, "_account_store", None) is None:
            try:
                required = bool(auth_required())
            except Exception:
                return None, True
            if not required:
                from types import SimpleNamespace
                return SimpleNamespace(login="owner", role="owner", customer_ids=()), False
        return None, True
    except Exception:
        return None, True


def store_less_owner_actor(handler: Any, ws: Any) -> dict[str, str] | None:
    """Owner actor for a handler with no principal store (a console double or CLI).

    Its cached account is the only account it has, so a cached employee is never
    promoted to the local owner. With no cached account the original rule holds:
    the local console is the owner only while auth is not required.
    """
    account = (getattr(handler, "_client_accounts", None) or {}).get(_socket_key(ws))
    if account is not None:
        return {"kind": "authenticated_account",
                "login": getattr(account, "login", ""), "role": getattr(account, "role", "")}
    getter = getattr(handler, "_accounts", None)
    if not callable(getter):
        return None
    try:
        if getter().auth_required():
            return None
    except Exception:
        return None
    return {"kind": "local_console_owner", "login": "owner", "role": "owner"}


class _PrincipalFault(Exception):
    pass


def _fault(code: str = "invalid_source") -> None:
    raise _PrincipalFault(code)


def _boundary(method):
    @wraps(method)
    def call(*args, **kwargs):
        failure = None
        try:
            return method(*args, **kwargs)
        except _PrincipalFault as exc:
            failure = exc.args[0] if (len(exc.args) == 1 and type(exc.args[0]) is str
                                     and exc.args[0] in _CODES) else "unavailable"
        except SocketPrincipalBindingError as exc:
            failure = exc.code if type(exc) is SocketPrincipalBindingError else "unavailable"
        except Exception:
            failure = "unavailable"
        # The method's with/except scopes have ended. External active-except
        # context and traceback locals are not globally scrubbed. Cancellation
        # and every other BaseException propagate as the original object.
        raise SocketPrincipalBindingError(failure) from None
    return call


def _async_boundary(method):
    @wraps(method)
    async def call(*args, **kwargs):
        failure = None
        try:
            return await method(*args, **kwargs)
        except _PrincipalFault as exc:
            failure = exc.args[0] if (len(exc.args) == 1 and type(exc.args[0]) is str
                                     and exc.args[0] in _CODES) else "unavailable"
        except SocketPrincipalBindingError as exc:
            failure = exc.code if type(exc) is SocketPrincipalBindingError else "unavailable"
        except Exception:
            failure = "unavailable"
        raise SocketPrincipalBindingError(failure) from None
    return call


def _text(value: Any, *, maximum: int, empty: bool = False) -> str:
    if (type(value) is not str or (not value and not empty) or len(value) > maximum
            or value.strip() != value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        _fault()
    invalid = False
    try:
        invalid = len(value.encode("utf-8")) > maximum
    except UnicodeError:
        invalid = True
    if invalid:
        _fault()
    return value


class _SocketPrincipalBinding:
    """Opaque identity reference: no credentials, Account or permission fields."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "<SocketPrincipalBinding>"

    def __reduce_ex__(self, protocol):
        raise TypeError("socket principal binding is private")

    def __reduce__(self):
        raise TypeError("socket principal binding is private")

    def __getstate__(self):
        raise TypeError("socket principal binding is private")


class _SocketSlot:
    __slots__ = ("owner", "ws", "token", "login", "generation", "binding")

    def __init__(self, owner, ws, token, login, generation, binding):
        for name, value in (("owner", owner), ("ws", ws), ("token", token),
                            ("login", login), ("generation", generation), ("binding", binding)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("socket principal slot is immutable")

    def __delattr__(self, name):
        raise AttributeError("socket principal slot is immutable")

    def __repr__(self) -> str:
        return "<PrivateSocketPrincipalSlot>"

    def __reduce_ex__(self, protocol):
        raise TypeError("socket principal slot is private")

    def __reduce__(self):
        raise TypeError("socket principal slot is private")

    def __getstate__(self):
        raise TypeError("socket principal slot is private")


def _guard(owner, ws) -> None:
    if (type(owner) is not SocketPrincipalBindings or ws is None
            or type(owner._store) is not AccountStore
            or type(_BINDINGS) is not dict or len(_BINDINGS) > MAX_SOCKET_BINDINGS):
        _fault()


def _slot(owner, ws):
    """Caller holds _LOCK; only a fixed identity lookup, no map enumeration."""
    _guard(owner, ws)
    key = id(ws)
    found = _BINDINGS.get(key, _MISSING)
    if found is _MISSING:
        return None
    if (type(found) is not _SocketSlot or found.owner is not owner or found.ws is not ws
            or type(found.binding) is not _SocketPrincipalBinding
            or type(found.generation) is not int
            or not 1 <= found.generation <= MAX_GENERATION):
        _fault()
    _text(found.token, maximum=MAX_TOKEN_BYTES)
    login = _text(found.login, maximum=64)
    if _LOGIN_RE.fullmatch(login) is None:
        _fault()
    return found


def _raw_fields(account):
    if type(account) is not Account:
        _fault()
    fields = vars(account)
    if type(fields) is not dict or not 5 <= len(fields) <= 32:
        _fault()
    size = len(fields)
    result = tuple(fields.get(name, _MISSING) for name in (
        "login", "role", "employee_id", "disabled", "customer_ids"))
    if len(fields) != size:
        _fault("binding_changed")
    return result


def _validate_account_fields(fields, *, expected_login=None):
    login, role, employee_id, disabled, customers = fields
    _text(login, maximum=64)
    if _LOGIN_RE.fullmatch(login) is None:
        _fault()
    if expected_login is not None and login != expected_login:
        _fault("binding_changed")
    if type(disabled) is not bool or disabled is not False:
        _fault("unavailable")
    if type(role) is not str or role not in ROLES:
        _fault()
    _text(employee_id, maximum=MAX_SCOPE_BYTES, empty=role != "employee")
    if role == "customer" and employee_id != "":
        _fault()
    if type(customers) is not tuple or len(customers) > MAX_CUSTOMERS:
        _fault()
    count = len(customers)
    if (role == "owner" and count != 0) or (role == "customer" and count == 0):
        _fault()
    seen = set()
    for index in range(count):
        if len(customers) != count:
            _fault("binding_changed")
        item = _text(customers[index], maximum=MAX_SCOPE_BYTES)
        if item in seen:
            _fault()
        seen.add(item)
    if len(customers) != count:
        _fault("binding_changed")
    return fields


def _account_fields(account, *, expected_login=None):
    fields = _validate_account_fields(_raw_fields(account), expected_login=expected_login)
    again = _validate_account_fields(_raw_fields(account), expected_login=expected_login)
    # Both sides are validated scalars/tuples before equality: a concurrently
    # inserted arbitrary field object is not allowed to run an __eq__ hook.
    if again != fields:
        _fault("binding_changed")
    return fields


def _token_entry(store, token):
    tokens = store._tokens
    if type(tokens) is not dict:
        _fault()
    entry = tokens.get(token, _MISSING)
    if entry is _MISSING:
        # The first bind after a normal restart has no process-local tuple.
        # Only the exact AccountStore's current durable digest reader may
        # restore it; caller input alone never creates a session.
        from .current_account_principal import CurrentAccountPrincipalError
        try:
            restored = AccountStore._capture_token_current(store, token)
        except CurrentAccountPrincipalError as exc:
            _fault(exc.code if exc.code in ("invalid_source", "unavailable") else "unavailable")
        if restored is None:
            _fault("invalid_token")
        if store._tokens is not tokens or restored[1] is not tokens:
            _fault("binding_changed")
        entry = tokens.get(token, _MISSING)
    if type(entry) is not tuple or len(entry) != 2:
        _fault()
    login_key, expires = entry
    _text(login_key, maximum=64)
    if _LOGIN_RE.fullmatch(login_key) is None or login_key.lower() != login_key:
        _fault()
    if (type(expires) not in (int, float) or expires <= 0
            or (type(expires) is int and expires > MAX_GENERATION)
            or (type(expires) is float and not math.isfinite(expires))):
        _fault()
    return tokens, entry, login_key, expires


def _finish_current_account(owner, token, captured, account, source_failed, *, expected_login=None):
    tokens, entry, login_key, expires = captured
    if source_failed:
        # A repository/source fault is not evidence of expired credentials.
        # Keep tuple/generation checks and let the client retain its token.
        if owner._store._tokens is not tokens:
            _fault("binding_changed")
        after = tokens.get(token, _MISSING)
        if after is _MISSING:
            _fault("unavailable")
        if after is not entry:
            _fault("binding_changed")
        _fault("unavailable")
    if account is None:
        if owner._store._tokens is not tokens:
            _fault("binding_changed")
        after = tokens.get(token, _MISSING)
        if after is not _MISSING and after is not entry:
            _fault("binding_changed")
        # The current resolver completed normally and proved invalidity.
        _fault("invalid_token")
    fields = _account_fields(account, expected_login=expected_login)
    # Original resolve_token gets an entry BEFORE get/_load. A logout/revoke
    # during that read otherwise returns an Account after its session is gone.
    # Only this already-owned store's exact memory entry is rechecked; no new
    # durable source, session, authority or grant is manufactured.
    if owner._store._tokens is not tokens:
        _fault("binding_changed")
    after = tokens.get(token, _MISSING)
    if after is _MISSING:
        _fault("unavailable")
    if after is not entry:
        _fault("binding_changed")
    if fields[0].lower() != login_key:
        _fault()
    now = _account_time.time()
    if (type(now) not in (int, float) or now < 0
            or (type(now) is int and now > MAX_GENERATION)
            or (type(now) is float and not math.isfinite(now))):
        _fault("unavailable")
    if now > expires:  # Keep the original store's exact expiry comparison.
        _fault("unavailable")
    return account, fields[0]


def _current_account(owner, token, *, expected_login=None):
    captured = _token_entry(owner._store, token)
    # Exact base call: no instance get_current/resolve override is authority.
    source_failed = False
    account = None
    try:
        account = AccountStore.resolve_token_current(owner._store, token)
    except Exception:
        source_failed = True
    return _finish_current_account(owner, token, captured, account, source_failed,
                                   expected_login=expected_login)


async def _current_account_async(owner, token, *, reader, expected_login=None):
    captured = _token_entry(owner._store, token)
    source_failed = False
    account = None
    try:
        account = await AccountStore.resolve_token_current_async(owner._store, token, reader=reader)
    except Exception:
        source_failed = True
    return _finish_current_account(owner, token, captured, account, source_failed,
                                   expected_login=expected_login)


def _expected(value):
    if value is not None and type(value) is not _SocketPrincipalBinding:
        _fault()


class SocketPrincipalBindings:
    """One server-owned store, process-local socket bindings, no grant API.

    None from resolve means healthy UNBOUND only. A revoked/expired/deleted
    principal or stale expected handle raises; never fall back to cached data
    or synthesize a bootstrap owner. The original auth_required/action/scope
    rules remain outside this class and must still run in the future WS host.
    """
    __slots__ = ("_store",)

    @_boundary
    def __init__(self, store: AccountStore):
        if type(self) is not SocketPrincipalBindings or type(store) is not AccountStore:
            _fault()
        object.__setattr__(self, "_store", store)

    def __setattr__(self, name, value):
        raise AttributeError("socket principal store is fixed")

    def __delattr__(self, name):
        raise AttributeError("socket principal store is fixed")

    def __repr__(self) -> str:
        return "<SocketPrincipalBindings>"

    def __reduce_ex__(self, protocol):
        raise TypeError("socket principal manager is private")

    def __reduce__(self):
        raise TypeError("socket principal manager is private")

    def __getstate__(self):
        raise TypeError("socket principal manager is private")

    @_boundary
    def bind(self, ws: object, token: str) -> _SocketPrincipalBinding:
        _guard(self, ws)
        _text(token, maximum=MAX_TOKEN_BYTES)
        with _LOCK:
            before = _slot(self, ws)
            if before is None and len(_BINDINGS) >= MAX_SOCKET_BINDINGS:
                _fault("capacity_exceeded")
            generation = 1 if before is None else before.generation + 1
            if generation > MAX_GENERATION:
                _fault("capacity_exceeded")
        account, login = _current_account(self, token)
        with _LOCK:
            if _slot(self, ws) is not before:
                _fault("binding_changed")
            if before is None and len(_BINDINGS) >= MAX_SOCKET_BINDINGS:
                _fault("capacity_exceeded")
            # Even same-token rebind gets another identity: old finally must
            # not forget a later authenticated binding with identical bytes.
            binding = _SocketPrincipalBinding()
            _BINDINGS[id(ws)] = _SocketSlot(self, ws, token, login, generation, binding)
            return binding

    def _capture_resolution(self, ws: object, expected_binding):
        _guard(self, ws)
        _expected(expected_binding)
        with _LOCK:
            captured = _slot(self, ws)
            if captured is None:
                if expected_binding is not None:
                    _fault("binding_changed")
                return None
            if expected_binding is not None and captured.binding is not expected_binding:
                _fault("binding_changed")
            return captured

    def _finish_resolution(self, ws: object, captured, account):
        with _LOCK:
            if _slot(self, ws) is not captured:
                _fault("binding_changed")
            return account

    @_boundary
    def resolve(self, ws: object, *, expected_binding=None) -> Account | None:
        captured = SocketPrincipalBindings._capture_resolution(self, ws, expected_binding)
        if captured is None:
            return None
        account, _login = _current_account(self, captured.token, expected_login=captured.login)
        return SocketPrincipalBindings._finish_resolution(self, ws, captured, account)

    @_async_boundary
    async def resolve_async(self, ws: object, *, reader, expected_binding=None) -> Account | None:
        captured = SocketPrincipalBindings._capture_resolution(self, ws, expected_binding)
        if captured is None:
            return None
        account, _login = await _current_account_async(
            self, captured.token, reader=reader, expected_login=captured.login,
        )
        return SocketPrincipalBindings._finish_resolution(self, ws, captured, account)

    @_boundary
    def forget(self, ws: object, *, expected_binding: _SocketPrincipalBinding) -> bool:
        _guard(self, ws)
        if type(expected_binding) is not _SocketPrincipalBinding:
            _fault()
        with _LOCK:
            current = _slot(self, ws)
            if current is None or current.binding is not expected_binding:
                return False
            del _BINDINGS[id(ws)]
            return True
