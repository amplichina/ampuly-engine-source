"""Console accounts: the owner and the sub-accounts of human employees.

Architecture step 1 (docs/ARCHITECTURE-LAYERS.zh.md): people enter the
system through a sub-account on the same console. This module is the whole
of that mechanism, plugin-side, no kernel change:

* accounts live in ``<ampuly_home>/accounts.json`` (runtime data, never in the
  repo); passwords are stored as PBKDF2-SHA256 hashes with a per-account
  salt, never in clear;
* while no account exists the console stays open, exactly as before; the
  moment the owner account is created every connection has to log in;
* a successful login returns a random token; only its SHA256 and fixed
  expiry are persisted. Restarting can recover an active session within its
  original 12 hours; revocation and current account scope still apply;
* roles are ``owner`` (everything), ``employee`` (their own work only) and,
  since S3-6A (2026-09-27), ``customer`` (its own orders only, read, plus its
  acceptance decision); an employee account is linked to an employee in the
  org directory, a customer account is always bound to its customer ids.
  No customer account is created by this module on its own: the owner makes
  one when a customer is to log in, and that first contact is a decision
  reported before it is made.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROLES = ("owner", "employee", "customer")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.@\-]{2,64}$")
_PBKDF2_ROUNDS = 200_000
_TOKEN_TTL_SECONDS = 12 * 3600
_SESSION_LOCK = threading.RLock()


@dataclass
class Account:
    login: str
    role: str = "employee"
    employee_id: str = ""
    display_name: str = ""
    disabled: bool = False
    created_at: str = ""
    last_login_at: str = ""
    # Customers this account may see.  Empty means unrestricted, which is
    # what an owner gets; an employee bound to customers can never widen it.
    # A customer account is never empty here: it IS one of these customers.
    customer_ids: tuple[str, ...] = ()
    # A proxy owner signs for this login; kept apart from the login itself so
    # the review fact names the person represented, not only the device.
    proxy_for: str = ""

    @property
    def customer_scope_is_unrestricted(self) -> bool:
        return not self.customer_ids

    def may_see_customer(self, customer_id: str) -> bool:
        clean = str(customer_id or "").strip()
        if self.customer_scope_is_unrestricted:
            return True
        return bool(clean) and clean in self.customer_ids

    def to_payload(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "role": self.role,
            "employee_id": self.employee_id,
            "display_name": self.display_name or self.login,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "customer_ids": list(self.customer_ids),
            "proxy_for": self.proxy_for,
        }


@dataclass
class _Record:
    account: Account
    salt: str
    password_hash: str


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()


class AccountStore:
    """Accounts live in business.db; a legacy accounts.json is imported once."""

    def __init__(self, ampuly_home: Path | str, business: Any | None = None) -> None:
        from ampuly.plugins.office_ui.business_store import BusinessStore

        self.home = Path(ampuly_home)
        self.path = self.home / "accounts.json"  # legacy location, import-only
        self.business = business or BusinessStore(self.home)
        self._tokens: dict[str, tuple[str, float]] = {}
        self._import_legacy_json()

    # ── persistence ──
    def _import_legacy_json(self) -> None:
        if not self.path.is_file() or self.business.account_count() > 0:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in list(raw.get("accounts", []) or []) if isinstance(raw, dict) else []:
            if isinstance(item, dict) and str(item.get("login", "") or "").strip():
                self.business.upsert_account(item)
        try:
            os.replace(self.path, self.path.with_suffix(".json.imported"))
        except OSError:
            pass

    def _load(self) -> dict[str, _Record]:
        records: dict[str, _Record] = {}
        proxy_rows = getattr(self.business, "account_proxy_rows", None)
        proxies = ({str(row.get("login", "") or "").strip().lower():
                    str(row.get("proxy_for", "") or "").strip()
                    for row in proxy_rows()}
                   if callable(proxy_rows) else {})
        for item in self.business.account_rows():
            login = str(item.get("login", "") or "").strip()
            if not login:
                continue
            records[login.lower()] = _Record(
                account=Account(
                    login=login,
                    role=str(item.get("role", "employee") or "employee"),
                    employee_id=str(item.get("employee_id", "") or ""),
                    display_name=str(item.get("display_name", "") or ""),
                    disabled=bool(item.get("disabled", False)),
                    created_at=str(item.get("created_at", "") or ""),
                    last_login_at=str(item.get("last_login_at", "") or ""),
                    customer_ids=tuple(
                        part.strip()
                        for part in str(item.get("customer_ids", "") or "").split(",")
                        if part.strip()
                    ),
                    proxy_for=proxies.get(login.lower(), ""),
                ),
                salt=str(item.get("salt", "") or ""),
                password_hash=str(item.get("password_hash", "") or ""),
            )
        return records

    def _save(self, records: dict[str, _Record]) -> None:
        for rec in records.values():
            self.business.upsert_account({
                **rec.account.to_payload(),
                "display_name": rec.account.display_name,
                "salt": rec.salt,
                "password_hash": rec.password_hash,
            })
            set_proxy = getattr(self.business, "set_account_proxy", None)
            if callable(set_proxy):
                set_proxy(rec.account.login, rec.account.proxy_for)

    # ── queries ──
    def auth_required(self) -> bool:
        dependency_failed = False
        try:
            from .business_store import BusinessStore
            from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        except Exception:
            dependency_failed = True
        if dependency_failed:
            raise RuntimeError("当前账号记录暂时无法读取。") from None
        failure = None
        result = None
        try:
            if type(self) is not AccountStore:
                raise _CurrentError("invalid_source")
            business = getattr(self, "business", None)
            if type(business) is not BusinessStore:
                raise _CurrentError("invalid_source")
            result = BusinessStore._has_accounts_current(business, _account_store=self)
        except Exception as exc:
            failure = "unavailable"
            if (type(exc) is _CurrentError and type(exc.code) is str
                    and exc.code in ("invalid_source", "unavailable")):
                failure = exc.code
        if failure is not None:
            raise _CurrentError(failure) from None
        return result

    def bootstrap_needed(self) -> bool:
        return not AccountStore.auth_required(self)

    def list_accounts(self) -> list[Account]:
        return [rec.account for rec in self._load().values()]

    def get(self, login: str) -> Account | None:
        rec = self._load().get(str(login or "").strip().lower())
        return rec.account if rec else None

    # ── commands ──
    @staticmethod
    def _clean_customer_ids(customer_ids: Any) -> tuple[str, ...]:
        if isinstance(customer_ids, str):
            parts = customer_ids.split(",")
        else:
            parts = list(customer_ids or [])
        seen: list[str] = []
        for part in parts:
            value = str(part or "").strip()
            if value and value not in seen:
                seen.append(value)
        return tuple(seen)

    def set_customer_scope(self, login: str, customer_ids: Any) -> Account:
        """Bind an account to the customers it may see; empty means every customer.

        Owners stay unrestricted: narrowing the only account that can widen a
        scope would leave nobody able to repair a mistake.
        """
        records = self._load()
        rec = records.get(str(login or "").strip().lower())
        if rec is None:
            raise KeyError("account not found")
        cleaned = self._clean_customer_ids(customer_ids)
        if rec.account.role == "owner" and cleaned:
            raise ValueError("an owner account is not bound to customers")
        if rec.account.role == "customer" and not cleaned:
            # Unbound would mean unrestricted; a customer with no customer is a door to everyone's orders.
            raise ValueError("a customer account must be bound to at least one customer")
        rec.account.customer_ids = cleaned
        self._save(records)
        return rec.account

    def create(self, login: str, password: str, *, role: str = "employee", employee_id: str = "", display_name: str = "", customer_ids: Any = (), proxy_for: str = "") -> Account:
        login = str(login or "").strip()
        if not _LOGIN_RE.match(login):
            raise ValueError("login must be 2-64 letters, digits, '_', '.', '@' or '-'")
        if len(str(password or "")) < 6:
            raise ValueError("password must be at least 6 characters")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        records = self._load()
        if login.lower() in records:
            raise ValueError("login already exists")
        if role == "employee" and not str(employee_id or "").strip():
            raise ValueError("an employee account must be linked to an employee")
        if role == "customer" and str(employee_id or "").strip():
            raise ValueError("a customer account is not linked to an employee")
        salt = secrets.token_hex(16)
        cleaned_customers = self._clean_customer_ids(customer_ids)
        if role == "owner" and cleaned_customers:
            raise ValueError("an owner account is not bound to customers")
        clean_proxy = str(proxy_for or "").strip()
        if clean_proxy:
            if role != "owner":
                raise ValueError("only an owner account can act as a proxy")
            if clean_proxy.lower() == login.lower():
                raise ValueError("an owner cannot proxy itself")
            target = records.get(clean_proxy.lower())
            if target is None or target.account.role != "owner" or target.account.proxy_for:
                raise ValueError("a proxy must name a non-proxy owner")
            if target.account.disabled:
                raise ValueError("a proxy cannot be bound to a disabled owner")
        if role == "customer" and not cleaned_customers:
            raise ValueError("a customer account must be bound to at least one customer")
        account = Account(
            login=login,
            role=role,
            employee_id=str(employee_id or "").strip(),
            display_name=str(display_name or "").strip(),
            created_at=datetime.now().isoformat(),
            customer_ids=cleaned_customers,
            proxy_for=clean_proxy,
        )
        records[login.lower()] = _Record(account=account, salt=salt, password_hash=_hash(password, salt))
        self._save(records)
        return account

    def set_password(self, login: str, password: str) -> None:
        if len(str(password or "")) < 6:
            raise ValueError("password must be at least 6 characters")
        records = self._load()
        rec = records.get(str(login or "").strip().lower())
        if rec is None:
            raise KeyError("account not found")
        rec.salt = secrets.token_hex(16)
        rec.password_hash = _hash(password, rec.salt)
        self._save(records)
        AccountStore._forget_login_tokens(self, login)

    def active_owner_logins(self) -> list[str]:
        """Every enabled non-proxy owner login."""
        return sorted(str(rec.account.login) for rec in self._load().values()
                      if rec.account.role == "owner" and not rec.account.disabled
                      and not rec.account.proxy_for)

    def account(self, login: str) -> Account | None:
        rec = self._load().get(str(login or "").strip().lower())
        return rec.account if rec is not None else None

    def signing_identity(self, login: str) -> str:
        """The owner a login represents: a proxy signs for its target."""
        account = self.account(login)
        if account is None:
            return ""
        return str(account.proxy_for or account.login)

    def required_owner_signers(self) -> list[str]:
        """Non-proxy owners, plus a disabled owner covered by an active proxy."""
        records = self._load()
        active = [str(rec.account.login) for rec in records.values()
                  if rec.account.role == "owner" and not rec.account.disabled
                  and not rec.account.proxy_for]
        covered = {r.account.proxy_for.lower() for r in records.values()
                   if r.account.role == "owner" and not r.account.disabled and r.account.proxy_for}
        disabled = [str(rec.account.login) for key, rec in records.items()
                    if rec.account.role == "owner" and rec.account.disabled
                    and not rec.account.proxy_for and key in covered]
        return sorted(set(active) | set(disabled))

    def set_disabled(self, login: str, disabled: bool) -> Account:
        records = self._load()
        rec = records.get(str(login or "").strip().lower())
        if rec is None:
            raise KeyError("account not found")
        if rec.account.role == "owner" and disabled:
            target = str(login or "").strip().lower()
            others = [r for key, r in records.items()
                      if key != target and r.account.role == "owner"
                      and not r.account.disabled and not r.account.proxy_for]
            proxies = [r for r in records.values()
                       if r.account.role == "owner" and not r.account.disabled
                       and r.account.proxy_for.lower() == target]
            if not others and not proxies:
                raise ValueError("the last active owner cannot be disabled")
        rec.account.disabled = bool(disabled)
        self._save(records)
        if disabled:
            AccountStore._forget_login_tokens(self, login)
        return rec.account

    # ── sessions ──
    def _issue_session(self, rec: _Record) -> tuple[Account, str] | None:
        """Commit the digest before returning a token; never persist token bytes."""
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        if type(self) is not AccountStore or type(self.business) is not BusinessStore or type(rec) is not _Record:
            raise _CurrentError("invalid_source")
        token = secrets.token_urlsafe(32)
        if type(token) is not str or not token or len(token) > 512:
            raise _CurrentError("invalid_source")
        now = AccountStore._current_principal_time()
        expires = now + _TOKEN_TTL_SECONDS
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with _SESSION_LOCK:
            tokens = self._tokens
            if type(tokens) is not dict or token in tokens:
                raise _CurrentError("invalid_source")
            issued = BusinessStore.issue_console_session(
                self.business, digest, rec.account.login.lower(), now, expires,
                expected_salt=rec.salt, expected_password_hash=rec.password_hash,
            )
            if issued is False:
                return None
            if issued is not True or self._tokens is not tokens:
                raise _CurrentError("unavailable")
            tokens[token] = (rec.account.login.lower(), expires)
        return rec.account, token

    def login(self, login: str, password: str) -> tuple[Account, str] | None:
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        if BusinessStore.console_session_readiness(self.business, _account_store=self) != "ready":
            raise _CurrentError("unavailable") from None
        records = self._load()
        rec = records.get(str(login or "").strip().lower())
        if rec is None or rec.account.disabled:
            return None
        if not secrets.compare_digest(_hash(str(password or ""), rec.salt), rec.password_hash):
            return None
        rec.account.last_login_at = datetime.now().isoformat()
        self._save(records)
        return AccountStore._issue_session(self, rec)

    async def login_async(self, login: str, password: str) -> tuple[Account, str] | None:
        """Offload only password work; all account/session I/O stays on the loop.

        The credential snapshot is not authority after the await: password changes,
        disablement and current role/scope are re-read before minting a token.
        The fresh read/save/token section has no await and uses the existing store.
        """
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        if BusinessStore.console_session_readiness(self.business, _account_store=self) != "ready":
            raise _CurrentError("unavailable") from None
        key = str(login or "").strip().lower()
        rec = self._load().get(key)
        if rec is None or rec.account.disabled:
            return None
        salt, password_hash = rec.salt, rec.password_hash
        clean_password = str(password or "")

        def verify() -> bool:
            return secrets.compare_digest(_hash(clean_password, salt), password_hash)

        if not await asyncio.to_thread(verify):
            return None
        records = self._load()
        current = records.get(key)
        if (current is None or current.account.disabled
                or current.salt != salt or current.password_hash != password_hash):
            return None
        if BusinessStore.console_session_readiness(self.business, _account_store=self) != "ready":
            raise _CurrentError("unavailable") from None
        current.account.last_login_at = datetime.now().isoformat()
        self._save(records)
        return AccountStore._issue_session(self, current)

    @staticmethod
    def _current_principal_time():
        import math
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        now = time.time()
        if (type(now) not in (int, float) or now < 0
                or type(now) is int and now > 2**63 - 1
                or type(now) is float and not math.isfinite(now)):
            raise _CurrentError("unavailable")
        return now

    def _capture_token_current(self, token: str):
        """Capture exact cache identity after a bounded durable-session read.

        A missing cache entry may be restored only from the current workspace's
        unexpired, non-revoked digest row. Never manufacture a row from a client
        token or from a pre-upgrade in-memory session.
        """
        import math
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        if type(self) is not AccountStore:
            raise _CurrentError("invalid_source")
        if (type(token) is not str or not token or len(token) > 512
                or token.strip() != token
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in token)):
            raise _CurrentError("invalid_source")
        try:
            token_bytes = token.encode("utf-8")
        except UnicodeError:
            raise _CurrentError("invalid_source") from None
        if len(token_bytes) > 512:
            raise _CurrentError("invalid_source")
        business = getattr(self, "business", None)
        if type(business) is not BusinessStore:
            raise _CurrentError("invalid_source")
        now = AccountStore._current_principal_time()
        digest = hashlib.sha256(token_bytes).hexdigest()
        with _SESSION_LOCK:
            tokens = self._tokens
            if type(tokens) is not dict:
                raise _CurrentError("invalid_source")
            row = BusinessStore.read_console_session_current(
                business, digest, now=now, _account_store=self,
            )
            if self._tokens is not tokens:
                raise _CurrentError("unavailable")
            if row is None:
                tokens.pop(token, None)
                return None
            if type(row) is not dict:
                raise _CurrentError("invalid_source")
            login_key, expires = row.get("login_key"), row.get("expires_at")
            if (type(login_key) is not str or _LOGIN_RE.fullmatch(login_key) is None
                    or login_key.lower() != login_key
                    or type(expires) not in (int, float) or not math.isfinite(expires)
                    or expires <= 0 or now > expires):
                raise _CurrentError("invalid_source")
            missing = object()
            entry = tokens.get(token, missing)
            if entry is missing:
                entry = (login_key, expires)
                tokens[token] = entry
            elif (type(entry) is not tuple or len(entry) != 2
                    or type(entry[0]) is not str or type(entry[1]) not in (int, float)
                    or entry[0] != login_key or entry[1] != expires):
                raise _CurrentError("invalid_source")
            return business, tokens, entry, login_key, expires, threading.get_ident()

    def _finish_token_current(self, token: str, captured, account) -> Account | None:
        """Recheck durable revocation after a possibly asynchronous account read."""
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        business, tokens, entry, login_key, expires, owner_thread = captured
        with _SESSION_LOCK:
            if (threading.get_ident() != owner_thread or type(self) is not AccountStore
                    or self.business is not business or self._tokens is not tokens
                    or tokens.get(token) is not entry):
                raise _CurrentError("unavailable")
            now = AccountStore._current_principal_time()
            row = BusinessStore.read_console_session_current(
                business, hashlib.sha256(token.encode("utf-8")).hexdigest(),
                now=now, _account_store=self, _with_principal=True,
            )
            if self._tokens is not tokens or tokens.get(token) is not entry:
                raise _CurrentError("unavailable")
            if row is None or now > expires:
                tokens.pop(token, None)
                return None
            if (type(row) is not dict or row.get("login_key") != login_key
                    or row.get("expires_at") != expires):
                raise _CurrentError("invalid_source")
            # The worker result is an observation taken before the await.
            # Use the principal from the final session read's own transaction
            # so a scope change during that await cannot reuse the old grant.
            account = row.get("principal")
            if account is None or type(account) is Account and account.disabled is True:
                tokens.pop(token, None)
                return None
            if (type(account) is not Account or account.login.lower() != login_key
                    or type(account.disabled) is not bool or account.disabled is not False):
                raise _CurrentError("invalid_source")
            return account

    def _resolve_token_current_internal(self, token: str) -> Account | None:
        dependency_failed = False
        try:
            from .business_store import BusinessStore
            from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        except Exception:
            dependency_failed = True
        if dependency_failed:
            raise RuntimeError("当前账号记录暂时无法读取。") from None
        failure = None
        result = None
        try:
            captured = AccountStore._capture_token_current(self, token)
            if captured is None:
                return None
            account = BusinessStore._read_account_principal_current(
                captured[0], login=captured[3], _account_store=self,
            )
            result = AccountStore._finish_token_current(self, token, captured, account)
        except Exception as exc:
            failure = "unavailable"
            if (type(exc) is _CurrentError and type(exc.code) is str
                    and exc.code in ("invalid_source", "unavailable")):
                failure = exc.code
        if failure is not None:
            raise _CurrentError(failure) from None
        return result

    def resolve_token_current(self, token: str) -> Account | None:
        return AccountStore._resolve_token_current_internal(self, token)

    async def resolve_token_current_async(self, token: str, *, reader) -> Account | None:
        """Same capture/completion checks; only the independent RO query waits."""
        from .bounded_principal_reader import BoundedPrincipalReader
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        failure = None
        result = None
        try:
            if type(reader) is not BoundedPrincipalReader:
                raise _CurrentError("invalid_source")
            captured = AccountStore._capture_token_current(self, token)
            if captured is None:
                return None
            account = await BoundedPrincipalReader.read(
                reader, captured[0], login=captured[3], account_store=self,
            )
            result = AccountStore._finish_token_current(self, token, captured, account)
        except Exception as exc:
            failure = "unavailable"
            if (type(exc) is _CurrentError and type(exc.code) is str
                    and exc.code in ("invalid_source", "unavailable")):
                failure = exc.code
        if failure is not None:
            raise _CurrentError(failure) from None
        return result

    def resolve_token(self, token: str) -> Account | None:
        """HTTP and WS use the same durable session and current principal checks."""
        return AccountStore._resolve_token_current_internal(self, token)

    def logout(self, token: str) -> None:
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        if type(token) is not str or not token or len(token) > 512 or token.strip() != token:
            return
        if type(self) is not AccountStore or type(self.business) is not BusinessStore:
            raise _CurrentError("invalid_source")
        with _SESSION_LOCK:
            if type(self._tokens) is not dict:
                raise _CurrentError("invalid_source")
            BusinessStore.revoke_console_session(
                self.business, hashlib.sha256(token.encode("utf-8")).hexdigest(),
                now=AccountStore._current_principal_time(),
            )
            self._tokens.pop(token, None)

    def _forget_login_tokens(self, login: str) -> None:
        """Cache invalidation after an already committed credential mutation."""
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        key = str(login or "").strip().lower()
        with _SESSION_LOCK:
            tokens = self._tokens
            if type(tokens) is not dict:
                raise _CurrentError("invalid_source")
            for token, entry in list(tokens.items()):
                if (type(token) is not str or type(entry) is not tuple or len(entry) != 2
                        or type(entry[0]) is not str):
                    raise _CurrentError("invalid_source")
                if entry[0] == key:
                    tokens.pop(token, None)

    def revoke_login(self, login: str) -> None:
        from .business_store import BusinessStore
        from .current_account_principal import CurrentAccountPrincipalError as _CurrentError
        key = str(login or "").strip().lower()
        if _LOGIN_RE.fullmatch(key) is None:
            return
        if type(self) is not AccountStore or type(self.business) is not BusinessStore:
            raise _CurrentError("invalid_source")
        with _SESSION_LOCK:
            BusinessStore.revoke_console_login(
                self.business, key, now=AccountStore._current_principal_time(),
            )
            AccountStore._forget_login_tokens(self, key)


_SHARED_STORES: dict[str, AccountStore] = {}
_SHARED_STORES_LOCK = threading.Lock()


def shared_account_store(ampuly_home: Path | str) -> AccountStore:
    """The one account store this process keeps for one home.

    HTTP and WS still share one store and process-local binding identities.
    Active token digests also live in this workspace's business store, so a
    normal restart can recover a session within its original fixed expiry.
    Recovery never recreates revoked rows or restores an execution grant. A home whose database has gone (a drill's folder removed and
    made again) gets a fresh store rather than one bound to the old file.
    """
    home = Path(ampuly_home)
    key = str(home.resolve())
    with _SHARED_STORES_LOCK:
        store = _SHARED_STORES.get(key)
        if store is not None and not Path(getattr(store.business, "path", home / "business.db")).is_file():
            store = None
        if store is None:
            store = _SHARED_STORES[key] = AccountStore(home)
        return store


# Which console actions an employee account may call. Everything else is the
# owner's: the org, the workflows, other people's sessions, settings.
EMPLOYEE_ALLOWED_ACTIONS = frozenset({
    "ping",
    "auth_login", "auth_token", "auth_logout",
    "account_set_password",
    "my_tasks_list",
    "interaction_reply",
})
# Actions any connection may call before logging in.
ANONYMOUS_ALLOWED_ACTIONS = frozenset({"ping", "auth_login", "auth_token"})


def action_allowed(account: Account | None, action: str, *, auth_required: bool) -> bool:
    """The authorization rule for one console action (declared per action in api.ACTION_ROLES; a customer
    account's whole list is api.CUSTOMER_ALLOWED_ACTIONS)."""
    from ampuly.plugins.office_ui.api import role_allows

    return role_allows(getattr(account, "role", None) if account is not None else None, action, auth_required=auth_required)
