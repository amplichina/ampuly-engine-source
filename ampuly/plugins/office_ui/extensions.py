"""Console extensions: modules shipped with the engine that the console reaches only through named seams.

An extension is a package of its own (``ampuly/plugins/wecom_service`` is the first) that registers one
:class:`ConsoleExtension` here. The console asks this registry instead of naming the extension anywhere else. What an
extension can fill in, and which part of the console asks:

* ``ws_actions`` -- websocket actions, merged into ``WSHandler._HANDLERS`` (``install_ws_actions``), never over a
  console action; owner-only unless ``api.ACTION_ROLES`` names them (it names none of an extension's). Each is called
  as ``(host, ws, data)``, the host being the console seen through ``extension_host.HandlerConsoleHost``.
* ``on_duty_round_start`` / ``on_duty_round`` -- the duty round (``WSHandler._duty_watch_tick``): the first,
  synchronous, where the round starts; the second, awaited, after the order close cards went out.
* ``on_shutdown`` -- the console stopping (``WSHandler.shutdown``), once ``_shutting_down`` is set.
* ``order_timeline_extra`` -- fields added to an ``order_timeline`` ack: ``(host, ws, order) -> dict`` (only added,
  never an existing field).
* ``feishu_card`` -- a Feishu card kind of its own (:class:`FeishuCardKind`), in the gate group (``feishu_gate``).
* ``project_records`` -- its own business tables that belong to a project (:class:`ProjectRecords`): purged, carried
  in the project's backup, put back and taken back out with the project's records (``business_store``).
* ``customer_lane`` -- facts for the project centre's 客户确认 lane (``project_connections.gather``).
* ``contact_channels`` -- the contact-book channels it binds identities on, with the owner's word for each
  (``contact_identity``, ``project_connections``).
* ``secret_env_names`` -- key-storage variables the settings page may write and ``settings_service.configured_key``
  may read (``settings_service``).
* ``order_source_words`` -- the owner's word for an order ``source`` it creates (``project_connections``).

The built-in extensions (``BUILTIN``) are loaded once: at the console's start (``server.create_app``, and when
``ws_handler`` is imported), else on the first question asked of this registry. One that cannot be imported, or that
raises while registering, registers nothing: the console works without it and the log says so.

**Switching one off.** The environment variable ``AMPULY_DISABLED_EXTENSIONS`` names built-in extensions to leave out,
comma separated -- ``AMPULY_DISABLED_EXTENSIONS=wecom_service`` -- and is read when the built-ins are loaded, i.e. at the
console's start (a change takes effect at the next start). Default: every built-in extension loads. Switched off,
nothing of an extension runs -- no action, no duty round, no card, no link -- but when it offers a data upkeep
(``register_data_upkeep``) that is still registered: a project's purge, backup and restore keep covering its tables, so
deleting a project never leaves its customers' data behind (2026-09-28). Not importable, it registers nothing.
"""

from __future__ import annotations

import importlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from loguru import logger

DISABLE_ENV = "AMPULY_DISABLED_EXTENSIONS"
# The built-in extensions: name -> the package that registers it (its ``register_console_extension(register)``).
BUILTIN: dict[str, str] = {
    "crm_agent": "ampuly.plugins.crm_agent",
    "wecom_service": "ampuly.plugins.wecom_service",
    "customer_service": "ampuly.plugins.customer_service_console",
}


@dataclass(frozen=True)
class FeishuCardKind:
    """A card of an extension's own in the Feishu gate group (``feishu_gate``). No checkpoint is behind it: its
    ``gate_cards`` row is keyed by the extension (``target`` reads a key back, None for any other card), the card is
    built by ``build_card``, a press on it is handled by ``on_event`` once the gate service found its spec, and the duty
    round's retire sweep asks ``retire_reason`` whether it still stands.

    ``hooks`` builds the console-bound half (``load_spec(checkpoint_id)`` to rebuild a card after a restart,
    ``answer(...)`` for its buttons; its ``card_kind`` is ``name``); a gate service built without it rebuilds none of
    these cards and answers none of their buttons."""

    name: str                                              # GateSpec.card_kind
    label: str                                             # the card's title after the order
    decisions: Mapping[str, str]                           # its buttons' action ids -> the owner's word for each
    target: Callable[[str], Any]                           # checkpoint id -> what it names, or None
    build_card: Callable[..., dict[str, Any]]              # (spec, image_keys, *, status_line="") -> card
    on_event: Callable[..., Awaitable[dict[str, Any]]]     # (service, spec, action, hooks) -> outcome
    retire_reason: Callable[[Any, str], tuple[str, str]]   # (service, checkpoint id) -> (reason, newer key)
    hooks: Callable[[Any], Any] | None = None              # console host -> hooks
    not_this_card: str = "not_this_card"                   # a press of one of its buttons on another card is told this


@dataclass(frozen=True)
class ProjectRecords:
    """An extension's own business tables that belong to a project (``business_store``'s project records). Every hook
    runs inside the store's own transaction, on its connection, and checks that its tables exist: a database below the
    extension's schema version has none of them."""

    tables: tuple[str, ...]                                                # carried in a project's records
    export: Callable[[Any, str], dict[str, list[dict[str, Any]]]]          # (conn, project) -> rows per table
    purge: Callable[[Any, str], dict[str, int]]                            # (conn, project) -> rows removed per table
    check: Callable[[str, Mapping[str, list[dict[str, Any]]]], None]       # (project, rows): refuse before any write
    prepare_import: Callable[[Any, str, dict[str, list[dict[str, Any]]]], None]  # (conn, project, rows): refuse an
    #                                                                        overwrite; may change the rows to insert
    inserts: tuple[tuple[str, tuple[str, ...]], ...]                       # (table, columns not carried), in order
    discard: Callable[[Any, str, Mapping[str, Any]], None]                 # (conn, project, the export's tables)


@dataclass(frozen=True)
class ConsoleExtension:
    """Everything one extension hooks into the console; every part is optional."""

    name: str
    ws_actions: Mapping[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    on_duty_round_start: Callable[[Any], Any] | None = None
    on_duty_round: Callable[[Any], Awaitable[Any]] | None = None
    on_shutdown: Callable[[Any], Any] | None = None
    order_timeline_extra: Callable[[Any, Any, Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None
    feishu_card: FeishuCardKind | None = None
    project_records: ProjectRecords | None = None
    # (project id, business store) -> the lane's facts (``project_connections.CustomerLaneExtra``).
    customer_lane: Callable[[str, Any], Any] | None = None
    contact_channels: Mapping[str, str] = field(default_factory=dict)
    secret_env_names: tuple[str, ...] = ()
    order_source_words: Mapping[str, str] = field(default_factory=dict)


def disabled_names(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The built-in extensions the owner switched off (``AMPULY_DISABLED_EXTENSIONS``)."""
    raw = str((os.environ if environ is None else environ).get(DISABLE_ENV, "") or "")
    return frozenset(part.strip().lower() for part in raw.replace("，", ",").split(",") if part.strip())


class Registry:
    """The registered extensions. One per process (``_REGISTRY``); a test may put a fresh one in its place."""

    def __init__(self) -> None:
        self._extensions: dict[str, ConsoleExtension] = {}
        self._loaded = False
        self._loading = False
        # Held for the whole load, so a second thread asking meanwhile waits for it; reentrant, because an extension
        # registers from inside the load.
        self._lock = threading.RLock()

    def register(self, extension: ConsoleExtension) -> None:
        if not isinstance(extension, ConsoleExtension):
            raise TypeError("a console extension registers a ConsoleExtension")
        name = str(extension.name or "").strip()
        if not name:
            raise ValueError("a console extension needs a name")
        with self._lock:
            self._extensions[name] = extension

    def registered(self) -> tuple[ConsoleExtension, ...]:
        if not self._loaded:
            self.load_builtin()
        with self._lock:
            return tuple(self._extensions.values())

    def load_builtin(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        """Import and register every built-in extension not switched off, once; returns the names registered. Asked
        again (or from inside a load), it only answers."""
        with self._lock:
            if self._loaded or self._loading:
                return tuple(self._extensions)
            self._loading = True
            try:
                off = disabled_names(environ)
                for name, module_name in BUILTIN.items():
                    if name in off:
                        # Switched off: nothing of it runs, but its tables' upkeep (a project's purge, backup and
                        # restore) stays, when it offers one -- a destroyed project keeps no customer data behind.
                        logger.info("console extensions: {} is switched off ({}); only its data upkeep stays", name, DISABLE_ENV)
                        try:
                            module = importlib.import_module(module_name)
                            upkeep = getattr(module, "register_data_upkeep", None)
                            if callable(upkeep):
                                upkeep(self.register)
                        except Exception:
                            self._extensions.pop(name, None)
                            logger.opt(exception=True).warning("console extensions: {} data upkeep did not register", name)
                        continue
                    try:
                        module = importlib.import_module(module_name)
                        getattr(module, "register_console_extension")(self.register)
                    except ImportError:
                        self._extensions.pop(name, None)
                        logger.opt(exception=True).warning("console extensions: {} could not be imported; the console runs without it", name)
                    except Exception:
                        self._extensions.pop(name, None)
                        logger.opt(exception=True).warning("console extensions: {} did not register; the console runs without it", name)
            finally:
                self._loaded = True
                self._loading = False
            return tuple(self._extensions)


_REGISTRY = Registry()


def register(extension: ConsoleExtension) -> None:
    _REGISTRY.register(extension)


def registered() -> tuple[ConsoleExtension, ...]:
    return _REGISTRY.registered()


def load_builtin(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    return _REGISTRY.load_builtin(environ)


# ── websocket actions ──


def ws_actions() -> dict[str, Callable[..., Awaitable[None]]]:
    """Every registered extension's ws actions, by name (the first extension to name one keeps it)."""
    out: dict[str, Callable[..., Awaitable[None]]] = {}
    for extension in registered():
        for name, action in dict(extension.ws_actions or {}).items():
            out.setdefault(str(name), action)
    return out


def _console_action(action: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    async def run(handler: Any, ws: Any, data: dict) -> None:
        from ampuly.plugins.office_ui.extension_host import console_host

        await action(console_host(handler), ws, data)

    run.__extension_action__ = action  # type: ignore[attr-defined]
    run.__name__ = str(getattr(action, "__name__", "") or "extension_action")
    return run


def install_ws_actions(table: dict[str, Any]) -> list[str]:
    """Merge the registered extensions' ws actions into the console's action table (``WSHandler._HANDLERS``), each
    called with the console host in place of the handler. Never over an action the table already has -- a console
    action is never replaced; one installed before stays. Returns the names added now."""
    added: list[str] = []
    for name, action in ws_actions().items():
        present = table.get(name)
        if present is not None:
            if getattr(present, "__extension_action__", None) is not action:
                logger.warning("console extensions: the ws action {} is the console's own; the extension's was not installed", name)
            continue
        table[name] = _console_action(action)
        added.append(name)
    return added


# ── the duty round, the stop, the order record ──


def duty_round_start(host: Any) -> None:
    """Each registered extension's ``on_duty_round_start``; one that fails is logged and the round goes on."""
    for extension in registered():
        if extension.on_duty_round_start is None:
            continue
        try:
            extension.on_duty_round_start(host)
        except Exception:
            logger.opt(exception=True).warning("duty watch: extension {} could not be looked after this round", extension.name)


async def duty_round(host: Any) -> None:
    """Each registered extension's ``on_duty_round``; one that fails is logged and the round goes on."""
    for extension in registered():
        if extension.on_duty_round is None:
            continue
        try:
            await extension.on_duty_round(host)
        except Exception:
            logger.opt(exception=True).warning("duty watch: extension {} could not do its part of this round", extension.name)


def shutdown(host: Any) -> None:
    """Each registered extension's ``on_shutdown``; one that fails is logged and the stop goes on."""
    for extension in registered():
        if extension.on_shutdown is None:
            continue
        try:
            extension.on_shutdown(host)
        except Exception:
            logger.opt(exception=True).warning("shutdown: extension {} could not stop cleanly", extension.name)


async def order_timeline_extra(host: Any, ws: Any, order: Mapping[str, Any]) -> dict[str, Any]:
    """The fields the registered extensions add to one order's ``order_timeline`` ack (never one already named by an
    earlier extension); one that fails adds nothing and is logged."""
    extra: dict[str, Any] = {}
    for extension in registered():
        if extension.order_timeline_extra is None:
            continue
        try:
            added = dict(await extension.order_timeline_extra(host, ws, order) or {})
        except Exception:
            logger.opt(exception=True).warning("order timeline: extension {} added nothing to order {}", extension.name,
                                               str(dict(order or {}).get("order_id", "") or ""))
            continue
        for key, value in added.items():
            extra.setdefault(str(key), value)
    return extra


# ── Feishu card kinds ──


def feishu_card_kinds() -> tuple[FeishuCardKind, ...]:
    return tuple(extension.feishu_card for extension in registered() if extension.feishu_card is not None)


def feishu_card_kind(name: str) -> FeishuCardKind | None:
    """The registered card kind called ``name`` (a GateSpec's ``card_kind``); None for any other."""
    clean = str(name or "")
    return next((kind for kind in feishu_card_kinds() if kind.name == clean), None)


def feishu_card_kind_for(checkpoint_id: str) -> FeishuCardKind | None:
    """The registered card kind whose key this is; None for a checkpoint or another card."""
    for kind in feishu_card_kinds():
        try:
            if kind.target(str(checkpoint_id or "")) is not None:
                return kind
        except Exception:  # noqa: BLE001 - a key it cannot read is not its key
            continue
    return None


def feishu_decisions() -> dict[str, str]:
    """Every registered card kind's button action ids -> the owner's word for each."""
    words: dict[str, str] = {}
    for kind in feishu_card_kinds():
        for action, word in dict(kind.decisions or {}).items():
            words.setdefault(str(action), str(word))
    return words


def feishu_card_hooks(host: Any) -> list[Any]:
    """The console-bound hooks of every registered card kind that has them, for ``GateService(card_hooks=...)``."""
    hooks: list[Any] = []
    for kind in feishu_card_kinds():
        if kind.hooks is None:
            continue
        try:
            hooks.append(kind.hooks(host))
        except Exception:
            logger.opt(exception=True).warning("feishu gate: the {} cards have no hooks in this console", kind.name)
    return hooks


# ── the business store, the project centre, contacts, keys ──


def project_records() -> tuple[ProjectRecords, ...]:
    return tuple(extension.project_records for extension in registered() if extension.project_records is not None)


def project_record_tables() -> tuple[str, ...]:
    """The tables the registered extensions carry in a project's records, in registration order."""
    tables: list[str] = []
    for records in project_records():
        tables.extend(name for name in records.tables if name not in tables)
    return tuple(tables)


def customer_lane_readers() -> tuple[tuple[str, Callable[[str, Any], Any]], ...]:
    """(extension name, reader) for every registered extension that adds to the 客户确认 lane."""
    return tuple((extension.name, extension.customer_lane) for extension in registered() if extension.customer_lane is not None)


def contact_channels() -> dict[str, str]:
    """Contact-book channels the registered extensions bind identities on -> the owner's word for each."""
    channels: dict[str, str] = {}
    for extension in registered():
        for name, word in dict(extension.contact_channels or {}).items():
            channels.setdefault(str(name), str(word))
    return channels


def secret_env_names() -> frozenset[str]:
    """The key-storage variables the registered extensions use."""
    return frozenset(str(name) for extension in registered() for name in extension.secret_env_names)


def order_source_words() -> dict[str, str]:
    """Order ``source`` values the registered extensions create -> the owner's word for each."""
    words: dict[str, str] = {}
    for extension in registered():
        for source, word in dict(extension.order_source_words or {}).items():
            words.setdefault(str(source), str(word))
    return words
