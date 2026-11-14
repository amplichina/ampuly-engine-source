"""A project's engine, attached to the console (architecture candidate 3, step 1).

``ProjectEngines.engine(project_id, load=True)`` is the console's one answer to "give me project P's engine":

* **Whether P may have one at all.** A destroyed project gets none, and none is built for it: building one
  re-creates its folder and an empty store, which then looks like a live customer space (the 2026-09-06 destroy
  drill). The status is read at every loading lookup through a store the console keeps open, one SELECT on a
  connection of its own, so a project another process destroyed (the ampuly CLI, the board) is seen at once. An id that
  is not a project id gets none either.
* **Where it lives.** The root engine answers for its own project and a delegate for every other. The kernel's
  private delegate machinery is read in exactly one place, ``_kernel_view``.
* **A store closed under it** (the project deleted and made again while this engine stayed bound to it) is opened
  again before the engine is handed out.
* **What the console hangs on it** -- the progress and event callbacks, the effect and credential binders, a step
  model's key resolver, the desktop tools, the node-invoke launcher, the owner's registered connectors -- is attached
  once per engine instance, and per attachment. One that cannot be made yet (a part of a partly built engine is
  missing) or that failed is tried again at a later handout; one that went through never is. The marks sit on the
  engine itself (a weak map only for an engine that takes no attribute), never in a strong map, so an engine the
  kernel lets go takes its marks with it; a part rebuilt under an engine (a new provider, a new executor) is attached
  again.

``load=False`` answers only from what this process already runs: it never builds an engine and never reads a
project's status. What it hands out is attached all the same.

A custom org's child runtime (``layer2 custom_runtime``) never passes through a lookup: it is attached when the
kernel calls the kanban callback factory hung on the engine that builds it (``_child_runtime_factory``) -- once,
fully initialized, before any message runs, and only for a child that built a company executor.

Not covered, because it is the kernel's: a delegate the kernel builds itself inside ``process_message`` for another
project carries the root's callbacks until one of these lookups hands it out (a custom org child it builds meanwhile
is attached all the same, under its own project).
"""

from __future__ import annotations

import inspect
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from loguru import logger

from ampuly.plugins.office_ui.services.context import ProjectDestroyedError
from ampuly.project_id import is_valid_project_id


class UnknownProjectError(ValueError):
    """Raised when something asks for the engine of an id that is not a project id."""

    def __init__(self, project_id: str) -> None:
        super().__init__("项目编号不合规，没有为它打开运行环境")
        self.project_id = project_id


def normalize_project_id(project_id: Any) -> str:
    return str(project_id or "default").strip() or "default"


# ── the kernel's side: the one place its private delegate machinery is read ─────────────────────────────────────


def _is_real_ampuly_engine(engine: Any) -> bool:
    try:
        from ampuly.engine import AmpulyEngine
    except Exception:  # noqa: BLE001 - no kernel importable means no real engine here
        return False
    return isinstance(engine, AmpulyEngine)


def _kernel_view(root: Any) -> tuple[str, dict[str, Any], Callable[[str], Any] | None]:
    """The root's own project, the delegates it already holds, and how to have it build one (None when it cannot).

    A root that is not an ``AmpulyEngine`` builds delegates only through a ``_get_project_delegate`` set on the object
    itself: a mock's auto-created attribute is not a delegate factory, and treating it as one handed out mocks as
    engines.
    """
    root_project = normalize_project_id(getattr(root, "project_id", None))
    delegates = getattr(root, "_project_engine_delegates", None)
    held = dict(delegates) if isinstance(delegates, dict) else {}
    getter = getattr(root, "_get_project_delegate", None)
    explicit = "_get_project_delegate" in getattr(root, "__dict__", {})
    build = getter if callable(getter) and (explicit or _is_real_ampuly_engine(root)) else None
    return root_project, held, build


# ── attachments ──────────────────────────────────────────────────────────────────────────────────────────────────

# A part answer: this attachment has nothing to do on this engine (the root forwards its own events).
NOT_NEEDED = object()


@dataclass(frozen=True)
class Attachment:
    """One thing the console hangs on an engine.

    ``part(engine)`` is what it is attached to -- the engine, or one of its parts (provider, executor, event bus) --
    or None while that part is missing (tried again at the next handout), or ``NOT_NEEDED``. ``apply(engine, part)``
    attaches it; what it returns is kept with the mark, and an exception marks it failed.
    """

    name: str
    part: Callable[[Any], Any]
    apply: Callable[[Any, Any], Any]


@dataclass
class _Mark:
    owner: Any  # weakref to the ProjectEngines that attached it
    part: Any  # the part it was attached to; None = the engine itself (no reference cycle)
    ok: bool
    at: float
    result: Any = None
    error: str = ""


_MARKS_ATTR = "_ampuly_console_attachments"
# Engines that take no attribute but can be weakly referenced keep their marks here instead. An engine that takes
# neither is attached at every handout, as every engine was before.
_WEAK_MARKS: "weakref.WeakKeyDictionary[Any, dict[str, _Mark]]" = weakref.WeakKeyDictionary()


def _marks_of(engine: Any, *, create: bool) -> dict[str, _Mark] | None:
    try:
        own = vars(engine)
    except TypeError:
        own = None
    if own is not None:
        # Read from the instance's own attributes: a mock answers any getattr with a new mock.
        marks = own.get(_MARKS_ATTR)
        if isinstance(marks, dict):
            return marks
        if not create:
            return None
        marks = {}
        try:
            setattr(engine, _MARKS_ATTR, marks)
        except Exception:  # noqa: BLE001 - an engine refusing the attribute falls through to the weak map
            marks = None
        if marks is not None and vars(engine).get(_MARKS_ATTR) is marks:
            return marks
    try:
        marks = _WEAK_MARKS.get(engine)
        if marks is None and create:
            marks = _WEAK_MARKS.setdefault(engine, {})
        return marks
    except TypeError:
        return None


class ProjectEngines:
    """The console's source of project engines. See the module docstring."""

    # How long an attachment that failed waits before a handout tries it again. A screen switch does not wait.
    RETRY_AFTER_FAILURE_SEC = 60.0

    def __init__(
        self,
        *,
        root: Callable[[], Any],
        attachments: Iterable[Attachment],
        is_destroyed: Callable[[str], bool] | None = None,
        store_ready: Callable[[Any], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = root
        self._attachments = tuple(attachments)
        self._is_destroyed = is_destroyed
        self._store_ready = store_ready or _store_is_ready
        self._clock = clock
        self._self_ref = weakref.ref(self)

    # ── lookups ──

    async def engine(self, project_id: str, *, load: bool = True) -> Any | None:
        """Project P's engine, attached; None when P has none: destroyed, not a project id, or -- with
        ``load=False`` -- not running in this process. Anything else that goes wrong (no delegates, the kernel
        closing its doors for shutdown) raises."""
        if not load:
            return self.loaded(project_id)
        try:
            return await self.require(project_id)
        except (ProjectDestroyedError, UnknownProjectError):
            return None

    async def require(self, project_id: str) -> Any:
        """``engine(project_id)`` that says why there is none: ``ProjectDestroyedError``, ``UnknownProjectError``."""
        normalized = normalize_project_id(project_id)
        if not is_valid_project_id(normalized):
            raise UnknownProjectError(normalized)
        if self._destroyed(normalized):
            raise ProjectDestroyedError(normalized)
        root = self._root()
        if root is None:
            raise RuntimeError("the console has no engine to answer for projects")
        root_project, _held, build = _kernel_view(root)
        if normalized == root_project:
            engine = root
        else:
            # Always the kernel's own answer, even for a delegate it already holds: it is the one that refuses new
            # work while the controller shuts down, and the one that drops a delegate whose store closed.
            if build is None:
                raise RuntimeError(
                    "Project switching requires AmpulyEngine project delegates or an explicit "
                    "_get_project_delegate test double."
                )
            made = build(normalized)
            engine = await made if inspect.isawaitable(made) else made
        await _reopen_closed_store(engine, normalized)
        self.attach(engine)
        return engine

    def loaded(self, project_id: str) -> Any | None:
        """The engine this process already runs for P -- the root or a delegate whose store is open -- attached; or
        None. Never builds, never reads a store."""
        engine = self.peek(project_id)
        if engine is not None:
            self.attach(engine)
        return engine

    def peek(self, project_id: str) -> Any | None:
        """Read an existing engine without building, reopening or attaching parts."""
        normalized = normalize_project_id(project_id)
        root = self._root()
        if root is None:
            return None
        root_project, held, _build = _kernel_view(root)
        if normalized == root_project:
            engine = root
        else:
            engine = held.get(normalized)
            if engine is None or not self._store_ready(getattr(engine, "store", None)):
                return None
        return engine

    def loaded_project_ids(self) -> list[str]:
        """The projects whose engine this process runs: the root's first, then each delegate's, sorted."""
        root = self._root()
        if root is None:
            return []
        root_project, held, _build = _kernel_view(root)
        return list(dict.fromkeys([root_project, *sorted(normalize_project_id(pid) for pid in held)]))

    def _destroyed(self, project_id: str) -> bool:
        if self._is_destroyed is None:
            return False
        try:
            return bool(self._is_destroyed(project_id))
        except Exception:  # noqa: BLE001 - a status that cannot be read never blocks ordinary work
            logger.opt(exception=True).debug("project engines: the status of project {} could not be read", project_id)
            return False

    # ── attaching ──

    def attach(self, engine: Any, *, retry_failed: bool = False) -> dict[str, str]:
        """Attach what is not attached to this engine yet. Per attachment: ``attached``, ``waiting`` (a part is
        missing), ``failed`` (tried again after ``RETRY_AFTER_FAILURE_SEC``, or at once with ``retry_failed``) or
        ``not_needed``. Never raises."""
        report: dict[str, str] = {}
        if engine is None:
            return report
        marks = _marks_of(engine, create=True)
        now = self._clock()
        for spec in self._attachments:
            try:
                part = spec.part(engine)
            except Exception:  # noqa: BLE001 - a part that cannot be looked at is a part not there yet
                part = None
            if part is None:
                report[spec.name] = "waiting"
                continue
            if part is NOT_NEEDED:
                report[spec.name] = "not_needed"
                continue
            mark = marks.get(spec.name) if marks is not None else None
            if mark is not None and self._is_mine(mark, engine, part):
                if mark.ok:
                    report[spec.name] = "attached"
                    continue
                if not retry_failed and now - mark.at < float(self.RETRY_AFTER_FAILURE_SEC):
                    report[spec.name] = "failed"
                    continue
            try:
                result = spec.apply(engine, part)
            except Exception as exc:  # noqa: BLE001 - one attachment failing never keeps the others off
                project = normalize_project_id(getattr(engine, "project_id", None))
                if spec.name == "registered_connectors":
                    logger.opt(exception=True).warning("console attach: the owner's connectors could not be bound for project {}", project)
                else:
                    logger.opt(exception=True).debug("console attach: {} failed for project {}", spec.name, project)
                new = _Mark(self._self_ref, None if part is engine else part, False, now, None, str(exc)[:300])
                report[spec.name] = "failed"
            else:
                new = _Mark(self._self_ref, None if part is engine else part, True, now, result, "")
                report[spec.name] = "attached"
            if marks is not None:
                marks[spec.name] = new
        return report

    def _is_mine(self, mark: _Mark, engine: Any, part: Any) -> bool:
        return mark.owner() is self and (engine if mark.part is None else mark.part) is part

    def attached(self, engine: Any, name: str) -> bool:
        """Whether this console's attachment ``name`` went through on this engine (its part unchanged since)."""
        return self.outcome(engine, name)[0]

    def outcome(self, engine: Any, name: str) -> tuple[bool, Any, str]:
        """``(attached, what it returned, the error of its last failure)`` for one attachment of this engine."""
        marks = _marks_of(engine, create=False) or {}
        mark = marks.get(name)
        spec = next((item for item in self._attachments if item.name == name), None)
        if mark is None or spec is None:
            return False, None, ""
        try:
            part = spec.part(engine)
        except Exception:  # noqa: BLE001
            part = None
        if part is None or part is NOT_NEEDED or not self._is_mine(mark, engine, part):
            return False, None, ""
        return mark.ok, mark.result, mark.error

    def mark_attached(self, engine: Any, name: str, result: Any = None) -> None:
        """Record that ``name`` was attached to this engine by other means (an attachment whose part is the engine
        itself), so a later handout does not attach it again."""
        marks = _marks_of(engine, create=True)
        if marks is not None:
            marks[name] = _Mark(self._self_ref, None, True, self._clock(), result, "")


def _store_is_ready(store: Any) -> bool:
    if not store:
        return False
    ready = getattr(store, "is_ready", None)
    if callable(ready):
        return bool(ready())
    return True if ready is None else bool(ready)


async def _reopen_closed_store(engine: Any, project_id: str) -> None:
    """A store closed under a loaded engine (the project deleted then made again while this instance stayed bound to
    it -- the root engine, say, which never leaves its own delegate cache) is opened again."""
    store = getattr(engine, "store", None)
    if store is not None and not getattr(store, "is_ready", True):
        ensure_ready = getattr(store, "ensure_ready", None)
        if callable(ensure_ready):
            logger.warning(f"Reopening closed store for project '{project_id}'")
            opened = ensure_ready()
            if inspect.isawaitable(opened):
                await opened


# ── what the console hangs on an engine ─────────────────────────────────────────────────────────────────────────


def console_attachments(console: Any) -> tuple[Attachment, ...]:
    """The console's attachments, in the order they are made. ``console`` is the WSHandler; its methods are looked
    up at each attach, so a binder replaced on it later is the one used."""
    return (
        Attachment("ui_callbacks", lambda engine: engine, lambda engine, _part: _attach_ui_callbacks(console, engine)),
        Attachment("interaction_presentation", lambda engine: getattr(engine, "interaction_coordinator", None),
                   lambda _engine, coordinator: setattr(coordinator, "presentation_callback", None)),
        Attachment("executor_callbacks", lambda engine: getattr(engine, "company_executor", None),
                   lambda engine, executor: _attach_executor_callbacks(console, engine, executor)),
        Attachment("reorg_progress", lambda engine: getattr(engine, "reorg_manager", None),
                   lambda engine, reorg: setattr(reorg, "progress_callback", console._progress_callback_for_engine(engine))),
        Attachment("effect_bindings", lambda engine: _effect_bindings_part(engine, console),
                   lambda engine, _part: console._bind_channel_credential_check(engine)),
        Attachment("step_model_resolver", _step_model_part, _attach_step_model_resolver),
        Attachment("desktop_connector", _desktop_part, lambda engine, _part: _attach_desktop_connector(console, engine)),
        Attachment("event_forwarding", lambda engine: _event_forwarding_part(console, engine),
                   lambda engine, bus: _attach_event_forwarding(console, engine, bus)),
        Attachment("node_invoke_launcher", lambda engine: getattr(engine, "company_executor", None),
                   lambda engine, _executor: console._bind_node_invoke_callback(engine)),
        Attachment("registered_connectors", _connectors_part,
                   lambda engine, _part: console._bind_registered_connector_runtime(engine, normalize_project_id(getattr(engine, "project_id", None)))),
    )


def _attach_ui_callbacks(console: Any, engine: Any) -> None:
    engine.on_company_runtime_children = console._register_company_runtime_children
    engine.on_company_kanban_callback_factory = _child_runtime_factory(console, engine)
    # Human interactions are durable ExecutionCheckpoints. Office UI must never install the legacy synchronous
    # escalation callback: custom runtimes have their own EventBus and a missed callback would otherwise turn a tool
    # permission into generic user input.
    engine.on_escalation = None
    engine.on_progress = console._progress_callback_for_engine(engine)
    engine.on_runtime_event = console._runtime_event_callback_for_engine(engine)


def _child_runtime_factory(console: Any, engine: Any) -> Callable[[Any], Any]:
    """The kanban callback factory the kernel calls with a custom org's child runtime (layer2 custom_runtime).

    The kernel calls it once per child, after the child is initialized and has taken its provider's resolvers from
    the engine that built it, before any message runs on it -- and only for a child that built a company executor.
    The child is attached to this console like every engine the console hands out: its progress and event callbacks
    bound to the child itself (same project as the engine that built it; the typed runtime-event forwarder it
    inherited is replaced by one subscription to its own bus, so an event is delivered once), the node-invoke
    launcher, the owner's registered connectors, a step model's key resolver, the desktop tools, the effect bindings.
    The marks sit on the child and go with it. Then its kanban refresh, as before.

    Never raises: the kernel calls this after the part of the child's construction that shuts the child down on an
    error, so an exception here would leave the child registered with the engine that built it and running until
    that engine shuts down. A failed attachment is logged; the marks say which one, as for any engine.
    """

    def factory(child: Any) -> Any:
        if child is not None and child is not engine:
            try:
                console._project_engines().attach(child)
            except Exception:  # noqa: BLE001 - see the docstring: the child must still be handed back
                logger.opt(exception=True).warning(
                    "console attach: a custom org runtime of project {} could not be attached",
                    normalize_project_id(getattr(child, "project_id", None)),
                )
        return console._kanban_callback_for_engine(child)

    return factory


def _attach_executor_callbacks(console: Any, engine: Any, executor: Any) -> None:
    executor.progress_callback = console._progress_callback_for_engine(engine)
    executor.on_kanban_changed = console._kanban_callback_for_engine(engine)


class _EffectBindingSources:
    """Identity of what the effect bindings (budget reader, holds, checks) were made from.

    The attachment mark keeps this object; while the engine's Store connection, gateway, approval service, home,
    project and the console's account store stay the same objects, the same instance is handed back and nothing is
    re-bound. When any of them changes -- a Store closed and opened again under a loaded engine, a replaced account
    store -- a new instance is handed out, the mark no longer matches and the next handout binds afresh. The old
    reader already refuses on its own; this only restores a correct one.
    """

    __slots__ = ("sources",)

    def __init__(self, sources: tuple[Any, ...]) -> None:
        self.sources = sources

    def same(self, sources: tuple[Any, ...]) -> bool:
        return len(sources) == len(self.sources) and all(a is b for a, b in zip(sources, self.sources))


_SOURCES_ATTR = "_console_effect_binding_sources"


def _effect_bindings_part(engine: Any, console: Any = None) -> Any:
    """What the effect bindings hang on: absent until the engine has an effect gateway listing providers, a home and
    an open Store; then an identity of its binding sources, new whenever one of them changes."""
    gateway = getattr(engine, "effect_gateway", None)
    providers = getattr(gateway, "providers", None)
    home = getattr(engine, "ampuly_home", None)
    if not callable(providers) or home is None:
        return None
    store = getattr(engine, "store", None)
    if store is not None and not _store_is_ready(store):
        return None  # nothing is bound on a closed Store; the old reader keeps refusing
    sources = (
        gateway, getattr(engine, "effect_approval_service", None), store, getattr(store, "_db", None), home,
        getattr(engine, "project_id", None), getattr(console, "_account_store", None) if console is not None else None,
    )
    try:
        current = vars(engine).get(_SOURCES_ATTR)
    except TypeError:
        current = None
    if isinstance(current, _EffectBindingSources) and current.same(sources):
        return current
    fresh = _EffectBindingSources(sources)
    try:
        setattr(engine, _SOURCES_ATTR, fresh)
    except Exception:  # noqa: BLE001 - an engine refusing the attribute rebinds at every handout, still correct
        pass
    return fresh


def _step_model_part(engine: Any) -> Any:
    llm = getattr(engine, "llm", None)
    return llm if callable(getattr(llm, "bind_model_key_resolver", None)) else None


def _attach_step_model_resolver(_engine: Any, llm: Any) -> bool:
    """A step's own model finds its provider's key variable through the console's catalogue, and the residency
    setting through this provider's own config: bound per provider, never through the process-wide fallback, so two
    projects' engines in one process never read each other's setting (step_models.current_residency)."""
    from ampuly.plugins.office_ui.step_models import bind_step_model_resolver

    return bind_step_model_resolver(llm, process_default=False)


def _desktop_part(engine: Any) -> Any:
    registry = getattr(engine, "tool_registry", None)
    return engine if callable(getattr(registry, "register", None)) else None


def _attach_desktop_connector(console: Any, engine: Any) -> None:
    """4.7-A06: a desktop step's tools, and the owner's desktop channel switch (closed unless explicitly opened).
    The switch is read when a step asks, so an owner who opens it does not need a restart."""
    from ampuly.computer_use.desktop_runtime_binding import bind_desktop_runtime

    bind_desktop_runtime(engine, lambda name: console._accounts().business.connector_switch(name))


def _event_forwarding_part(console: Any, engine: Any) -> Any:
    if engine is getattr(console, "_root_engine", None):
        return NOT_NEEDED  # the server subscribes the root engine's events itself
    if getattr(engine, "_initialized", True) is False:
        return None  # initialize() still subscribes its own forwarder; attach after it
    bus = getattr(engine, "event_bus", None)
    return bus if bus is not None and callable(getattr(bus, "subscribe_all", None)) else None


def _attach_event_forwarding(console: Any, engine: Any, bus: Any) -> None:
    """Delegates need all events, not only runtime_event. They may have inherited a typed runtime forwarder during
    initialize(); it is removed so runtime_event is not delivered twice."""
    forward_runtime_event = getattr(engine, "_forward_runtime_event", None)
    listeners_by_type = getattr(bus, "_listeners", {})
    runtime_list = listeners_by_type.get("runtime_event", []) if isinstance(listeners_by_type, dict) else []
    if callable(forward_runtime_event) and isinstance(runtime_list, list):
        runtime_list[:] = [listener for listener in runtime_list if listener != forward_runtime_event]
    project_marker = normalize_project_id(getattr(engine, "project_id", None))
    already_subscribed = any(
        getattr(listener, "_ampuly_ui_handler_id", None) == id(console)
        and getattr(listener, "_ampuly_ui_project_id", None) == project_marker
        for listener in list(getattr(bus, "_global_listeners", []) or [])
    )
    if not already_subscribed:
        bus.subscribe_all(console._runtime_event_callback_for_engine(engine))


def _connectors_part(engine: Any) -> Any:
    """The engine, once it has an effect gateway, a tool registry and a home of its own to read registrations from."""
    ready = getattr(engine, "effect_gateway", None) is not None and getattr(engine, "tool_registry", None) is not None
    return engine if ready and isinstance(getattr(engine, "ampuly_home", None), (str, Path)) else None
