"""One server-private socket-bound Stop request, not a token or grant API.

The original B57 manager owns the socket binding and AccountStore. The future
owned host must perform its real synchronous transaction/current-owner checks
and confirm commit plus close before returning. This bridge neither resolves
cached Accounts nor changes token/binding state, opens a database, stops a
worker, refunds money or infers that external effects have been rolled back.
"""
from __future__ import annotations

from . import socket_principal_binding as _socket
from .launch_stop_intent import LaunchStopIntentError, LaunchStopIntentFact


_PRE_CODES = {
    'invalid_source':'invalid_source',
    'unavailable':'unavailable',
    'binding_changed':'conflict',
    'capacity_exceeded':'unavailable',
}


def request_socket_launch_stop(
    manager, ws, *, expected_binding, business_store, source, reason,
) -> LaunchStopIntentFact:
    """Request through one original exact socket binding and private host.

    expected_binding is a server-retained original opaque identity, never a
    decoded wire object. No healthy-unbound/bootstrap fallback is supported.
    Only the owned host's committed receipt is returned, never a raw token.
    Post-host binding uncertainty is outcome_unknown, not a claim of no write.
    An original host LaunchStopIntentError or BaseException is not replaced.
    """
    failure = None
    captured = store = token = login = generation = None
    try:
        # Capture under the original manager lock. No map enumeration, current
        # resolver, Account/display projection, or caller permission callback.
        with _socket._LOCK:
            _socket._guard(manager, ws)
            _socket._expected(expected_binding)
            if type(expected_binding) is not _socket._SocketPrincipalBinding:
                _socket._fault('invalid_source')
            captured = _socket._slot(manager, ws)
            if captured is None:
                failure = 'forbidden'
            elif captured.binding is not expected_binding:
                _socket._fault('binding_changed')
            else:
                store = manager._store
                if type(store) is not _socket.AccountStore:
                    _socket._fault('invalid_source')
                # _slot already checks bounded exact token/login/generation.
                # Retain identity of immutable slot fields, not an Account.
                token, login, generation = captured.token, captured.login, captured.generation
                if manager._store is not store or _socket._slot(manager, ws) is not captured:
                    _socket._fault('binding_changed')
    except _socket._PrincipalFault as exc:
        code = (exc.args[0] if type(exc) is _socket._PrincipalFault
                and type(exc.args) is tuple and len(exc.args)==1 and type(exc.args[0]) is str else None)
        failure = _PRE_CODES.get(code,'unavailable')
    except _socket.SocketPrincipalBindingError as exc:
        code = exc.code if type(exc) is _socket.SocketPrincipalBindingError else None
        failure = _PRE_CODES.get(code,'unavailable') if type(code) is str else 'unavailable'
    except Exception:
        failure = 'unavailable'
    if failure is not None:
        # Outside the handler: no private B57 exception is attached.
        raise LaunchStopIntentError(failure) from None

    dependency_failed = False
    try:
        # This host is a separate Root-frozen dependency, not an existing
        # fallback or a caller-supplied hook. No host constructor is run here.
        from .launch_stop_host import request_launch_stop_owned
    except Exception:
        dependency_failed = True
    if dependency_failed:
        raise LaunchStopIntentError('unavailable') from None

    result = None
    failure = None
    try:
        # Original global binding lock is not held over owned native I/O.
        # The host itself verifies AccountStore/Business ownership, the real
        # token entry, expiry and current owner before and after its write.
        # It supplies the timestamp; the socket caller cannot choose one.
        result = request_launch_stop_owned(
            business_store=business_store, account_store=store, token=token,
            source=source, reason=reason,
        )
    except LaunchStopIntentError:
        # In particular, preserve outcome_unknown as the original host error.
        # Do not recategorize forbidden/conflict as successful cancellation.
        raise
    except Exception:
        # A non-contract ordinary host failure has no proven write phase.
        failure = 'outcome_unknown'
    if failure is not None:
        raise LaunchStopIntentError(failure) from None

    failure = None
    try:
        with _socket._LOCK:
            _socket._guard(manager, ws)
            _socket._expected(expected_binding)
            current = _socket._slot(manager, ws)
            if (type(result) is not LaunchStopIntentFact or current is not captured
                    or manager._store is not store or type(store) is not _socket.AccountStore
                    or captured.owner is not manager or captured.ws is not ws
                    or captured.binding is not expected_binding
                    or captured.token is not token or captured.login is not login
                    or captured.generation != generation):
                failure = 'outcome_unknown'
    except Exception:
        # The host may have committed: malformed/revoked/rebound post-state is
        # never mapped to pre-write forbidden/conflict or a healthy None.
        failure = 'outcome_unknown'
    if failure is not None:
        raise LaunchStopIntentError(failure) from None
    return result

