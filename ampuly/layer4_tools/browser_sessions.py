"""One browser per binding, instead of one browser.

`browser.py` holds a module-level `BrowserRuntime()`. Every task in the
process reaches the same Playwright browser, the same context and the same
page -- so a customer's logged-in session is available to the next task that
calls `browser_navigate`, whoever that task belongs to, and a screenshot taken
for one order can show another order's page. The plan's A07 is exactly this
case: switch customer, and the previous customer's data and login state must
not be obtainable.

This is the binding layer for that. It does not change how a browser is
launched or driven; it changes how many there are and who gets which.

**A binding is the scope, not the task alone.** Project, customer,
environment and task together. Two tasks never share, and a task cannot
reach a browser opened under a different customer even by accident, because
it is not looked up by anything they have in common.

**An unbound call gets an unbound browser.** A call with no task -- a CLI
probe, a test -- lands on its own key and shares only with other unbound
calls. That is the behaviour the singleton already had, kept for exactly the
callers that had it, and it can never be handed a customer's profile.

**A bound session never uses the operator's own Chrome profile.** The config
may name a `user_data_dir`; a bound session ignores it and uses a profile
directory of its own under the AMPULY home. Reusing a person's real browser
profile for an agent's customer session is what the plan forbids in as many
words, and it is also how one customer's cookies end up in another's jar.

**The cap refuses rather than evicts.** Browsers are expensive and a limit is
required, but an eviction that reclaims a slot would silently discard a
login a person performed by hand. So a new binding past the cap is refused --
and the refusal does not say which bindings hold the browsers, because it is
raised into the asking agent's tool result and that agent belongs to one
customer. Idle sessions do expire -- that is the plan's `expired` state, and
an abandoned logged-in browser is the thing expiry is for -- but expiry is by
time, not by convenience, and it is also what releases a reservation nobody
used.

The human-handoff lifecycle and domain allow-listing now sit on this
isolation layer.  The registry still exposes them only as operator methods;
the product surface decides who may invoke them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ampuly.core.config import get_ampuly_home
from ampuly.core.config import replace_with_retry


UNBOUND_KEY = "unbound"

# How many browsers may be live at once. Each is a real Chromium process with
# its own memory; the number is small on purpose.
DEFAULT_MAX_LIVE_SESSIONS = 3

# A browser nobody has touched for this long is closed. The plan lists
# `expired` as a lifecycle state for exactly this: an abandoned session that
# is still logged in is a standing credential, not a convenience.
DEFAULT_IDLE_EXPIRY_SEC = 30 * 60
# X09: one customer may not take every slot, and a host short of memory
# refuses a new browser rather than starving the ones that are open. Both are
# refusals, never evictions: nothing that is open is closed to make room.
DEFAULT_MAX_PER_CUSTOMER = 2
DEFAULT_MIN_FREE_MEMORY_MB = 512


def host_id() -> str:
    """This machine, as a session record names it: a digest of the host name and platform, never the
    name itself (the state file must not carry it). A record made elsewhere is not this host's to restore."""
    import hashlib
    import platform as _platform

    return hashlib.sha256(f"{_platform.node()}|{_platform.system()}|{_platform.machine()}".encode("utf-8")).hexdigest()[:16]


def available_memory_mb() -> int | None:
    """Free physical memory in MB, or None when this host cannot say (then no refusal is based on it)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullAvailPhys // (1024 * 1024))
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size // (1024 * 1024))
    except Exception:  # noqa: BLE001 - an unmeasurable host is reported as unknown, not as empty
        return None


class BrowserSessionRefused(RuntimeError):
    """A browser could not be opened, with the reason a caller can act on."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = str(reason or "").strip()


@dataclass(frozen=True)
class ProfileScope:
    """Which stored login a browser session may use -- the shape only; nothing stores one yet (Q05 B7).

    The owner's rules, written here because the shape is what enforces most of them:

    * **Per customer, per site, per account.** ``(project, customer, environment, site_host, account_label)``.
      The environment is in the key so a login made in a test environment is never reused in the real one.
    * **Never across customers.** With no ``customer_id`` there is no profile at all: the order handles its own
      login, and nothing is kept. That is why :meth:`storable` refuses an empty customer rather than inventing
      a shared one.
    * **Off by default.** ``keep`` starts false. The person who helps with the first login is the one who
      decides, at that moment, whether this customer's login is kept -- not a setting decided beforehand.
    * **Never for money.** A host that serves banking or payment is refused outright, whatever anyone chose.
    * **One order at a time.** Enforced where a profile is lent out, in B8; the shape carries the identity that
      lending has to be exclusive on.
    """

    project_id: str = ""
    customer_id: str = ""
    environment_id: str = ""
    site_host: str = ""
    account_label: str = ""
    keep: bool = False

    def key(self) -> str:
        """The profile's own identity. Not a session key, and never used as one."""
        raw = "\x1f".join((self.project_id, self.customer_id, self.environment_id,
                            self.site_host.casefold(), self.account_label))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def storable(self) -> str:
        """``""`` when this login may be kept, else why not. Nothing in this phase acts on a 「yes」."""
        if not self.keep:
            return "not_kept"
        if not str(self.customer_id or "").strip():
            # No customer means no owner for the login: the order handles it and nothing is kept.
            return "no_customer"
        if not str(self.site_host or "").strip():
            return "no_site"
        if is_money_host(self.site_host):
            return "money_host"
        return ""


# Hosts whose logins are never kept, whatever anyone chose: a stored session here is a stored ability to move
# the owner's money. Matched on the registrable part, so a subdomain cannot slip past.
MONEY_HOST_MARKS = (
    "bank", "icbc", "ccb", "abchina", "boc", "bankcomm", "cmbchina", "spdb", "cib", "citic", "pingan",
    "alipay", "unionpay", "chinapay", "95516", "paypal", "stripe", "tenpay", "wepay", "yeepay",
    # 「pay」 on its own catches pay.*, *-pay.* and mypay.*; it also catches a few names that are not payment at
    # all. That trade is deliberate: a false 「yes」 costs one handoff, a false 「no」 stores a login that can
    # move the owner's money.
    "pay", "网银", "支付", "银行", "wallet", "钱包",
)


def is_money_host(host: str) -> bool:
    """Whether this host serves banking or payment. Errs towards yes: a false 「no」 stores a bank login."""
    name = str(host or "").strip().casefold()
    if not name:
        return False
    return any(mark in name for mark in MONEY_HOST_MARKS)

@dataclass(frozen=True)
class BrowserBinding:
    """Who a browser session belongs to.

    All four parts, not the task alone: the task is what makes two runs
    separate, and the other three are what a refusal has to be able to name
    when a session is reached from the wrong place.
    """

    project_id: str = ""
    customer_id: str = ""
    environment_id: str = ""
    task_id: str = ""
    # Q05 B7: which stored login this session is allowed to use, once there are any. Reserved, and deliberately
    # outside `key()` and `__eq__`: the key IS the gateway's session_id, and changing it would leave every
    # undecided latch recovered from the log after a restart unable to find its session. Nothing writes a
    # profile in this phase; B8 is where they are stored.
    profile_scope: "ProfileScope | None" = field(default=None, compare=False)

    @classmethod
    def for_task(cls, task: Any) -> "BrowserBinding":
        """Read the binding off a task, or return the unbound one.

        Nothing is invented. A task that carries no customer is bound to its
        task id alone, which still separates it from every other task -- the
        thing that must not happen is inheriting somebody else's session,
        and an empty field cannot cause that here because the key is the
        whole tuple.
        """
        if task is None:
            return cls()
        metadata = dict(getattr(task, "metadata", {}) or {})
        nested = metadata.get("harness_scope", {})
        if not isinstance(nested, dict):
            raise BrowserSessionRefused("Invalid browser scope", reason="invalid_scope")
        project_id = str(getattr(task, "project_id", "") or "").strip()
        if "project_id" in nested and str(nested["project_id"] or "").strip() != project_id:
            raise BrowserSessionRefused("Conflicting browser scope", reason="scope_conflict")
        resolved: dict[str, str] = {}
        for field in ("customer_id", "environment_id"):
            top_value = str(metadata.get(field, "") or "").strip()
            nested_value = str(nested.get(field, "") or "").strip()
            if field in metadata and field in nested and top_value != nested_value:
                raise BrowserSessionRefused("Conflicting browser scope", reason="scope_conflict")
            resolved[field] = nested_value if field in nested else top_value
        return cls(
            project_id=project_id,
            customer_id=resolved["customer_id"],
            environment_id=resolved["environment_id"],
            task_id=str(getattr(task, "id", "") or "").strip(),
        )

    @property
    def is_bound(self) -> bool:
        return any(
            (self.project_id, self.customer_id, self.environment_id, self.task_id)
        )

    def key(self) -> str:
        """The gateway's session id. Four parts, and deliberately not the profile scope.

        A key is what a recovered latch looks itself up by after a restart. Adding anything to it would make
        yesterday's undecided browser action unfindable, so a new field is a new field -- never a new ingredient
        here. A test fixes these values literally, so changing them has to be a decision.
        """
        if not self.is_bound:
            return UNBOUND_KEY
        raw = "\x1f".join(
            (self.project_id, self.customer_id, self.environment_id, self.task_id)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        if not self.is_bound:
            return "unbound"
        parts = [
            f"{label}={value}"
            for label, value in (
                ("project", self.project_id),
                ("customer", self.customer_id),
                ("environment", self.environment_id),
                ("task", self.task_id),
            )
            if value
        ]
        return " ".join(parts)


# The lifecycle from plan §9. Which states an agent tool may drive the
# page in, and which a person holds it in, is the whole point of naming them.
STATE_CREATED = "created"
STATE_READY = "ready"
STATE_AGENT_OPERATING = "agent_operating"
STATE_AWAITING_HUMAN = "awaiting_human"
STATE_HUMAN_RETURNED = "human_returned"
STATE_IDENTITY_VERIFIED = "identity_verified"
STATE_REVOKED = "revoked"
STATE_CLOSED = "closed"
# X01 / §5.5: the agent's control of a session is a lease with a term. When
# it runs out the browser stays as it is -- a person may be mid-flow -- but
# no agent tool reaches the page until the owner renews or revokes.
STATE_LEASE_EXPIRED = "lease_expired"
DEFAULT_LEASE_TERM_SEC = 60 * 60.0

# States in which an agent tool may reach the page.
AGENT_MAY_OPERATE = frozenset({STATE_CREATED, STATE_READY, STATE_AGENT_OPERATING, STATE_IDENTITY_VERIFIED})

# What a recorded state becomes after the process that held the browser is gone.
RESTORED_STATE = {
    STATE_CREATED: STATE_CREATED,
    STATE_READY: STATE_CREATED,
    STATE_AGENT_OPERATING: STATE_CREATED,
    STATE_AWAITING_HUMAN: STATE_AWAITING_HUMAN,
    STATE_HUMAN_RETURNED: STATE_HUMAN_RETURNED,
    STATE_IDENTITY_VERIFIED: STATE_HUMAN_RETURNED,
    STATE_REVOKED: STATE_REVOKED,
    # The page is gone with the old process; the next agent run starts a new lease.
    STATE_LEASE_EXPIRED: STATE_CREATED,
}


# C24 audit: how many of the owner's most recent control actions a session keeps for the audit
# view. A live convenience, not the durable record (that is the effect/checkpoint journal);
# bounded so a long-lived session cannot grow it without limit.
_CONTROL_EVENT_CAP = 200


def _utc_now_iso() -> str:
    """Wall-clock ISO stamp for a control event, so it orders against the gateway/effect times in
    the audit timeline; the registry's own clock is monotonic and cannot be sorted against them."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Entry:
    binding: BrowserBinding
    runtime: Any
    last_used: float
    state: str = STATE_CREATED
    # Why the agent may not drive this page right now, in the plan's words.
    state_note: str = ""
    # Hosts this session may navigate to. Empty means nothing is reachable:
    # delegation is the authorization, so no list means no delegated host.
    allowed_domains: tuple[str, ...] = field(default_factory=tuple)
    # Why this runtime may not be handed to an agent: "" (it may), "closing"
    # (a close is in flight), "close_failed" (a close raised or was cancelled
    # -- the browser may still be running and nobody can prove otherwise).
    # A non-empty value keeps the reservation and its slot.
    unavailable: str = ""
    # When the agent's control lease was granted and when it ends (registry
    # clock; 0.0 = no term). Not persisted: a restart starts a new lease.
    lease_granted_at: float = 0.0
    lease_expires_at: float = 0.0
    # C07: the page's own facts (host, path, cookie count) at the hand-off, so the
    # verification after the hand-back can compare instead of only trusting a click.
    handoff_facts: dict[str, Any] = field(default_factory=dict)
    # C24 audit: the owner's control actions on this session (action, wall-clock time, note), for the
    # audit timeline. Persisted with the session in the state file, so the owner's control history survives
    # a service restart like the Gateway journal does (C14); a closed session's is gone with it. Bounded to
    # the most recent _CONTROL_EVENT_CAP.
    control_events: list[dict[str, Any]] = field(default_factory=list)
    # X09: the host that opened this session. Kept with the record, so a record from another machine stays that
    # machine's through every save on this one.
    host: str = ""
    # This process proved the browser closed (not persisted): only then may a revoke purge the profile, and retry it.
    closed_here: bool = False
    # Account occupancy is separate from the task's control lease and key.
    account_handle: Any = None
    account_evidence_failed: bool = False
    account_intents: set[str] = field(default_factory=set)
    control_fence: str = field(default_factory=lambda: uuid.uuid4().hex)
    resource_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    login_policy: Any = None
    login_evidence: Any = None

    def has_reservation(self) -> bool:
        # A factory can start a process and then fail before returning its runtime.
        # Its locally held account remains reserved until closure is established.
        return self.runtime is not None or self.account_handle is not None

    def is_live(self) -> bool:
        """Whether this entry is actually holding a browser open.

        A runtime that has been created but never navigated, or one that has
        been closed, owns no process and must not consume a slot -- otherwise
        the cap counts objects instead of browsers.

        Asked of the runtime rather than read off its internals: a cap that
        reads a private attribute becomes no cap at all the day that
        attribute is renamed, and nothing would fail.
        """
        return bool(getattr(self.runtime, "is_open", False))


class BrowserSessionRegistry:
    """The browsers this process has open, one per binding."""

    def __init__(
        self,
        *,
        runtime_factory: Callable[[BrowserBinding], Any] | None = None,
        max_live: int = DEFAULT_MAX_LIVE_SESSIONS,
        idle_expiry_sec: float = DEFAULT_IDLE_EXPIRY_SEC,
        clock: Callable[[], float] = time.monotonic,
        event_clock: Callable[[], str] = _utc_now_iso,
        lease_term_sec: float = DEFAULT_LEASE_TERM_SEC,
        max_per_customer: int = DEFAULT_MAX_PER_CUSTOMER,
        min_free_memory_mb: int = DEFAULT_MIN_FREE_MEMORY_MB,
        memory_probe: Callable[[], int | None] = available_memory_mb,
        profile_purger: Callable[[BrowserBinding], dict[str, Any]] | None = None,
        state_path: Path | str | Callable[[], Path] | None = None,
        account_store: Any = None,
        account_runtime_factory: Callable[[BrowserBinding, str], Any] | None = None,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        # X05: consoles that want to hear when a session's state moves (a hand-off
        # to a person, a hand-back, a verification, a revocation). Called inline,
        # never awaited; a listener that raises is dropped from that call only.
        self._observers: list[Any] = []
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._account_runtime_factory = account_runtime_factory
        self._custom_runtime_factory = runtime_factory is not None
        self._account_store = account_store
        self._account_bindings: dict[str, ProfileScope] = {}
        self._account_policies: dict[str, Any] = {}
        self._account_verification: set[str] = set()
        self._account_recovery_inflight: set[tuple[str, str]] = set()
        self.max_live = int(max_live)
        self.idle_expiry_sec = float(idle_expiry_sec)
        self._clock = clock
        self._event_clock = event_clock
        self.lease_term_sec = float(lease_term_sec)
        self.max_per_customer = int(max_per_customer)
        self.min_free_memory_mb = int(min_free_memory_mb)
        self._memory_probe = memory_probe
        # A07: what removes a revoked session's profile from disk once its
        # browser has actually closed. Bounded to that one directory.
        self._profile_purger = profile_purger or _default_profile_purger
        # X05 / C14: the lifecycle survives a service restart. The file holds
        # binding ids, state, note and allowed domains -- never page content,
        # cookies or a URL. No path means in-memory only (tests, probes).
        self._state_path = state_path
        self._loaded = state_path is None
        self._host = host_id()

    def inspect_account(self, binding: BrowserBinding) -> dict[str, Any]:
        """Observe the account already selected by an authorized server caller.

        The caller must authorize the exact task before and after any async
        lookup. This method does not authorize a user, restore registry state,
        create a store, select an account, acquire a profile or start a browser.
        A scope supplied on ``binding`` is deliberately not an account source.
        """
        if not isinstance(binding, BrowserBinding) or not all(
            isinstance(value, str) and 0 < len(value) <= 256 and value.strip() == value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in (binding.project_id, binding.customer_id, binding.environment_id, binding.task_id)
        ):
            raise BrowserSessionRefused("A complete task scope is required", reason="account_scope_mismatch")
        if not self._loaded:
            raise BrowserSessionRefused("Account source is not ready", reason="account_source_not_ready")

        key = binding.key()
        entry = self._entries.get(key)
        selected = self._account_bindings.get(key)
        stored_binding = entry.binding if entry is not None else None
        if entry is not None and (not isinstance(stored_binding, BrowserBinding) or stored_binding != binding):
            raise BrowserSessionRefused("Account and task ownership must match", reason="account_scope_mismatch")
        scope = stored_binding.profile_scope if stored_binding is not None else selected
        unavailable = entry.unavailable if entry is not None else ""
        if entry is not None and selected is not None and selected != scope:
            raise BrowserSessionRefused("Account sources disagree", reason="account_scope_mismatch")
        if scope is None:
            if unavailable:
                reason = "account_recovery_required" if unavailable == "account_recovery_required" else "account_source_unavailable"
                raise BrowserSessionRefused("Account source is unavailable", reason=reason)
            return {"state": "unbound", "account": None}
        self._validate_account_scope(binding, scope)
        store = self._account_store
        inspect = getattr(store, "inspect", None)
        if not callable(inspect):
            raise BrowserSessionRefused("Account store is not ready", reason="account_store_not_ready")
        generation = (getattr(entry, "resource_id", None), getattr(entry, "control_fence", None))
        view = inspect(scope)
        # Store inspection can observe external changes. A result is useful
        # only for the same in-memory source and generation we started with.
        if (not self._loaded or self._account_store is not store
                or self._entries.get(key) is not entry
                or self._account_bindings.get(key) != selected
                or (entry is not None and (
                    entry.binding is not stored_binding or entry.binding.profile_scope != scope
                    or entry.unavailable != unavailable
                    or (getattr(entry, "resource_id", None), getattr(entry, "control_fence", None)) != generation))):
            raise BrowserSessionRefused("Account observation changed", reason="account_observation_changed")
        fields = ("present", "profile_present", "owner_present", "pending_present", "revoked")
        if (type(view) is not dict or set(view) != {"account_id", *fields}
                or type(view["account_id"]) is not str or len(view["account_id"]) != 64
                or any(char not in "0123456789abcdef" for char in view["account_id"])
                or any(type(view[name]) is not bool for name in fields)):
            raise BrowserSessionRefused("Invalid account observation", reason="account_inspection_invalid")
        return {"state": "bound", "account": dict(view)}

    def bind_account_profile(self, binding: BrowserBinding, scope: ProfileScope, *,
                             login_policy: Any = None, verification_only: bool = False) -> None:
        """Owner/service-only attachment after authorization; never a model argument.

        A caller must authorize the selected account before invoking this method.
        It records an explicit choice, not proof that the website login is valid.
        Existing browser resources cannot be silently switched to another account.
        """
        self._validate_account_scope(binding, scope)
        from ampuly.layer4_tools.browser_account_status import LoginProbePolicy
        if (type(verification_only) is not bool or (login_policy is not None and
                (not isinstance(login_policy, LoginProbePolicy) or login_policy.site_host != scope.site_host.casefold()))):
            raise BrowserSessionRefused("A matching server-owned login policy is required", reason="account_policy_mismatch")
        self._ensure_loaded()
        if binding.key() in self._entries:
            raise BrowserSessionRefused("Close the current task browser before choosing an account",
                                        reason="account_binding_in_use")
        self._account_bindings[binding.key()] = scope
        self._account_policies[binding.key()] = login_policy
        if verification_only:
            self._account_verification.add(binding.key())
        else:
            self._account_verification.discard(binding.key())

    @staticmethod
    def _validate_account_scope(binding: BrowserBinding, scope: ProfileScope) -> None:
        if (not isinstance(scope, ProfileScope) or type(scope.keep) is not bool
                or not all((binding.project_id, binding.customer_id, binding.environment_id, binding.task_id))
                or (scope.project_id, scope.customer_id, scope.environment_id)
                != (binding.project_id, binding.customer_id, binding.environment_id)
                or not str(scope.account_label or "").strip()):
            raise BrowserSessionRefused("Account and task ownership must match", reason="account_scope_mismatch")
        reason = scope.storable()
        if reason:
            raise BrowserSessionRefused("This profile may not be retained", reason=f"account_{reason}")

    def _account_store_for_use(self) -> Any:
        if self._account_store is None:
            from ampuly.layer4_tools.browser_accounts import BrowserAccountStore
            self._account_store = BrowserAccountStore(get_ampuly_home() / "browser" / "accounts")
        return self._account_store

    def _create_runtime(self, entry: _Entry) -> None:
        entry.resource_id = uuid.uuid4().hex
        entry.login_evidence = None
        scope = entry.binding.profile_scope
        if scope is None:
            entry.runtime = self._runtime_factory(entry.binding)
            return
        self._validate_account_scope(entry.binding, scope)
        if self._custom_runtime_factory and self._account_runtime_factory is None:
            raise BrowserSessionRefused("An account-aware runtime factory is required",
                                        reason="account_runtime_factory_required")
        store = self._account_store_for_use()
        try:
            acquire = store.acquire_for_verification if entry.binding.key() in self._account_verification else store.acquire
            entry.account_handle = acquire(scope, entry.binding.key())
            entry.login_policy = self._account_policies.get(entry.binding.key())
        except Exception as exc:
            raise BrowserSessionRefused("Account resource is unavailable",
                                        reason=getattr(exc, "reason", "account_unavailable")) from None
        try:
            path = str(entry.account_handle.profile_path)
            factory = self._account_runtime_factory
            entry.runtime = (factory(entry.binding, path) if factory is not None
                             else _default_runtime_factory(entry.binding, account_profile_dir=path))
            if entry.account_handle.verification_only:
                restrict = getattr(entry.runtime, "set_verification_only", None)
                if not callable(restrict):
                    raise BrowserSessionRefused("This runtime cannot fence background verification traffic",
                                                reason="account_verification_fence_required")
                restrict(True)
        except BaseException:
            # A factory failure proves neither that no process started nor that it stopped.
            entry.unavailable = "close_failed"
            self._entries[entry.binding.key()] = entry
            self._save()
            raise

    def _check_account(self, entry: _Entry) -> None:
        if entry.account_handle is not None:
            try:
                self._account_store_for_use().assert_current(entry.account_handle)
            except Exception as exc:
                raise BrowserSessionRefused("Account control is unavailable",
                                            reason=getattr(exc, "reason", "account_unavailable")) from None

    def mark_account_unknown(self, binding: BrowserBinding, action_id: str) -> None:
        """Project an unresolved action into the account resource before it is lent again."""
        entry = self._entries.get(binding.key())
        if entry is not None and entry.account_handle is not None:
            try:
                self._account_store_for_use().mark_unknown(entry.account_handle, action_id)
                entry.account_intents.add(action_id)
            except Exception as exc:
                if getattr(exc, "reason", "") not in {"account_write_pending", "account_revoked", "stale_handle"}:
                    entry.account_evidence_failed = True
                raise

    def resolve_account_action(self, binding: BrowserBinding, action_id: str, evidence_ref: str) -> None:
        """Only called after the formal Gateway confirms this exact attempt's outcome."""
        entry = self._entries.get(binding.key())
        if entry is not None and entry.account_handle is not None and action_id in entry.account_intents:
            try:
                self._account_store_for_use().resolve_unknown(entry.account_handle, action_id, evidence_ref)
                entry.account_intents.discard(action_id)
            except Exception:
                entry.account_evidence_failed = True
                raise

    def revoke_account(self, binding: BrowserBinding) -> None:
        """Owner/service-only account revocation; does not delete stored login files."""
        entry = self._require(binding)
        if entry.binding.profile_scope is None:
            raise BrowserSessionRefused("Task has no retained account", reason="no_account")
        self._account_store_for_use().revoke(entry.binding.profile_scope)
        self.revoke(binding, reason="account revoked")

    def account_view(self, binding: BrowserBinding) -> dict[str, Any] | None:
        """Owner projection only: retained selection is not proof of website login."""
        entry = self._entries.get(binding.key())
        if entry is None or entry.binding.profile_scope is None:
            return None
        handle = entry.account_handle
        account_id = handle.account_id if handle is not None else ""
        login = {"state": "unverified", "reason": "login_probe_required"}
        if entry.login_policy is not None and entry.login_evidence is not None:
            login = entry.login_evidence.projection(now=self._clock(), policy=entry.login_policy,
                        account_id=account_id, resource_id=entry.resource_id, control_fence=entry.control_fence)
        availability = entry.unavailable or "available"
        pending = None
        pending_known = False
        if handle is None:
            availability = "account_recovery_required"
        else:
            try:
                self._check_account(entry)
                pending = self._account_store_for_use().pending_view(handle)
                pending_known = True
            except Exception as exc:
                reason = str(getattr(exc, "reason", "") or "account_unavailable")
                if reason == "no_pending_verification":
                    pending_known = True
                else:
                    availability = reason
        return {"account_id": account_id, "selection": "retained_account", "availability": availability,
                "resource_id": entry.resource_id, "control_fence": entry.control_fence,
                "access_mode": "verification_only" if handle is not None and handle.verification_only else "normal",
                "login": login, "pending_known": pending_known, "pending": pending}

    async def refresh_account_login(self, binding: BrowserBinding, runtime: Any) -> dict[str, Any] | None:
        """Observe configured site markers; never grant or renew the control lease."""
        entry = self._entries.get(binding.key())
        if entry is None or entry.binding.profile_scope is None:
            return None
        self._check_account(entry)
        if entry.runtime is not runtime or entry.account_handle is None or entry.unavailable:
            raise BrowserSessionRefused("Account runtime is not current", reason="account_runtime_changed")
        if entry.login_policy is None:
            return self.account_view(binding)
        resource_id, fence = entry.resource_id, entry.control_fence
        probe = getattr(runtime, "probe_account_login", None)
        try:
            raw = await probe(entry.login_policy) if callable(probe) else {}
        except Exception:
            raw = {}
        if (self._entries.get(binding.key()) is not entry or entry.runtime is not runtime
                or entry.resource_id != resource_id or entry.control_fence != fence or entry.unavailable):
            raise BrowserSessionRefused("Control changed during login probe", reason="browser_control_changed")
        self._check_account(entry)
        from ampuly.layer4_tools.browser_account_status import LoginProbeEvidence
        entry.login_evidence = LoginProbeEvidence.from_probe(entry.login_policy, raw,
                account_id=entry.account_handle.account_id, resource_id=resource_id, control_fence=fence,
                observation_id=uuid.uuid4().hex, observed_at=self._clock())
        self._changed(entry)
        return self.account_view(binding)

    def check_account_write(self, binding: BrowserBinding) -> None:
        entry = self._entries.get(binding.key())
        if entry is None or entry.binding.profile_scope is None:
            return
        self._check_account(entry)
        if entry.account_handle is None or entry.unavailable:
            raise BrowserSessionRefused("Account is unavailable", reason="account_unavailable")
        if entry.account_handle.verification_only:
            raise BrowserSessionRefused("This account is borrowed only to verify the prior result", reason="account_verification_only")
        view = self.account_view(binding)
        if not view or view["availability"] != "available" or view["login"]["state"] != "authenticated":
            raise BrowserSessionRefused("Website login has not been freshly verified", reason="account_login_required")

    def resolve_account_recovery(self, binding: BrowserBinding, *, resource_id: str, control_fence: str,
                                 pending_ref: str, outcome: str, evidence_ref: str,
                                 record_verified_result: Callable[[dict[str, Any]], bool]) -> None:
        """Trusted owner/Effect consumer only, after independent result verification.

        The callback must durably record that verified result in the existing
        audit/Effect system. It is not a model-facing claim or a new approval.
        This handle stays read-only; a later normal task requires a fresh borrow.
        """
        entry = self._require(binding)
        self._check_account(entry)
        handle = entry.account_handle
        if (handle is None or not handle.verification_only or entry.unavailable
                or resource_id != entry.resource_id or control_fence != entry.control_fence):
            raise BrowserSessionRefused("Recovery control is not current", reason="account_recovery_changed")
        pending = self._account_store_for_use().pending_view(handle)
        if (not pending or pending["pending_ref"] != pending_ref
                or outcome not in {"succeeded", "failed_before_effect"}
                or not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref.encode("utf-8")) > 512):
            raise BrowserSessionRefused("A matching verified result is required", reason="account_recovery_evidence_required")
        recovery_key = (handle.account_id, pending_ref)
        if recovery_key in self._account_recovery_inflight:
            raise BrowserSessionRefused("This result is already being recorded", reason="account_recovery_in_progress")
        self._account_recovery_inflight.add(recovery_key)
        try:
            recorded = record_verified_result({"account_id": handle.account_id, **pending,
                    "outcome": outcome, "evidence_ref": evidence_ref, "verification_binding": binding.key()})
            if recorded is not True:
                raise BrowserSessionRefused("Verified result was not recorded", reason="account_recovery_record_failed")
            if (self._entries.get(binding.key()) is not entry or entry.account_handle is not handle
                    or entry.resource_id != resource_id or entry.control_fence != control_fence or entry.unavailable):
                raise BrowserSessionRefused("Recovery control changed", reason="account_recovery_changed")
            self._check_account(entry)
            self._account_store_for_use().resolve_verified(handle, pending_ref, outcome, evidence_ref)
        finally:
            self._account_recovery_inflight.discard(recovery_key)
        self._changed(entry)

    # ── persistence (X05 / C14) ─────────────────────────────────────────

    def _resolved_state_path(self) -> Path | None:
        if self._state_path is None:
            return None
        return Path(self._state_path() if callable(self._state_path) else self._state_path)

    def _ensure_loaded(self) -> None:
        """Restore what a previous process recorded, once, before the first look."""
        if self._loaded:
            return
        self._loaded = True
        path = self._resolved_state_path()
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # unreadable state is absent state; nothing is invented
        for record in payload.get("sessions", []) if isinstance(payload, dict) else []:
            try:
                binding = BrowserBinding(project_id=str(record["project_id"]), customer_id=str(record["customer_id"]),
                                         environment_id=str(record["environment_id"]), task_id=str(record["task_id"]))
            except (KeyError, TypeError):
                continue
            stored_scope = record.get("account_scope")
            if stored_scope is not None:
                try:
                    scope = ProfileScope(**stored_scope)
                    self._validate_account_scope(binding, scope)
                    binding = replace(binding, profile_scope=scope)
                except (TypeError, ValueError, BrowserSessionRefused):
                    # Preserve a blocked identity rather than silently opening a fresh task profile.
                    stored_scope = {}
            state = str(record.get("state", ""))
            # The page that was being driven is gone with the old process: an
            # agent's page starts over; a person's hand-back has to be verified
            # again (C07) because the verification was of a page, not a person.
            restored = RESTORED_STATE.get(state)
            if restored is None:
                continue
            note = str(record.get("note", "") or "")[:300]
            # X09: a session belongs to the host that opened it. A record written on another
            # machine (a copied state file, a moved home) is kept as a fact, never re-opened here.
            recorded_host = str(record.get("host", "") or "")
            # A file an older save restamped with this host still says other_host where it was one.
            other_host = (bool(recorded_host) and recorded_host != self._host) or record.get("unavailable") == "other_host"
            restored_note = "recorded on another host; not restored here" if other_host else "restored after service restart"
            if restored_note in note:
                restored_note = ""
            entry = _Entry(binding=binding, runtime=None, last_used=self._clock(), state=restored,
                           host=recorded_host or self._host,
                           state_note="; ".join(part for part in (note, restored_note) if part),
                           allowed_domains=tuple(str(d) for d in record.get("allowed_domains", []) if str(d)),
                           unavailable="other_host" if other_host else ("close_failed" if record.get("unavailable") == "close_failed" else ""),
                           control_events=[dict(e) for e in (record.get("control_events") or []) if isinstance(e, dict)][-_CONTROL_EVENT_CAP:])
            if (stored_scope is not None or record.get("unavailable") == "account_recovery_required") and not other_host:
                entry.unavailable = "account_recovery_required"
            self._entries[binding.key()] = entry

    def _save(self) -> None:
        path = self._resolved_state_path()
        if path is None:
            return
        sessions = []
        for entry in self._entries.values():
            if entry.state == STATE_CLOSED:
                continue
            binding = entry.binding
            sessions.append({
                "project_id": binding.project_id, "customer_id": binding.customer_id,
                "environment_id": binding.environment_id, "task_id": binding.task_id,
                "state": entry.state, "note": entry.state_note[:300], "allowed_domains": list(entry.allowed_domains),
                "unavailable": entry.unavailable, "host": entry.host or self._host,
                "control_events": list(entry.control_events),
                **({"account_scope": asdict(binding.profile_scope)} if binding.profile_scope is not None else {}),
            })
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "sessions": sessions}, ensure_ascii=False, indent=2), encoding="utf-8")
            replace_with_retry(tmp, path)
        except OSError:
            pass  # a state file that cannot be written must not take the session down with it

    async def runtime_for(self, binding: BrowserBinding) -> Any:
        """The runtime this binding owns, creating it if the cap allows."""

        async with self._lock:
            self._ensure_loaded()
            # A lease that ran out while nobody called is an ended lease, not an idle agent session to drop.
            self._expire_leases()
            await self._expire_idle()
            self._expire_leases()
            key = binding.key()
            entry = self._entries.get(key)
            scope = binding.profile_scope or self._account_bindings.get(key)
            if entry is not None:
                if scope is not None and scope != entry.binding.profile_scope:
                    raise BrowserSessionRefused("A task cannot switch accounts in place", reason="account_binding_conflict")
                binding = entry.binding
            elif scope is not None:
                self._validate_account_scope(binding, scope)
                binding = replace(binding, profile_scope=scope)
            if entry is not None and entry.runtime is None and entry.state in AGENT_MAY_OPERATE and not entry.unavailable:
                # Restored from a previous process, or its browser closed while the record stayed: the record is real,
                # the browser is not. A new one is held to the same limits as any new session (X09: refuse, never evict).
                self._check_capacity(binding)
                self._create_runtime(entry)
                _revive(entry.runtime)
                self._bind_human_input_guard(entry)
                self._grant_lease(entry)
            if entry is not None:
                # W05: the page belongs to whoever the lifecycle says it does.
                if entry.state == STATE_LEASE_EXPIRED:
                    raise BrowserSessionRefused(
                        f"the agent's control lease for {binding.describe()} ended; "
                        "the owner renews it (renew_lease) or revokes the session",
                        reason="lease_expired",
                    )
                if entry.state == STATE_REVOKED:
                    raise BrowserSessionRefused(
                        f"browser session for {binding.describe()} was revoked"
                        + (f": {entry.state_note}" if entry.state_note else ""),
                        reason="session_revoked",
                    )
                if entry.state == STATE_AWAITING_HUMAN:
                    raise BrowserSessionRefused(
                        f"a person is operating the browser for {binding.describe()}; "
                        "agent input is paused until they hand it back and the session is verified",
                        reason="awaiting_human",
                    )
                if entry.state == STATE_HUMAN_RETURNED:
                    raise BrowserSessionRefused(
                        f"the browser for {binding.describe()} was handed back but not yet verified; "
                        "the system must check page state and account ownership first",
                        reason="verification_required",
                    )
                if entry.unavailable == "other_host":
                    raise BrowserSessionRefused(
                        f"the browser session for {binding.describe()} was recorded on another host and is not "
                        "restored here; the owner revokes it or runs the order on that host",
                        reason="session_bound_to_other_host",
                    )
                if entry.unavailable:
                    # Not the old runtime, and not a new one either: the
                    # browser this binding held may still be running, and
                    # a replacement would be built on top of it. Only an
                    # explicit close resolves this.
                    raise BrowserSessionRefused(
                        "This session's browser did not close cleanly; call "
                        "browser_close to retry before opening it again.",
                        reason="session_close_failed",
                    )
                self._check_account(entry)
                entry.last_used = self._clock()
                return entry.runtime
            # Every entry is a reservation, live or not. Counting only the
            # ones already holding a browser open bounded nothing: a runtime
            # is created idle and opens on its first navigate, so N bindings
            # could each take one while the count still read zero. An
            # unused reservation is released by idle expiry above, which is
            # the right answer to "a failed launch must not block the next
            # caller" -- declining to count it was not.
            # X09: refuse, never evict. A customer at their own cap is told so
            # in terms of their own sessions; a host short of memory is told
            # the numbers. Nothing open is closed to make room either way.
            # X05 real run (2026-09-14): a record restored from a previous process holds no browser and
            # no reservation (its runtime is None until an agent re-attaches), so it takes no slot; two
            # hand-offs interrupted by a restart had filled the cap and refused every new order.
            self._check_capacity(binding)
            entry = _Entry(binding=binding, runtime=None, last_used=self._clock(), host=self._host)
            self._create_runtime(entry)
            _revive(entry.runtime)
            self._bind_human_input_guard(entry)
            self._grant_lease(entry)
            self._entries[key] = entry
            self._save()
            return entry.runtime

    def _bind_human_input_guard(self, entry: _Entry) -> None:
        setter = getattr(entry.runtime, "set_human_input_guard", None)
        if not callable(setter):
            return
        key = entry.binding.key()
        runtime = entry.runtime
        def allowed() -> bool:
            from ampuly.layer4_tools.browser import _AGENT_DISPATCH_GUARD
            current = self._entries.get(key)
            request_guard = _AGENT_DISPATCH_GUARD.get()
            return bool(current is entry and current.runtime is runtime
                        and current.state == STATE_AWAITING_HUMAN and not current.unavailable
                        and not getattr(current.account_handle, "verification_only", False)
                        and (request_guard is None or request_guard()))
        setter(allowed)

    def assert_control(self, binding: BrowserBinding, resource_id: Any, control_fence: Any, *, human: bool = False) -> _Entry:
        """Match a caller's observed incarnation; never fill missing client identity."""
        self._ensure_loaded()
        self._expire_leases()
        entry = self._entries.get(binding.key())
        if (entry is None or type(resource_id) is not str or not resource_id
                or type(control_fence) is not str or not control_fence
                or resource_id != entry.resource_id or control_fence != entry.control_fence):
            raise BrowserSessionRefused("Browser control changed", reason="browser_control_changed")
        if human:
            self._check_account(entry)
            if getattr(entry.account_handle, "verification_only", False):
                raise BrowserSessionRefused("Verification sessions are read-only", reason="account_verification_only")
            if entry.state != STATE_AWAITING_HUMAN:
                raise BrowserSessionRefused("Human does not hold this page", reason="human_not_in_control")
            if entry.runtime is None or entry.unavailable:
                raise BrowserSessionRefused("Browser view unavailable", reason="browser_view_unavailable")
        return entry

    def _check_capacity(self, binding: BrowserBinding) -> None:
        """Whether one more browser may open for this binding: the customer's cap, the host's memory, the live cap."""
        customer = str(getattr(binding, "customer_id", "") or "")
        if customer and self.max_per_customer > 0:
            mine = sum(1 for entry in self._entries.values()
                       if entry.has_reservation() and str(getattr(entry.binding, "customer_id", "") or "") == customer)
            if mine >= self.max_per_customer:
                raise BrowserSessionRefused(
                    f"{binding.describe()} already holds {mine} browser session(s), the most one customer may; "
                    "close one of them before opening another",
                    reason="customer_session_cap_reached",
                )
        free = self._memory_probe() if callable(self._memory_probe) else None
        if free is not None and self.min_free_memory_mb > 0 and free < self.min_free_memory_mb:
            raise BrowserSessionRefused(
                f"this host has {free} MB free, below the {self.min_free_memory_mb} MB a new browser needs; "
                f"no session was opened for {binding.describe()} and none was closed to make room",
                reason="host_resources_exhausted",
            )
        if self._reserved() >= self.max_live:
            # Deliberately does not name who holds them. This message is
            # raised into the tool result of the agent that asked, and
            # that agent belongs to one customer; listing the others
            # would hand it another customer's identity, which is the
            # thing this module exists to prevent. An operator who needs
            # the list has `live_bindings()`, which no agent reaches.
            raise BrowserSessionRefused(
                f"{self.max_live} browser sessions are already open; close one "
                f"before opening a session for {binding.describe()}",
                reason="session_cap_reached",
            )

    def _state_without_browser(self, entry: _Entry) -> str:
        """What a record says once its browser is gone: the restart rule. An agent-side session starts over (created);
        a hand-off, a hand-back, a revocation stay; a verification was of a page that is gone; an ended lease --
        including one that ran out unnoticed -- stays ended until the owner acts."""
        if entry.state in AGENT_MAY_OPERATE and entry.lease_expires_at and self._clock() >= entry.lease_expires_at:
            return STATE_LEASE_EXPIRED
        if entry.state == STATE_LEASE_EXPIRED:
            return STATE_LEASE_EXPIRED
        return RESTORED_STATE.get(entry.state, entry.state)

    def _settle_closed(self, entry: _Entry, *, note: str, event: str, closed: bool) -> bool:
        """The entry's browser has just closed. Drop an agent-side record, keep any other as its state says; True when
        the kept record changed (its observers are told).

        Read from the state the entry has now, not one sampled before the close awaited: an owner who revoked or
        handed the page to a person meanwhile is what the record keeps. A record from another host is never dropped
        (dropping it would let this host open the binding afresh, X09). Closing a record that held no browser adds
        no note and no audit event, so a looping close cannot push the owner's own actions out of the bounded audit.
        """
        key = entry.binding.key()
        if self._entries.get(key) is not entry:
            return False
        if entry.unavailable == "other_host":
            return False
        if entry.unavailable == "account_recovery_required" or entry.account_evidence_failed:
            entry.unavailable = "account_recovery_required"
            return True
        if entry.account_handle is not None:
            if not closed:
                entry.unavailable = "close_failed"
                return True
            try:
                self._account_store_for_use().confirm_closed(entry.account_handle)
            except Exception:
                entry.unavailable = "close_failed"
                return True
            entry.account_handle = None
        after = self._state_without_browser(entry)
        if after == STATE_CREATED:
            del self._entries[key]
            return False
        moved = after != entry.state
        if closed:
            entry.closed_here = True
        if entry.state in AGENT_MAY_OPERATE and after == STATE_LEASE_EXPIRED:
            minutes = int(round((entry.lease_expires_at - entry.lease_granted_at) / 60))
            entry.state_note = f"the agent's control lease ended after {minutes} min; the owner renews or revokes"
        entry.runtime = None
        if entry.unavailable == "closing":
            entry.unavailable = ""
        entry.state = after
        if not (closed or moved):
            return False
        entry.state_note = (f"{entry.state_note}; " if entry.state_note else "") + note
        entry.last_used = self._clock()
        self._record_control_event(entry, event, note=note)
        return True

    # ── the agent's control lease (X01 / §5.5) ──────────────────────────

    def _grant_lease(self, entry: _Entry, term_sec: float | None = None) -> None:
        entry.control_fence = uuid.uuid4().hex
        term = self.lease_term_sec if term_sec is None else float(term_sec)
        entry.lease_granted_at = self._clock()
        entry.lease_expires_at = entry.lease_granted_at + term if term > 0 else 0.0

    def _expire_leases(self) -> None:
        """A lease past its term ends the agent's control; the browser is left as it is."""
        now = self._clock()
        changed = False
        for entry in self._entries.values():
            if entry.state in AGENT_MAY_OPERATE and entry.lease_expires_at and now >= entry.lease_expires_at:
                entry.control_fence = uuid.uuid4().hex
                entry.state = STATE_LEASE_EXPIRED
                minutes = int(round((entry.lease_expires_at - entry.lease_granted_at) / 60))
                entry.state_note = f"the agent's control lease ended after {minutes} min; the owner renews or revokes"
                changed = True
        if changed:
            self._save()

    def lease_seconds_left(self, binding: BrowserBinding) -> int | None:
        """Seconds of agent control left, 0 when ended, None when the session has no term or is not the agent's."""
        entry = self._entries.get(binding.key())
        if entry is None or not entry.lease_expires_at:
            return None
        if entry.state not in AGENT_MAY_OPERATE and entry.state != STATE_LEASE_EXPIRED:
            return None
        return max(0, int(entry.lease_expires_at - self._clock()))

    def bind_lease_term(self, binding: BrowserBinding, seconds: Any) -> None:
        """The term the launching task set for this session's lease. From the task, never the model."""
        try:
            term = float(seconds or 0)
        except (TypeError, ValueError):
            return
        entry = self._entries.get(binding.key())
        if entry is None or term <= 0:
            return
        expires = entry.lease_granted_at + term
        if expires != entry.lease_expires_at:
            entry.lease_expires_at = expires
            self._expire_leases()

    def renew_lease(self, binding: BrowserBinding, *, note: str = "") -> dict[str, Any]:
        """The owner grants the agent a fresh term. Only an agent-side or expired session is renewable:
        a page a person holds is handed back and verified, not renewed."""
        self._expire_leases()
        entry = self._require(binding)
        if entry.state not in AGENT_MAY_OPERATE and entry.state != STATE_LEASE_EXPIRED:
            raise BrowserSessionRefused(
                f"browser session for {binding.describe()} is {entry.state}; a lease is renewed only for an agent-side or expired session",
                reason="not_renewable",
            )
        if entry.state == STATE_LEASE_EXPIRED:
            entry.state = STATE_READY
        entry.last_used = self._clock()
        self._grant_lease(entry)
        entry.state_note = str(note or "").strip() or "lease renewed by the owner"
        self._record_control_event(entry, "lease_renewed", note=entry.state_note)
        self._save()
        return self.state_of(binding)

    async def close(self, binding: BrowserBinding, *, actor: str = "owner") -> bool:
        """Close one binding's browser. False when it had none.

        `actor="agent"` (the agent's own browser_close) may not close a browser a person holds or has handed back,
        nor one whose lease ended: the person may be mid-login (C16), and those are the owner's to end.

        The reservation is kept until the close has actually returned. It
        used to be popped first, so a browser that refused to exit lost its
        slot to the next caller while it kept running. A failed or cancelled
        close leaves the entry quarantined; calling this again is the retry,
        and the only one -- nothing retries on a timer.
        """
        key = binding.key()
        async with self._lock:
            self._expire_leases()
            entry = self._entries.get(key)
            if entry is None:
                return False
            if entry.unavailable == "other_host":
                return False  # no browser was opened here; the record stays that host's fact
            if entry.unavailable == "account_recovery_required":
                raise BrowserSessionRefused("Account resource needs explicit recovery verification",
                                            reason="account_recovery_required")
            if actor == "agent":
                refusals = {STATE_AWAITING_HUMAN: ("awaiting_human", "a person is operating this browser; it is theirs until they hand it back"),
                            STATE_HUMAN_RETURNED: ("verification_required", "this browser was handed back and waits for verification"),
                            STATE_LEASE_EXPIRED: ("lease_expired", "the agent's control lease ended; the owner renews it or closes the session")}
                if entry.state in refusals:
                    reason, detail = refusals[entry.state]
                    raise BrowserSessionRefused(f"{detail} ({binding.describe()})", reason=reason)
            entry.unavailable = "closing"
        await self._close_entry(entry)
        return True

    async def close_all(self) -> int:
        """Close every session; the count is of closes that succeeded."""
        async with self._lock:
            entries = [entry for entry in self._entries.values()
                       if entry.unavailable not in {"other_host", "account_recovery_required"}]
            for entry in entries:
                entry.unavailable = "closing"
        closed = 0
        for entry in entries:
            try:
                await self._close_entry(entry)
            except BrowserSessionRefused:
                continue
            closed += 1
        return closed

    async def _close_entry(self, entry: _Entry) -> None:
        """Close one entry's browser. An agent-side record is removed; a hand-off, a hand-back, a revocation or an
        ended lease keeps its record (C13, C14: closing the browser -- the agent's browser_close included -- is not
        how a revoked or person-held binding comes back as a fresh session). A failed close is quarantined."""
        key = entry.binding.key()
        closed = False
        _retire(entry.runtime)
        closer = getattr(entry.runtime, "close", None)
        try:
            if callable(closer):
                await closer()
                closed = True
            elif entry.has_reservation():
                raise RuntimeError("No callable browser closer")
        except asyncio.CancelledError:
            entry.unavailable = "close_failed"
            raise
        except Exception:  # noqa: BLE001 - the provider's text does not leave the process
            entry.unavailable = "close_failed"
            self._save()
            raise BrowserSessionRefused(
                f"The browser for {entry.binding.describe()} did not close; its "
                "session is held until browser_close succeeds.",
                reason="close_failed",
            ) from None
        async with self._lock:
            kept = self._settle_closed(entry, note="browser closed", event="browser_closed", closed=closed)
            release_failed = entry.unavailable in {"close_failed", "account_recovery_required"}
            self._save()
        if kept:
            self._changed(entry)
        if release_failed:
            raise BrowserSessionRefused(
                "The browser account could not be released; its session remains quarantined.",
                reason=entry.unavailable,
            )

    # ── W05 lifecycle ────────────────────────────────────────────────────

    def state_of(self, binding: BrowserBinding) -> dict[str, Any]:
        """Where this session is in its lifecycle, for the console; nothing for an agent."""
        self._ensure_loaded()
        self._expire_leases()
        entry = self._entries.get(binding.key())
        if entry is None:
            return {"state": STATE_CLOSED, "note": "", "allowed_domains": []}
        return {
            "state": entry.state,
            "resource_id": entry.resource_id, "control_fence": entry.control_fence,
            "note": entry.state_note,
            "allowed_domains": list(entry.allowed_domains),
            "live": entry.is_live(),
            "lease_seconds_left": self.lease_seconds_left(binding),
        }

    def lease_view(self, binding: BrowserBinding) -> dict[str, Any]:
        """What the Computer Use Gateway needs to mirror this session's control lease (X02)."""
        self._ensure_loaded()
        self._expire_leases()
        entry = self._entries.get(binding.key())
        if entry is None:
            return {"state": STATE_CLOSED, "unavailable": "", "allowed_domains": [], "lease_seconds_left": None}
        if entry.unavailable.startswith("account_"):
            return {"state": STATE_REVOKED, "unavailable": entry.unavailable, "allowed_domains": [],
                    "lease_seconds_left": 0, "control_fence": entry.control_fence}
        try:
            self._check_account(entry)
        except BrowserSessionRefused:
            return {"state": STATE_REVOKED, "unavailable": "account_unavailable", "allowed_domains": [],
                    "lease_seconds_left": 0, "control_fence": entry.control_fence}
        return {"state": entry.state, "unavailable": entry.unavailable, "allowed_domains": list(entry.allowed_domains),
                "lease_seconds_left": self.lease_seconds_left(binding), "control_fence": entry.control_fence}

    def _require(self, binding: BrowserBinding) -> _Entry:
        entry = self._entries.get(binding.key())
        if entry is None:
            raise BrowserSessionRefused(
                f"no browser session for {binding.describe()}", reason="no_session",
            )
        if entry.state == STATE_REVOKED:
            raise BrowserSessionRefused(
                f"browser session for {binding.describe()} was revoked", reason="session_revoked",
            )
        return entry

    def subscribe(self, callback: Any) -> None:
        """Hear every state move of every session as one row (binding_key, project_id, task_id, state, note, live)."""
        if callable(callback) and callback not in self._observers:
            self._observers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        if callback in self._observers:
            self._observers.remove(callback)

    def _changed(self, entry: _Entry) -> None:
        if not self._observers:
            return
        binding = entry.binding
        row = {"binding_key": binding.key(), "project_id": binding.project_id, "customer_id": binding.customer_id,
               "task_id": binding.task_id, "state": entry.state, "note": entry.state_note[:300],
               "resource_id": entry.resource_id, "control_fence": entry.control_fence,
               "account": self.account_view(binding),
               "allowed_domains": list(entry.allowed_domains), "live": entry.is_live()}
        for callback in list(self._observers):
            try:
                callback(dict(row))
            except Exception:  # noqa: BLE001 - a listener's failure is not the session's
                pass

    def note_agent_operating(self, binding: BrowserBinding) -> None:
        """The agent drove the page. Only from a state that allows it."""
        entry = self._entries.get(binding.key())
        if entry is not None and entry.state in AGENT_MAY_OPERATE:
            changed = entry.state != STATE_AGENT_OPERATING
            entry.state = STATE_AGENT_OPERATING
            entry.state_note = ""
            if changed:
                self._save()
                self._changed(entry)

    def hand_to_human(self, binding: BrowserBinding, *, note: str = "", facts: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pause agent input; a person is about to operate the page."""
        entry = self._require(binding)
        entry.control_fence = uuid.uuid4().hex
        entry.state = STATE_AWAITING_HUMAN
        entry.last_used = self._clock()
        entry.state_note = str(note or "").strip() or "waiting for a person"
        entry.handoff_facts = dict(facts or {})
        self._record_control_event(entry, "hand_to_human", note=entry.state_note)
        self._save()
        self._changed(entry)
        return self.state_of(binding)

    def human_returned(self, binding: BrowserBinding, *, note: str = "") -> dict[str, Any]:
        """The person says they are done. That is a signal, not a verification."""
        entry = self._require(binding)
        if entry.state != STATE_AWAITING_HUMAN:
            raise BrowserSessionRefused(
                f"browser session for {binding.describe()} is {entry.state}, not awaiting a person",
                reason="not_awaiting_human",
            )
        if int(getattr(entry.runtime, "human_inputs_in_flight", 0) or 0) > 0:
            raise BrowserSessionRefused(
                "a browser input is still being dispatched; wait for its receipt before handing the page back",
                reason="human_input_in_flight",
            )
        entry.state = STATE_HUMAN_RETURNED
        entry.last_used = self._clock()
        entry.state_note = str(note or "").strip() or "handed back; not yet verified"
        self._record_control_event(entry, "human_returned", note=entry.state_note)
        self._save()
        self._changed(entry)
        return self.state_of(binding)

    def handoff_facts(self, binding: BrowserBinding) -> dict[str, Any]:
        entry = self._entries.get(binding.key())
        return dict(entry.handoff_facts) if entry is not None else {}

    def _record_control_event(self, entry: _Entry, action: str, *, note: str = "") -> None:
        """Append one owner control action in wall-clock time, bounded to the most recent."""
        entry.control_events.append({"action": str(action), "at": self._event_clock(), "note": str(note or "")[:300]})
        if len(entry.control_events) > _CONTROL_EVENT_CAP:
            del entry.control_events[: len(entry.control_events) - _CONTROL_EVENT_CAP]

    def control_events(self, binding: BrowserBinding) -> list[dict[str, Any]]:
        """The owner's control actions on this session, oldest first; empty for an unknown session."""
        self._ensure_loaded()
        entry = self._entries.get(binding.key())
        return [dict(event) for event in entry.control_events] if entry is not None else []

    def verify_identity(self, binding: BrowserBinding, *, ok: bool, checks: dict[str, Any] | None = None) -> dict[str, Any]:
        """The system's check after a hand-back: page state, account ownership, permissions.

        Only this moves the session back to a state the agent may operate in.
        A failed check leaves it handed back and says why; a person can try
        again. Nothing here reads a login as a release of anything.
        """
        entry = self._require(binding)
        if entry.state != STATE_HUMAN_RETURNED:
            raise BrowserSessionRefused(
                f"browser session for {binding.describe()} is {entry.state}; there is nothing to verify",
                reason="nothing_to_verify",
            )
        summary = ", ".join(f"{k}={v}" for k, v in sorted(dict(checks or {}).items()))
        entry.last_used = self._clock()
        if ok:
            entry.state = STATE_IDENTITY_VERIFIED
            entry.state_note = summary
            self._grant_lease(entry)  # control comes back to the agent on a fresh term
        else:
            entry.state_note = "verification failed" + (f": {summary}" if summary else "")
        self._record_control_event(entry, "identity_verified" if ok else "verification_failed", note=summary)
        self._save()
        self._changed(entry)
        return self.state_of(binding)

    def revoke(self, binding: BrowserBinding, *, reason: str) -> dict[str, Any]:
        """Refuse everything after. The record stays so a later call is
        refused as revoked rather than re-opened; closing the browser is
        `revoke_and_close`, because a close can fail and revocation cannot."""
        entry = self._entries.get(binding.key())
        if entry is None:
            return {"state": STATE_CLOSED, "note": "", "allowed_domains": []}
        entry.control_fence = uuid.uuid4().hex
        entry.state = STATE_REVOKED
        entry.last_used = self._clock()
        entry.state_note = str(reason or "").strip() or "revoked"
        self._record_control_event(entry, "revoked", note=entry.state_note)
        self._save()
        self._changed(entry)
        return self.state_of(binding)

    async def revoke_and_close(self, binding: BrowserBinding, *, reason: str) -> dict[str, Any]:
        """Revoke first -- that part cannot fail -- then try to close the
        browser. A close that fails leaves the entry revoked and marked, and
        the operator's quarantine list shows it; nothing is handed back."""
        state = self.revoke(binding, reason=reason)
        entry = self._entries.get(binding.key())
        _retire(getattr(entry, "runtime", None))
        closer = getattr(getattr(entry, "runtime", None), "close", None)
        closed = entry is None
        if entry is not None and callable(closer):
            try:
                await closer()
                closed = True
            except asyncio.CancelledError:
                entry.unavailable = "close_failed"
                raise
            except Exception:  # noqa: BLE001 - revoked either way; the close failure is quarantined
                entry.unavailable = "close_failed"
            if closed:
                # The browser is gone: its live and customer slots are free now, not after the idle window.
                async with self._lock:
                    kept = entry.runtime is not None and self._settle_closed(
                        entry, note="browser closed", event="browser_closed", closed=True)
                    self._save()
                if kept:
                    self._changed(entry)
        elif entry is not None and entry.has_reservation():
            entry.unavailable = "close_failed"
            self._save()
        # The receipt describes the session after the close, not before it:
        # `live` read true a moment ago while the browser was still up (V3,
        # real Chromium, 2026-09-12), and a console showing a revoked session
        # as live is a lie either way.
        state = self.state_of(binding)
        # Only a browser that is provably closed lets go of its profile:
        # deleting under a running one is how cookies get rewritten from
        # memory a moment later. A failed close keeps the disk as it is.
        state["profile_purged"] = False
        # A browser this process closed earlier (a first purge that hit a locked file) may be purged again now; a record
        # restored from a previous process is not -- its browser may still be running on the profile.
        closed_before = (entry is not None and entry.runtime is None and entry.closed_here
                         and entry.unavailable not in {"close_failed", "other_host"})
        retained_account = entry is not None and entry.binding.profile_scope is not None
        if (closed or closed_before) and binding.is_bound and not retained_account:
            try:
                state["profile_purged"] = bool(self._profile_purger(binding).get("removed"))
            except Exception:  # noqa: BLE001 - a purge failure is reported, never raised into a revoke
                state["profile_purged"] = False
        return state

    # ── W05 domain allow-list ───────────────────────────────────────────

    def bind_domains(self, binding: BrowserBinding, domains: Any) -> None:
        """Set the hosts this session may navigate to. From the task, never the model."""
        entry = self._require(binding)
        clean = tuple(sorted({host for item in list(domains or [])
                              if (host := str(item or "").strip().lower().lstrip("."))}))
        if entry.allowed_domains != clean:
            entry.allowed_domains = clean
            self._save()

    # P2 护栏一: what the shared rule's answers are called here. ``domain_not_allowed`` is the name this gate has
    # always raised and callers match on; the other two are the things it did not use to judge -- part of a site,
    # and the pages no scope ever includes.
    _NAVIGATION_REASONS = {"off_site": "domain_not_allowed", "off_path": "path_not_allowed",
                           "forbidden_page": "forbidden_page"}

    def check_host(self, binding: BrowserBinding, url: str) -> None:
        """Refuse a URL whose HOST is outside the delegated scope. Not a navigation.

        Used where the product reads something the flow named -- an API back-check, say -- which is a request and
        not a page. The page rules deliberately do not apply: an order API legitimately lives at
        ``/v2/orders/9/payment``, and refusing it would fail the very check that proves a submit landed.
        """
        from ampuly.computer_use.web_scope import host_in_scope

        entry = self._require(binding)
        if not entry.allowed_domains:
            raise BrowserSessionRefused(
                f"no hosts are delegated to {binding.describe()}", reason="no_delegated_hosts")
        host = (urlsplit(str(url or "")).hostname or "").lower()
        if not host_in_scope(host, entry.allowed_domains):
            raise BrowserSessionRefused(
                f"{host or 'this address'} is outside the domains approved for {binding.describe()}",
                reason="domain_not_allowed")

    def check_navigation(self, binding: BrowserBinding, url: str, *, by_person: bool = False) -> None:
        """Refuse a navigation outside the delegated scope, before the page moves.

        An entry may name a whole site (``example.com``, as every entry has until now) or an address on one
        (``example.com/creator``). Whatever the scope says, the account's own pages -- security, password,
        rebinding the phone, the wallet, withdrawal, the cashier -- are never opened by the product.
        """
        from ampuly.computer_use.web_scope import REFUSAL_WORDS, navigation_refusal

        entry = self._require(binding)
        scheme = (urlsplit(str(url or "")).scheme or "").lower()
        if scheme in {"data", "blob", "about", "chrome-error"}:
            return
        if not entry.allowed_domains:
            raise BrowserSessionRefused(
                f"no hosts are delegated to {binding.describe()}: delegate the order's hosts (master_delegate_browser), "
                "or allow hosts on this session from the owner console",
                reason="no_delegated_hosts",
            )
        if by_person:
            # A person the owner handed this session to is the authority on it. The pages the product never opens
            # are the ones it must not open BY ITSELF; the reason this module gives for refusing them is that a
            # person can still do them, and that is only true if this gate lets them. 「帮忙登录」、短信验证、
            # 实名 are exactly the hand-offs `handoff: {captcha: human, login: human}` exists for, and the session
            # holding the login is the only one that can finish them. The delegation itself still binds.
            self.check_host(binding, url)
            return
        refusal = navigation_refusal(str(url or ""), entry.allowed_domains)
        if not refusal:
            return
        host = (urlsplit(str(url or "")).hostname or "").lower()
        where = host if host else "navigation target has no host and this session"
        raise BrowserSessionRefused(
            f"{where} is outside what {binding.describe()} may open: {REFUSAL_WORDS[refusal]}",
            reason=self._NAVIGATION_REASONS[refusal],
        )

    def limits(self) -> dict[str, Any]:
        """The caps this registry enforces and where the host stands, for the owner's list."""
        free = self._memory_probe() if callable(self._memory_probe) else None
        return {"max_live": self.max_live, "live": self._reserved(), "max_per_customer": self.max_per_customer,
                "min_free_memory_mb": self.min_free_memory_mb, "free_memory_mb": free}

    def _reserved(self) -> int:
        """Local runtimes or unclosed account owners reserve slots; restored records hold neither."""
        return sum(1 for entry in self._entries.values() if entry.has_reservation())

    def live_bindings(self) -> list[BrowserBinding]:
        self._ensure_loaded()
        return [entry.binding for entry in self._entries.values() if entry.is_live()]

    def known_bindings(self) -> list[BrowserBinding]:
        self._ensure_loaded()
        return [entry.binding for entry in self._entries.values()]

    def quarantined_bindings(self) -> list[BrowserBinding]:
        """Sessions whose browser could not be proven closed. For the
        operator; an agent never sees this list."""
        self._ensure_loaded()
        return [
            entry.binding
            for entry in self._entries.values()
            if entry.unavailable == "close_failed"
        ]

    def operator_sessions(self) -> list[dict[str, Any]]:
        """A bounded, non-secret projection for the owner control surface."""
        self._ensure_loaded()
        rows: list[dict[str, Any]] = []
        for entry in self._entries.values():
            binding = entry.binding
            state = self.state_of(binding)
            rows.append({
                "binding_key": binding.key(),
                "project_id": binding.project_id,
                "customer_id": binding.customer_id,
                "environment_id": binding.environment_id,
                "task_id": binding.task_id,
                "resource_id": entry.resource_id,
                "control_fence": entry.control_fence,
                "state": state["state"],
                "note": state["note"],
                "allowed_domains": state["allowed_domains"],
                "live": state["live"],
                "lease_seconds_left": state.get("lease_seconds_left"),
                "quarantined": entry.unavailable == "close_failed",
                "other_host": entry.unavailable == "other_host",
                "latest_screenshot": self._latest_screenshot_name(entry),
                "tabs": self._tabs_of(entry),
                "egress": self._egress_of(entry),
                "account": self.account_view(binding),
            })
        return sorted(rows, key=lambda row: (row["project_id"], row["customer_id"], row["task_id"]))

    @staticmethod
    def _tabs_of(entry: _Entry) -> list[dict[str, Any]]:
        """The session's tabs as the runtime lists them (index, host and path, active): never page content."""
        lister = getattr(entry.runtime, "tabs", None)
        if not callable(lister):
            return []
        try:
            return [{"index": int(t.get("index", 0)), "page": str(t.get("page", ""))[:200], "active": bool(t.get("active"))}
                    for t in list(lister() or [])][:20]
        except Exception:  # noqa: BLE001 - a runtime that cannot say has no tabs to show
            return []

    @staticmethod
    def _egress_of(entry: _Entry) -> dict[str, Any]:
        """What the request-level policy refused for this session: host names and a count, never a URL (C26)."""
        reporter = getattr(entry.runtime, "egress_report", None)
        if not callable(reporter):
            return {"policy": [], "blocked": 0, "hosts": []}
        try:
            report = dict(reporter() or {})
            return {"policy": [str(h) for h in list(report.get("policy", []) or [])][:50], "blocked": int(report.get("blocked", 0) or 0),
                    "hosts": [str(h) for h in list(report.get("hosts", []) or [])][:50]}
        except Exception:  # noqa: BLE001 - a runtime that cannot say has nothing to report
            return {"policy": [], "blocked": 0, "hosts": []}

    @staticmethod
    def _latest_screenshot_name(entry: _Entry) -> str:
        """The file name of the last masked screenshot this session's runtime produced, or ""."""
        shot = getattr(entry.runtime, "last_screenshot", None) or {}
        path = str(shot.get("path", "") or "") if isinstance(shot, dict) else ""
        return Path(path).name if path and shot.get("redacted") else ""

    def binding_for_key(self, binding_key: str) -> BrowserBinding | None:
        """Resolve an opaque operator key without accepting scope from a client."""
        self._ensure_loaded()
        entry = self._entries.get(str(binding_key or "").strip())
        return entry.binding if entry is not None else None

    async def _expire_idle(self) -> None:
        """Close the browsers nobody has touched inside the expiry window.

        Called on the way in rather than from a timer: a process with no
        browser traffic has nothing to expire, and a background sweeper would
        be one more thing that can wedge holding a lock.

        The browser goes; what the record says does not (C13, C14, C16). An agent-side session is dropped as
        before, and its next call starts afresh. A session a person holds, one handed back and not yet verified,
        a revoked one or one whose lease ended keeps its record, as a restart keeps it: dropping it would let the
        next agent call open a fresh session for a binding that was revoked, or is mid-login, and skip the
        verification. A record with no browser (restored from a previous process) has nothing to close.
        """
        if self.idle_expiry_sec <= 0:
            return
        now = self._clock()
        # Quarantined entries are skipped: this runs on every lookup, and a
        # browser that would not close must not be hammered by whoever
        # happens to call next. A person retries it, once, on purpose.
        stale = [
            entry
            for entry in self._entries.values()
            if not entry.unavailable and now - entry.last_used > self.idle_expiry_sec
        ]
        kept: list[_Entry] = []
        for entry in stale:
            # Judged again as the sweep reaches it: an earlier close can take seconds, and meanwhile a person may have
            # used this page (touch), the owner acted on it, or another close taken it.
            current = self._clock()
            if (self._entries.get(entry.binding.key()) is not entry or entry.unavailable
                    or current - entry.last_used <= self.idle_expiry_sec):
                continue
            if entry.runtime is None and self._state_without_browser(entry) != STATE_CREATED:
                continue
            idle_minutes = int(round((current - entry.last_used) / 60))
            entry.unavailable = "closing"
            _retire(entry.runtime)
            closer = getattr(entry.runtime, "close", None)
            closed = False
            try:
                if callable(closer):
                    await closer()
                    closed = True
                elif entry.has_reservation():
                    raise RuntimeError("No callable browser closer")
            except asyncio.CancelledError:
                entry.unavailable = "close_failed"
                raise
            except Exception:  # noqa: BLE001
                entry.unavailable = "close_failed"
                continue
            if entry.unavailable != "closing":
                continue  # another close settled or quarantined it meanwhile
            if self._settle_closed(entry, note=f"browser closed after {idle_minutes} min idle",
                                   event="idle_closed", closed=closed):
                kept.append(entry)
        if kept:
            self._save()
            for entry in kept:
                self._changed(entry)

    def touch(self, binding: BrowserBinding) -> None:
        """A person working the page (pane input, pane navigation) keeps the session from going idle."""
        entry = self._entries.get(binding.key())
        if entry is not None:
            entry.last_used = self._clock()


def _retire(runtime: Any) -> None:
    retire = getattr(runtime, "retire", None)
    if callable(retire):
        retire()


def _revive(runtime: Any) -> None:
    revive = getattr(runtime, "revive", None)
    if callable(revive):
        revive()


def profile_dir_for(binding: BrowserBinding) -> str:
    """Where a bound session keeps its own profile.

    Never the operator's Chrome profile, whatever the config says. That
    directory holds a person's real logins and is not an agent's to write
    into, and pointing several customers at one profile is how a cookie jar
    becomes shared. The unbound session keeps the configured behaviour,
    because it is the one that had it.
    """
    if not binding.is_bound:
        return ""
    return str(get_ampuly_home() / "browser" / "profiles" / binding.key())


def _default_profile_purger(binding: BrowserBinding) -> dict[str, Any]:
    from ampuly.layer4_tools.browser_profiles import purge_profile_for

    return purge_profile_for(binding)


def try_optional_ego_backend_for_binding(
    binding: BrowserBinding,
    *,
    task_id: str,
    env: dict[str, str] | None = None,
) -> Any:
    """Soft-open EgoBackend for a binding (legacy Ego-only helper).

    Returns None when Playwright is selected or Ego cannot open. Prefer
    try_open_hybrid_backend_for_binding for the B2 sessions entry that returns
    PlaywrightBackend when Ego is not selected. Soft: never raises.
    Shared-surface window: docs/4.9-EGO-OPTIONAL-BACKEND-WINDOW-2026-10-27.zh.md
    """
    from ampuly.layer4_tools.browser_provider import try_open_optional_ego_backend

    profile_id = binding.key().replace(":", "_").replace("/", "_")
    backend, _reason = try_open_optional_ego_backend(
        profile_id=profile_id,
        task_id=task_id,
        env=env,
    )
    return backend


def _hybrid_profile_id_for_binding(binding: BrowserBinding) -> str:
    """Stable profile id for Ego/Spaces from the gateway session key."""
    return binding.key().replace(":", "_").replace("/", "_")


def open_hybrid_backend_for_binding(
    binding: BrowserBinding,
    *,
    task_id: str,
    env: dict[str, str] | None = None,
    spaces=None,
    runner=None,
    require_windows: bool = True,
    artifact_dir: str | Path | None = None,
    runtime=None,
    runtime_factory: Callable[[], Any] | None = None,
) -> tuple[Any, str, str]:
    """B2: EgoBackend if Ego resolvable/selected, else PlaywrightBackend.

    Returns (backend, kind, selection_reason). Raises on hard ego/config errors.
    Does not replace _default_runtime_factory / BrowserRuntime this leaf.
    """
    from ampuly.layer4_tools.browser_provider import open_selected_browser_backend

    return open_selected_browser_backend(
        profile_id=_hybrid_profile_id_for_binding(binding),
        task_id=task_id,
        env=env,
        spaces=spaces,
        runner=runner,
        require_windows=require_windows,
        artifact_dir=artifact_dir,
        runtime=runtime,
        runtime_factory=runtime_factory,
    )


def try_open_hybrid_backend_for_binding(
    binding: BrowserBinding,
    *,
    task_id: str,
    env: dict[str, str] | None = None,
    spaces=None,
    runner=None,
    require_windows: bool = True,
    artifact_dir: str | Path | None = None,
    runtime=None,
    runtime_factory: Callable[[], Any] | None = None,
) -> tuple[Any | None, str, str]:
    """Soft B2 hybrid open for a binding. Never raises.

    Returns (backend_or_None, kind_or_empty, reason).
    """
    from ampuly.layer4_tools.browser_provider import try_open_selected_browser_backend

    return try_open_selected_browser_backend(
        profile_id=_hybrid_profile_id_for_binding(binding),
        task_id=task_id,
        env=env,
        spaces=spaces,
        runner=runner,
        require_windows=require_windows,
        artifact_dir=artifact_dir,
        runtime=runtime,
        runtime_factory=runtime_factory,
    )


def _default_runtime_factory(binding: BrowserBinding, *, account_profile_dir: str = "") -> Any:
    from ampuly.layer4_tools.browser import BrowserLaunchConfig, BrowserRuntime

    profile = account_profile_dir or profile_dir_for(binding)
    if not profile:
        return BrowserRuntime()

    def _bound_config() -> BrowserLaunchConfig:
        base = BrowserLaunchConfig.load()
        # dataclasses.replace would do, but naming the field here is the
        # point: this is the line that refuses to reuse a person's profile.
        return BrowserLaunchConfig(
            mode=base.mode,
            headless=base.headless,
            chrome_channel=base.chrome_channel,
            chrome_executable_path=base.chrome_executable_path,
            user_data_dir=profile,
            args=base.args,
            persistent_profile=bool(account_profile_dir),
            trusted_spki_sha256=base.trusted_spki_sha256,
        )

    return BrowserRuntime(config_loader=_bound_config)
