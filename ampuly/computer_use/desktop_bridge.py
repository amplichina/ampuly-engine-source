"""The desktop channel of a flow step (4.7-A06): a step's desktop connector reaches the desktop helper here, through
the one Computer Use Gateway, the way a browser step's tools do (browser_bridge).

What this module is, and what it is not:

- It is wiring. The product binds no helper: until one is bound (``bind_helper``), a step that reaches the desktop
  stops and asks a person. The tests bind ``FakeDesktopHelper``; nothing here drives a real screen by itself.
- The desktop channel is closed unless the owner opened it. ``bind_channel_check`` is how the console tells this
  module (the owner's switch, explicit only); without a check the channel is closed. Closed, a step can be saved but
  not run: its desktop tools stop the turn and ask.
- Every action goes through the Gateway (``browser_bridge.gateway()``, one per process, one journal): the desktop is
  observed first, the action admitted against that observation, handed to the helper with a single-use capability
  (``desktop_helper.run_through_helper``) and receipted. The journal is the record that survives a restart: an input
  whose outcome is unknown -- or an admission that never got a receipt -- blocks the step's next write, and is never
  sent again on its own.
- An unknown desktop input is cleared by a person, not by the step's own look: the model only ever sees an app name,
  a window title and a size, which cannot tell it whether its typing landed. ``observe(person_checked=True)`` clears
  it only together with the person's recorded answer to the step's own question about that very input (``answered``,
  the question's pending mark, read off the task by desktop_connector); without one it asks the person. The receipt
  says so (source ``human``).
- A step touches only the apps it names. Keystrokes that can commit (Enter, a hotkey) are external effects and are
  refused at the Gateway; no shell, no app launch, no clipboard, no file dialog (the helper has no words for them).
- Done is a code check of what the desktop shows against the step's done-when conditions, never the model's word --
  and only of THIS turn: the Gateway session is per order and step, so it outlives a rework round, and the check counts
  only the actions made since the kernel stamped the turn (``TURN_KEY``), and only a condition that did not already hold
  before the turn's first action (the browser's not_this_turn / held_before_the_work). Where the kernel stamps nothing
  (a trial, a run outside a company work item) the turn starts at the step's first desktop look or action in its
  harness turn (``FALLBACK_TURN_KEY``).
- A turn that ends DONE is complete only when this turn's check still stands at its end (``done_standing``, asked by
  connector_done_wait.as_pause): a deliverable written with an ordinary file tool is not a desktop check.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import inspect
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Callable

from ampuly.computer_use import browser_bridge
from ampuly.computer_use.contracts import ActionClass, ActionReceipt, ContractRefused, Observation, Outcome
from ampuly.computer_use.desktop_helper import HelperCapabilityBroker, HelperView, SecureInputBroker, run_through_helper
from ampuly.computer_use.gateway import AGENT_MAY_ACT, ComputerUseGateway, ControlLease, GatewayRefused, LeaseState
from ampuly.computer_use.vision_actions import parse_prediction, to_computer_action

COORDINATE_TTL_SEC = 30.0
# Test-only shortening of the coordinate lease. None keeps the product 30s.
# coordinate_ttl_sec() refuses to return anything longer than that default.
_ttl_override: float | None = None
_FRESH_LOOK: set[str] = set()
# Last desktop look the model was shown for this session (title + hash only; no pixels).
_VIEW_PROJECTION: dict[str, dict[str, str]] = {}
_HUMAN_CONTROL = frozenset({
    LeaseState.HANDOFF_REQUESTED, LeaseState.AWAITING_HUMAN, LeaseState.HUMAN_OPERATING,
    LeaseState.HUMAN_RETURNED,
})
_IDENTITY_REFUSAL = "window_identity_changed"
# The UI-TARS words a desktop step may use. `hotkey` is left out: a chord can commit, and the helper has no text for
# one; `finished` is a request for the done check (finish), `call_user` a hand-off to a person.
ACT_FUNCTIONS = frozenset({"click", "left_click", "left_single", "left_double", "double_click", "right_single",
                           "right_click", "drag", "type", "scroll", "wait"})
POSTCONDITION_KINDS = ("window_title_contains",)
# Why a desktop tool stopped the turn for a person (the console words each one).
STOP_CHANNEL_CLOSED = "channel_closed"
STOP_NO_HELPER = "no_helper"
STOP_UNKNOWN_OUTCOME = "unknown_outcome"
STOP_PERSON_ASKED = "person_asked"
_WRITE_KINDS = frozenset({"select_candidate", "type_draft"})
_LOCKING_REFUSALS = frozenset({"unresolved_outcome", "retry_without_facts", "turn_cancelled", "journal_unreadable"})
# When this work item's current turn started (connector_done_wait.TURN_KEY; the kernel stamps it before each turn of a
# connector or desktop step, from time.time() -- the Gateway's own clock). Named here, not imported: the computer-use
# layer does not import the organisation layer.
TURN_KEY = "connector_turn_started_at"
# Where no kernel stamp exists (a trial, a run outside a company work item) the step's turn is taken to start at its
# first desktop look or action in the harness turn it runs in (``harness_turn_id``, a fresh id per turn): {turn, at}.
FALLBACK_TURN_KEY = "desktop_turn_started"
_BASELINE = "desktop_baseline"
_DONE_CHECK = "desktop_done_check"

_channel_check: Callable[[], bool] | None = None
_helper: Any = None
_broker: HelperCapabilityBroker | None = None
_texts: SecureInputBroker | None = None
_SCOPED_HELPER: ContextVar[Any] = ContextVar('ampuly_desktop_helper_binding', default=None)
_PROJECT_GATES: Any = weakref.WeakValueDictionary()


def bind_project_gate(project_id, binding):
    if (type(project_id) is not str or not project_id or len(project_id) > 128
            or not callable(getattr(binding, 'enabled', None))
            or not callable(getattr(binding, 'helper_available', None))):
        raise ValueError('desktop_binding_invalid')
    _PROJECT_GATES[project_id] = binding


def unbind_project_gate(project_id, binding):
    """An old engine may only remove its own exact published gate."""
    if _PROJECT_GATES.get(project_id) is binding:
        _PROJECT_GATES.pop(project_id, None)


def _project_gate(task):
    return _PROJECT_GATES.get(getattr(task, 'project_id', None)) if task is not None else None


@contextmanager
def helper_scope(helper, broker, texts, *, channel_check):
    """Server-only task binding; serializable metadata cannot activate it."""
    if (_SCOPED_HELPER.get() is not None or not callable(channel_check)
            or type(broker) is not HelperCapabilityBroker or type(texts) is not SecureInputBroker):
        raise ValueError('desktop_binding_invalid')
    token = _SCOPED_HELPER.set((helper, broker, texts, channel_check))
    try:
        yield
    finally:
        _SCOPED_HELPER.reset(token)


def _helper_objects():
    scoped = _SCOPED_HELPER.get()
    return scoped[:3] if scoped is not None else (_helper, _broker, _texts)
# The Gateway the ids below belong to. A new Gateway (a restart; browser_bridge.configure) starts a new namespace, so
# an attempt id a previous process spent is never offered again.
_gateway_ref: ComputerUseGateway | None = None
_nonce = secrets.token_hex(4)
_sequences: dict[str, int] = {}


def bind_channel_check(check: Callable[[], bool] | None) -> None:
    """How this module asks whether the owner opened the desktop channel. None: closed."""
    global _channel_check
    _channel_check = check


def channel_open(task=None) -> bool:
    """The owner opened the desktop channel. A check that cannot answer is a closed channel."""
    scoped = _SCOPED_HELPER.get()
    project = _project_gate(task)
    check = scoped[3] if scoped is not None else project.enabled if project is not None else _channel_check
    if check is None:
        return False
    try:
        return check() is True
    except Exception:  # noqa: BLE001 - a switch that cannot be read does not open anything
        return False


def bind_helper(helper: Any, *, broker: HelperCapabilityBroker | None = None, texts: SecureInputBroker | None = None) -> None:
    """The helper this process drives, with the capability and text brokers it redeems against. None unbinds."""
    global _helper, _broker, _texts
    _helper = helper
    _broker = broker if broker is not None else getattr(helper, "broker", None)
    _texts = texts if texts is not None else getattr(helper, "texts", None)


def helper_bound(task=None) -> bool:
    project = _project_gate(task)
    if _SCOPED_HELPER.get() is None and project is not None:
        return project.helper_available() is True
    return all(value is not None for value in _helper_objects())


def reset_for_tests() -> None:
    global _gateway_ref, _nonce, _ttl_override
    bind_channel_check(None)
    bind_helper(None)
    _gateway_ref = None
    _nonce = secrets.token_hex(4)
    _sequences.clear()
    _PROJECT_GATES.clear()
    _FRESH_LOOK.clear()
    _VIEW_PROJECTION.clear()
    _ttl_override = None


def coordinate_ttl_sec() -> float:
    """Product coordinate lease. A test may only shorten it, never extend it."""
    override = _ttl_override
    if type(override) not in (int, float) or isinstance(override, bool):
        return COORDINATE_TTL_SEC
    value = float(override)
    if not 0 < value <= COORDINATE_TTL_SEC:
        return COORDINATE_TTL_SEC
    return value


@contextmanager
def coordinate_ttl_for_tests(seconds: float):
    """Shrink the coordinate lease for a test. Values above 30s are rejected.

    The product constant ``COORDINATE_TTL_SEC`` stays 30. This does not read
    an environment variable and is not a runtime setting.
    """
    global _ttl_override
    if type(seconds) not in (int, float) or isinstance(seconds, bool) or not 0 < float(seconds) <= COORDINATE_TTL_SEC:
        raise ValueError("coordinate_ttl_override_invalid")
    previous = _ttl_override
    _ttl_override = float(seconds)
    try:
        yield
    finally:
        _ttl_override = previous


def _controller(lease: ControlLease) -> str:
    if lease.state in AGENT_MAY_ACT:
        return "agent"
    if lease.state in _HUMAN_CONTROL:
        return "human"
    return "none"


def _runtime(task: Any):
    binding = _project_gate(task)
    return getattr(binding, "_lifecycle", None) if binding is not None else None


def _hold_runtime_input(task: Any, reason: str) -> None:
    projection = getattr(_project_gate(task), 'ui_frames', None)
    if projection is not None:
        projection.revoke_task(getattr(task, 'id', None))
    lifecycle = _runtime(task)
    hold = getattr(lifecycle, "hold_input", None)
    if callable(hold):
        hold(reason)
    indicator = getattr(_project_gate(task), 'indicator', None)
    if indicator is not None:
        indicator.pause_task(getattr(task, 'id', None))


def _release_runtime_input(task: Any) -> None:
    lifecycle = _runtime(task)
    release = getattr(lifecycle, "release_input", None)
    if callable(release):
        release()


def _drop_sent_frame(task: Any) -> None:
    binding = _project_gate(task)
    holder = getattr(binding, "frames", None) if binding is not None else None
    vault = getattr(holder, "vault", None) if holder is not None else None
    if vault is not None and callable(getattr(vault, "clear", None)):
        vault.clear()


def _frame_coordinate_deadline(task: Any) -> float | None:
    """Expiry of the frame the model was shown, when one is still current."""
    binding = _project_gate(task)
    holder = getattr(binding, "frames", None) if binding is not None else None
    vault = getattr(holder, "vault", None) if holder is not None else None
    sent = getattr(vault, "last_sent", None) if vault is not None else None
    geometry = sent.get("geometry") if isinstance(sent, dict) else None
    expires = getattr(geometry, "expires_at", None)
    if type(expires) not in (int, float) or isinstance(expires, bool):
        return None
    return float(expires)


def helper_refusal() -> str:
    """A pinned window that was recreated, when the helper recorded that fact."""
    helper = _helper_objects()[0]
    reason = getattr(helper, "refusal", "") if helper is not None else ""
    return reason if reason == _IDENTITY_REFUSAL else ""


def input_block(task: Any, node_id: str) -> str:
    """Why the agent may not send desktop input now, or '' when it may.

    Does not open a session. No lease yet means this fence is clear; the
    channel and helper gates still apply.
    """
    session_id = session_for(task, node_id)
    try:
        lease = _gateway().lease(session_id)
    except GatewayRefused:
        return ""
    if session_id in _FRESH_LOOK or lease.state is LeaseState.HUMAN_RETURNED:
        return "desktop_fresh_look_required"
    if lease.state not in AGENT_MAY_ACT:
        return "agent_not_in_control"
    return ""


def agent_may_input(task: Any, node_id: str | None) -> bool:
    if type(node_id) is not str or not node_id:
        return False
    return input_block(task, node_id) == ""


def request_takeover(task: Any, node_id: str, *, source: str = "api") -> dict[str, Any]:
    """Person takes the desktop. The lease walks the gateway's human states.

    ``AGENT_MAY_ACT`` → ``HANDOFF_REQUESTED`` → ``AWAITING_HUMAN`` →
    ``HUMAN_OPERATING``. OS input is held at ``input_allowed`` until a later
    hand-back and a fresh look. Does not edit the gateway; it only calls
    ``set_lease_state``.
    """
    note = str(source or "api")[:80]
    session_id = session_for(task, node_id)
    lease = _lease(session_id)
    gateway = _gateway()
    if lease.state is LeaseState.HUMAN_OPERATING:
        _hold_runtime_input(task, "human_operating")
        return {"ok": True, "state": lease.state.value, "controller": "human", "fresh_look_required": False}
    if lease.state is LeaseState.HUMAN_RETURNED:
        gateway.set_lease_state(session_id, LeaseState.HUMAN_OPERATING, note=note)
        _FRESH_LOOK.discard(session_id)
        _hold_runtime_input(task, "human_operating")
        return {"ok": True, "state": LeaseState.HUMAN_OPERATING.value, "controller": "human",
                "fresh_look_required": False}
    if lease.state in AGENT_MAY_ACT or lease.state is LeaseState.CREATED:
        steps = (LeaseState.HANDOFF_REQUESTED, LeaseState.AWAITING_HUMAN, LeaseState.HUMAN_OPERATING)
    elif lease.state is LeaseState.HANDOFF_REQUESTED:
        steps = (LeaseState.AWAITING_HUMAN, LeaseState.HUMAN_OPERATING)
    elif lease.state is LeaseState.AWAITING_HUMAN:
        steps = (LeaseState.HUMAN_OPERATING,)
    else:
        return {"ok": False, "refused": "desktop_takeover_unavailable", "state": lease.state.value,
                "controller": _controller(lease)}
    for state in steps:
        gateway.set_lease_state(session_id, state, note=note)
    _hold_runtime_input(task, "human_operating")
    return {"ok": True, "state": LeaseState.HUMAN_OPERATING.value, "controller": "human",
            "fresh_look_required": False}


def hand_back(task: Any, node_id: str) -> dict[str, Any]:
    """Person returns the desktop. The old frame is dropped.

    The lease becomes ``HUMAN_RETURNED``, which is not in ``AGENT_MAY_ACT``.
    The next agent look is what restores ``AGENT_OPERATING``. Until that look
    lands, input stays held and the frame the model already saw is refused.
    """
    session_id = session_for(task, node_id)
    try:
        lease = _gateway().lease(session_id)
    except GatewayRefused:
        return {"ok": False, "refused": "desktop_not_in_human_control"}
    if lease.state is not LeaseState.HUMAN_OPERATING:
        return {"ok": False, "refused": "desktop_not_in_human_control", "state": lease.state.value,
                "controller": _controller(lease)}
    _gateway().set_lease_state(session_id, LeaseState.HUMAN_RETURNED, note="hand_back")
    _FRESH_LOOK.add(session_id)
    _drop_sent_frame(task)
    _hold_runtime_input(task, "fresh_look_required")
    return {"ok": True, "state": LeaseState.HUMAN_RETURNED.value, "controller": "human",
            "fresh_look_required": True}


def readonly_lease(task: Any) -> dict[str, Any] | None:
    """Existing Gateway window/lease only; no initialization or new observation."""
    from ampuly.plugins.office_ui.desktop_connector import _step
    step = _step(task)
    gateway = _gateway_ref
    if step is None or gateway is None or gateway is not browser_bridge._gateway:
        return None
    session_id = session_for(task, step[1])
    session = gateway._sessions.get(session_id)
    if session is None:
        return None
    lease, observation = session.lease, session.latest_observation
    if (not lease.agent_may_act(gateway._clock())
            or observation is None or observation.geometry is None
            or observation.task_id != getattr(task, 'id', None)
            or observation.scope_digest != lease.scope_digest):
        return None
    return {'identity': (id(gateway), session_id, lease.lease_id, lease.scope_digest, lease.state.value),
            'geometry': observation.geometry}


def control_view(task: Any, node_id: str) -> dict[str, Any]:
    """Read-only lease view for the owner console. Does not create a lease.

    Projection fields (T11 terminal/live-view closeout): window_title / screen_hash /
    frame_delivered / last_refusal are owner-console facts only — no pixels, no new helper.
    """
    session_id = session_for(task, node_id)
    projection = _VIEW_PROJECTION.get(session_id) or {}
    last_refusal = helper_refusal() or input_block(task, node_id) or ""
    if not last_refusal:
        # Most recent gateway refused annotate, when any.
        try:
            for event in reversed(_gateway().events(session_id)):
                if event.get("type") == "refused":
                    last_refusal = str(event.get("reason") or "")[:200]
                    break
        except Exception:
            pass
    fields = {
        "window_title": str(projection.get("window_title") or ""),
        "screen_hash": str(projection.get("screen_hash") or ""),
        "frame_delivered": bool(projection),
        "last_refusal": str(last_refusal)[:200],
        "app": str(projection.get("app") or ""),
    }
    try:
        lease = _gateway().lease(session_id)
    except GatewayRefused:
        return {"ok": True, "state": "none", "controller": "none",
                "fresh_look_required": False, **fields}
    return {
        "ok": True,
        "state": lease.state.value,
        "controller": _controller(lease),
        "fresh_look_required": session_id in _FRESH_LOOK,
        **fields,
    }



def note_foreign_foreground(task: Any, node_id: str) -> dict[str, Any]:
    """The pinned window is no longer the OS foreground. Same lease walk as the API."""
    return request_takeover(task, node_id, source="foreground")


def gate(task=None) -> str:
    """'' when a desktop step may act now; otherwise why it stops for a person."""
    if not channel_open(task):
        return STOP_CHANNEL_CLOSED
    if not helper_bound(task):
        return STOP_NO_HELPER
    return ""


def app_key(name: Any) -> str:
    """An app as the allow-list matches it: the program's own name, lower case, without a folder or ``.exe``/``.com``
    (``cmd.com`` is ``cmd``)."""
    text = str(name or "").strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    return text[:-4] if text.endswith((".exe", ".com")) else text


def app_allowed(app: Any, allowed: list[str] | tuple[str, ...]) -> bool:
    key = app_key(app)
    return bool(key) and key in {app_key(item) for item in allowed or () if app_key(item)}


def session_for(task: Any, node_id: str) -> str:
    """One Gateway session per order and step, so the next process finds what this one left unknown."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    project = str(getattr(task, "project_id", "") or "").strip() or "default"
    anchor = str(metadata.get("order_id", "") or "").strip() or str(getattr(task, "id", "") or "").strip()
    digest = hashlib.sha256(f"{project}|{anchor}|{node_id}".encode("utf-8")).hexdigest()[:32]
    return f"desktop-{digest}"


def _stamp(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def turn_started(task: Any) -> float | None:
    """When this turn of the step started, or None when nothing says.

    The kernel's stamp (``TURN_KEY``, written before each turn of a company work item) when there is one. Without it
    -- a trial, a run outside a company work item -- the start of this harness turn's desktop work
    (``FALLBACK_TURN_KEY``, see ``_note_turn_start``), and only while the task is still in that same harness turn: a
    later turn has another ``harness_turn_id`` and its own start. Neither: None, and nothing is counted."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    kernel = _stamp(metadata.get(TURN_KEY))
    if kernel is not None:
        return kernel
    harness_turn = str(metadata.get("harness_turn_id", "") or "").strip()
    fallback = metadata.get(FALLBACK_TURN_KEY)
    if not harness_turn or not isinstance(fallback, dict) or str(fallback.get("turn", "") or "") != harness_turn:
        return None
    return _stamp(fallback.get("at"))


def _note_turn_start(task: Any) -> None:
    """No kernel stamp: this harness turn's desktop work starts now (its first look or action), on the Gateway's clock.

    Written once per harness turn and never over a kernel stamp. A task with neither a kernel stamp nor a harness turn
    id is left alone: nothing then shows which actions are whose, and the done check counts none."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    harness_turn = str(metadata.get("harness_turn_id", "") or "").strip()
    if task is None or not harness_turn or _stamp(metadata.get(TURN_KEY)) is not None or turn_started(task) is not None:
        return
    try:
        task.metadata = {**metadata, FALLBACK_TURN_KEY: {"turn": harness_turn, "at": _gateway()._clock()}}
    except Exception:  # noqa: BLE001 - a task that cannot carry the stamp is counted as unstamped (stricter)
        return


def _at(event: dict[str, Any]) -> float:
    try:
        return float(event.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _posts(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in list(summary.get("postconditions", []) or []) if isinstance(p, dict)]


def _posts_digest(posts: list[dict[str, Any]]) -> str:
    pairs = [[str(p.get("kind", "") or ""), str(p.get("value", "") or "").strip()] for p in posts]
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _condition_holds(post: dict[str, Any], title: str) -> bool:
    kind, value = str(post.get("kind", "") or ""), str(post.get("value", "") or "").strip()
    return kind in POSTCONDITION_KINDS and bool(value) and value in str(title or "")


def _gateway() -> ComputerUseGateway:
    global _gateway_ref, _nonce
    current = browser_bridge.gateway()
    if current is not _gateway_ref:
        _gateway_ref = current
        _nonce = secrets.token_hex(4)
        _sequences.clear()
    return current


def _lease(session_id: str) -> ControlLease:
    gateway = _gateway()
    try:
        return gateway.lease(session_id)
    except GatewayRefused:
        lease = ControlLease(lease_id=f"lease-{session_id}", session_id=session_id,
                             scope_digest=hashlib.sha256(f"desktop:{session_id}".encode("utf-8")).hexdigest(),
                             state=LeaseState.AGENT_OPERATING)
        gateway.open_session(lease)
        return lease


async def _view() -> HelperView | None:
    reader = getattr(_helper_objects()[0], "observe", None)
    if not callable(reader):
        return None
    try:
        view = reader()
        if inspect.isawaitable(view):
            view = await view
        if not channel_open():
            return None
    except Exception:  # noqa: BLE001 - a desktop that cannot be read is not acted on
        return None
    return view if isinstance(view, HelperView) else None


def _observe(task: Any, session_id: str, lease: ControlLease, view: HelperView) -> tuple[str, int, Any]:
    """The desktop as it is, hashed: what the next action binds to. Returns (observation_hash, sequence, geometry)."""
    gateway = _gateway()
    now = gateway._clock()
    # A coordinate is good for seconds from now, whatever the helper's own clock said (C30).
    # It is not extended past the frame the model was already shown.
    deadline = now + coordinate_ttl_sec()
    frame_deadline = _frame_coordinate_deadline(task)
    if frame_deadline is not None:
        deadline = min(deadline, frame_deadline)
    geometry = replace(view.geometry, expires_at=deadline)
    sequence = _sequences.get(session_id, 0) + 1
    _sequences[session_id] = sequence
    title = str(view.title or "")[:200]
    snapshot_hash = str(view.screen_hash or "") or hashlib.sha256(f"{view.app}\n{view.title}".encode("utf-8")).hexdigest()
    observation = Observation(
        session_id=session_id, task_id=str(getattr(task, "id", "") or ""), scope_digest=lease.scope_digest,
        sequence=sequence, url=f"desktop://{app_key(view.app)}", title=title, domain_allowed=True,
        snapshot_hash=snapshot_hash,
        controller=_controller(lease), lease_id=lease.lease_id, verification_state="none", produced_at=now, geometry=geometry,
    )
    _VIEW_PROJECTION[session_id] = {
        "window_title": title,
        "screen_hash": snapshot_hash[:16],
        "app": app_key(view.app),
    }
    return gateway.observe(observation), sequence, geometry


def _unresolved(session_id: str) -> bool:
    session = _gateway()._sessions.get(session_id)
    return bool(session is not None and (session.unresolved or session.cancelled_until_observed))


def pending_key(session_id: str) -> str:
    """Which unknown inputs a person is asked about, as a short mark ('' when none): the question carries it, and only
    an answer to a question with the mark the step has NOW settles them -- an answer about an earlier unknown input,
    or none at all, settles nothing. Action ids come back the same after a restart (the journal), so does the mark."""
    session = _gateway()._sessions.get(session_id)
    if session is None or not session.unresolved:
        return ""
    return hashlib.sha256("\n".join(sorted(session.unresolved)).encode("utf-8")).hexdigest()[:16]


def _events(session_id: str) -> list[dict[str, Any]]:
    """This session's Gateway events: the journal when there is one (it outlives a restart), else what is in memory."""
    gateway = _gateway()
    journal = getattr(gateway, "_journal", None)
    if journal is not None:
        return journal.events(session_id)
    session = gateway._sessions.get(session_id)
    return list(session.events) if session is not None else []


def action_log(session_id: str, since: float | None = None) -> list[dict[str, str]]:
    """What the step did on the desktop, in order: kind, outcome, reason. Never a coordinate's meaning or a typed text.
    With ``since``: only what was admitted at or after it (this turn)."""
    admitted: dict[str, str] = {}
    order: list[str] = []
    outcomes: dict[str, tuple[str, str]] = {}
    for event in _events(session_id):
        kind = event.get("type")
        attempt = str(event.get("attempt_id", "") or "")
        if kind == "admitted" and attempt and str(event.get("kind", "") or "") != "observe":
            if since is not None and _at(event) < since:
                continue
            admitted[attempt] = str(event.get("kind", "") or "")
            order.append(attempt)
        elif kind == "receipt" and attempt in admitted:
            outcomes[attempt] = (str(event.get("outcome", "") or ""), str(event.get("reason", "") or ""))
    # An admission with no receipt (the process ended in between) is unknown, never absent.
    return [{"kind": admitted[attempt], "outcome": outcomes.get(attempt, (Outcome.OUTCOME_UNKNOWN.value, ""))[0],
             "reason": outcomes.get(attempt, ("", "no_receipt"))[1]} for attempt in order]


def _did_work(session_id: str, since: float) -> bool:
    """Whether the step put anything on the desktop IN THIS TURN: a click or typing that reached it (a look, a scroll or
    a wait is not work). Without it a condition that holds proves nothing about this turn -- an earlier round's work
    on the same order and step is in the same session, and is not this round's."""
    return any(entry["kind"] in _WRITE_KINDS and entry["outcome"] in {Outcome.SUCCEEDED.value, Outcome.OUTCOME_UNKNOWN.value}
               for entry in action_log(session_id, since=since))


def _baseline(session_id: str, since: float, digest: str) -> list[int] | None:
    """Which done-when conditions (by position) already held before this turn's first action, or None when this turn
    recorded no baseline for these conditions."""
    for event in _events(session_id):
        if event.get("type") == _BASELINE and _at(event) >= since and event.get("conditions") == digest:
            return [int(i) for i in list(event.get("held", []) or []) if isinstance(i, int) and not isinstance(i, bool)]
    return None


async def observe(task: Any, summary: dict[str, Any], node_id: str, *, person_checked: bool = False,
                  answered: str = "") -> dict[str, Any]:
    """Look at the desktop. An unknown input of this step is settled as a person's finding only when ``person_checked``
    comes with ``answered`` -- the pending mark of the question a person answered 「我看过了」 to -- and that mark is
    the step's pending mark now. ``person_checked`` without such an answer is the model's word alone: the person is
    asked. Without ``person_checked`` the step's own look settles nothing."""
    stop = gate()
    if stop:
        return {"ok": False, "stop": stop}
    _note_turn_start(task)
    view = await _view()
    if view is None:
        identity = helper_refusal()
        if identity:
            return {"ok": False, "refused": identity}
        return {"ok": False, "refused": "desktop_unreadable"}
    if not app_allowed(view.app, list(summary.get("allowed_apps", []) or [])):
        # Which program is in front is said; what its window says is not -- it is not this step's to read.
        return {"ok": False, "refused": "app_not_allowed", "app": str(view.app or "")[:80]}
    session_id = session_for(task, node_id)
    lease = _lease(session_id)
    # A person is at the desktop. Record who holds it; do not admit an agent look.
    if lease.state not in AGENT_MAY_ACT and lease.state is not LeaseState.HUMAN_RETURNED:
        _observe(task, session_id, lease, view)
        return {"ok": False, "refused": "agent_not_in_control", "controller": _controller(lease),
                "state": lease.state.value, "app": str(view.app or "")[:80], "title": str(view.title or "")[:200]}
    promoted = False
    if lease.state is LeaseState.HUMAN_RETURNED:
        # This look is the one required after hand-back. Control returns only if it lands.
        _gateway().set_lease_state(session_id, LeaseState.AGENT_OPERATING, note="fresh_look")
        lease = _gateway().lease(session_id)
        promoted = True
    fresh = promoted or session_id in _FRESH_LOOK
    try:
        admitted = _observe_admitted(task, summary, node_id, session_id, lease, view, person_checked=person_checked, answered=answered)
    except Exception:
        if promoted:
            _gateway().set_lease_state(session_id, LeaseState.HUMAN_RETURNED, note="fresh_look_failed")
        raise
    if promoted and admitted.get("ok") is not True:
        _gateway().set_lease_state(session_id, LeaseState.HUMAN_RETURNED, note="fresh_look_failed")
    elif fresh and admitted.get("ok") is True:
        _FRESH_LOOK.discard(session_id)
        _release_runtime_input(task)
    return admitted


def _observe_admitted(task: Any, summary: dict[str, Any], node_id: str, session_id: str, lease: ControlLease,
                      view: HelperView, *, person_checked: bool, answered: str) -> dict[str, Any]:
    observation_hash, sequence, geometry = _observe(task, session_id, lease, view)
    # After the look: a cancelled turn's hold is lifted by a fresh observation alone; an unknown input is not.
    pending = _unresolved(session_id)
    mark = pending_key(session_id) if pending else ""
    confirmed = bool(pending and person_checked and mark and str(answered or "") == mark)
    ids = f"{session_id}:{_nonce}:{sequence}"
    try:
        admission = _gateway().submit(session_id, {
            "action_id": ids, "attempt_id": f"{ids}:1", "kind": "observe", "scope_digest": lease.scope_digest,
            "control_lease_id": lease.lease_id, "observation_hash": observation_hash, "target": {},
            "expected_effect": "look at the desktop", "risk": ActionClass.READ_ONLY.value, "timeout_sec": 10,
            "idempotent": True, "retryable": False,
        })
    except GatewayRefused as exc:
        return {"ok": False, "refused": exc.reason}
    if confirmed:
        verification = {"sources": ["human"], "checked": ["person_looked_at_the_desktop"], "holds": True}
    else:
        verification = {"sources": ["screenshot_region"], "checked": ["observation_after"], "holds": True}
    _gateway().record(session_id, ActionReceipt(admission.action.action_id, admission.action.attempt_id, Outcome.SUCCEEDED,
                                                evidence={"verification": verification}))
    cleared = False
    if confirmed:
        cleared = _gateway().facts_observed(session_id, admission.action.attempt_id)
    result: dict[str, Any] = {
        "ok": True, "app": str(view.app or "")[:80], "title": str(view.title or "")[:200],
        "size": [geometry.width, geometry.height], "controller": _controller(lease),
        "unknown_pending": pending and not cleared, "unknown_cleared_by_person": cleared,
    }
    if pending and person_checked and not cleared:
        # "A person looked" with no person's answer to this step's question about this input on record: ask them.
        result.update({"stop": STOP_UNKNOWN_OUTCOME, "pending": mark})
    return result


async def act(task: Any, summary: dict[str, Any], node_id: str, action: str) -> dict[str, Any]:
    """One action on the desktop, through the Gateway, or why not. Nothing here retries."""
    stop = gate()
    if stop:
        return {"ok": False, "stop": stop}
    _note_turn_start(task)
    blocked = input_block(task, node_id)
    if blocked:
        return {"ok": False, "refused": blocked}
    view = await _view()
    if view is None:
        identity = helper_refusal()
        if identity:
            return {"ok": False, "refused": identity}
        return {"ok": False, "refused": "desktop_unreadable"}
    if not app_allowed(view.app, list(summary.get("allowed_apps", []) or [])):
        return {"ok": False, "refused": "app_not_allowed", "app": str(view.app or "")[:80]}
    try:
        parsed = parse_prediction(str(action or ""), screen=(view.geometry.width, view.geometry.height))
    except ContractRefused as exc:
        return {"ok": False, "refused": exc.reason}
    if len(parsed) != 1:
        return {"ok": False, "refused": "one_action_at_a_time"}
    vision = parsed[0]
    if vision.function == "finished":
        return {"ok": False, "refused": "finished_is_not_an_action"}
    if vision.function == "call_user":
        return {"ok": False, "stop": STOP_PERSON_ASKED}
    if vision.function not in ACT_FUNCTIONS:
        return {"ok": False, "refused": "not_a_desktop_action", "function": vision.function}
    session_id = session_for(task, node_id)
    lease = _lease(session_id)
    gateway = _gateway()
    observation_hash, sequence, geometry = _observe(task, session_id, lease, view)
    since = turn_started(task)
    posts = _posts(summary)
    digest = _posts_digest(posts)
    if since is not None and _baseline(session_id, since, digest) is None:
        # The done-when conditions as the desktop showed them before this turn's first action: one that already held
        # then proves nothing about this turn's work (the browser's held_before_the_work).
        gateway.annotate(session_id, {"type": _BASELINE, "conditions": digest,
                                      "held": [i for i, post in enumerate(posts) if _condition_holds(post, str(view.title or ""))]})
    ids = f"{session_id}:{_nonce}:{sequence}"
    try:
        payload = to_computer_action(vision, action_id=ids, attempt_id=f"{ids}:1", scope_digest=lease.scope_digest,
                                     control_lease_id=lease.lease_id, observation_hash=observation_hash, geometry=geometry)
        admission = gateway.submit(session_id, payload)
    except ContractRefused as exc:
        return {"ok": False, "refused": exc.reason}
    except GatewayRefused as exc:
        if exc.reason in _LOCKING_REFUSALS:
            # An earlier input of this step ended unknown (in this process or before a restart): it is not sent
            # again, and nothing new is typed or clicked until a person has looked.
            return {"ok": False, "stop": STOP_UNKNOWN_OUTCOME, "refused": exc.reason, "pending": pending_key(session_id)}
        return {"ok": False, "refused": exc.reason}
    text_ref = ""
    helper, broker, texts = _helper_objects()
    if admission.action.kind == "type_draft" and vision.function == "type":
        text_ref = texts.deposit(str(vision.inputs.get("content", "") or ""))
    try:
        receipt = await run_through_helper(gateway, session_id, admission, helper, broker, text_ref=text_ref)
    except asyncio.CancelledError:
        # The turn was cancelled while the helper had the action: input may have reached the desktop. Cancelled
        # unknown locks the step's next write, as a cancelled browser turn does (gateway.cancel_in_flight).
        gateway.record(session_id, ActionReceipt(admission.action.action_id, admission.action.attempt_id,
                                                 Outcome.CANCELLED_UNKNOWN, reason="turn_cancelled"))
        raise
    except Exception:  # noqa: BLE001 - the helper may have sent input before it raised: unknown, never "did not happen"
        receipt = gateway.record(session_id, ActionReceipt(admission.action.action_id, admission.action.attempt_id,
                                                           Outcome.OUTCOME_UNKNOWN, reason="helper_raised",
                                                           evidence={"input_sent": "unknown"}))
    accepted = receipt.outcome is Outcome.SUCCEEDED or (
        receipt.outcome is Outcome.OUTCOME_UNKNOWN and receipt.reason == "input_accepted_unverified")
    result = {"ok": accepted, "kind": admission.action.kind, "outcome": receipt.outcome.value, "reason": receipt.reason,
              "app": str(view.app or "")[:80], "title": str(view.title or "")[:200]}
    if _unresolved(session_id):
        # This very input ended unknown (the helper lost track of it, the turn was cancelled under it): a person looks
        # now, before the model is tempted to do it again.
        result["stop"] = STOP_UNKNOWN_OUTCOME
        result["pending"] = pending_key(session_id)
    return result


async def finish(task: Any, summary: dict[str, Any], node_id: str) -> dict[str, Any]:
    """The done check: code reads the desktop against the step's done-when conditions. The model saying done is not done.

    Only this turn counts. The session is per order and step, so a rework round (another task, or the same one run
    again) finds the earlier round's clicks and window in it: the check counts the actions made since the kernel
    stamped this turn, and needs a condition that holds now and did not before this turn's first action."""
    stop = gate()
    if stop:
        return {"ok": False, "stop": stop}
    session_id = session_for(task, node_id)
    _lease(session_id)
    since = turn_started(task)
    log = action_log(session_id, since=since)
    base = {"ok": True, "log": log}
    posts = _posts(summary)
    digest = _posts_digest(posts)

    def unverified(reason: str, **extra: Any) -> dict[str, Any]:
        # Every ending of the check is journaled, not only the ones that read the desktop: the turn's end
        # (done_standing) says why this turn's check did not stand, not merely that there was none.
        _note_done_check(session_id, reason, digest, conditions=len(posts), app=str(extra.get("app", "") or ""))
        return {**base, "status": "done_unverified", "reason": reason, **extra}

    if _unresolved(session_id):
        return unverified("unresolved_outcome")
    if since is None:
        # No turn stamp: nothing shows which actions are this turn's, so none is counted -- stricter, never looser.
        return unverified("not_this_turn")
    if not _did_work(session_id, since):
        return unverified("no_work")
    view = await _view()
    if view is None:
        return unverified("desktop_unreadable")
    if not app_allowed(view.app, list(summary.get("allowed_apps", []) or [])):
        return unverified("app_not_allowed", app=str(view.app or "")[:80])
    if not posts:
        return unverified("no_postconditions")
    title = str(view.title or "")
    before = _baseline(session_id, since, digest)
    checks: list[dict[str, Any]] = []
    for index, post in enumerate(posts):
        holds = _condition_holds(post, title)
        # No baseline recorded this turn: the desktop as it is now is the baseline, so nothing on it counts as new.
        checks.append({"kind": str(post.get("kind", "") or ""), "value": str(post.get("value", "") or ""), "holds": holds,
                       "held_before": holds if before is None else index in before})
    if not all(check["holds"] for check in checks):
        reason = "condition_not_met"
    elif not any(check["holds"] and not check["held_before"] for check in checks):
        reason = "held_before_the_work"
    else:
        reason = ""
    _note_done_check(session_id, reason, digest, conditions=len(checks), app=str(view.app or ""))
    return {**base, "status": "done_unverified" if reason else "done_verified", "reason": reason,
            "checks": checks, "app": str(view.app or "")[:80], "title": title[:200]}


def _note_done_check(session_id: str, reason: str, digest: str, *, conditions: int, app: str = "") -> None:
    """One ending of the done check, journaled: whether it held ('' reason), on which conditions (their digest), and
    for which program -- a count and a program's name, never a window's text."""
    _gateway().annotate(session_id, {"type": _DONE_CHECK, "holds": not reason, "reason": reason, "conditions": conditions,
                                     "digest": digest, **({"app": app_key(app)[:80]} if app else {})})


def done_standing(task: Any, summary: dict[str, Any], node_id: str) -> tuple[bool, str]:
    """At the end of a turn: whether this turn's done check on the desktop still stands, and why not ('' when it does).

    What the kernel asks before a desktop step's DONE may complete it (connector_done_wait.as_pause): the model's
    saying so, or a deliverable it wrote with an ordinary file tool, is not a desktop check. It stands only when the
    last check made in THIS turn held, on the conditions the step has now, and nothing was put on the desktop after it
    (an admitted click, typing or scroll) and no input of the step is unknown. Reads the journal only -- it opens no
    session and touches no desktop. ``no_check``: no check was made in this turn at all."""
    try:
        since = turn_started(task)
        session_id = session_for(task, node_id)
        events = _events(session_id)
    except Exception:  # noqa: BLE001 - a record that cannot be read proves nothing: it does not stand
        return False, "no_check"
    if since is None:
        return False, "not_this_turn" if any(event.get("type") == _DONE_CHECK for event in events) else "no_check"
    last = None
    for index, event in enumerate(events):
        if event.get("type") == _DONE_CHECK and _at(event) >= since:
            last = index
    if last is None:
        return False, "no_check"
    check = events[last]
    if not check.get("holds"):
        return False, str(check.get("reason", "") or "condition_not_met")
    if str(check.get("digest", "") or "") != _posts_digest(_posts(summary)):
        return False, "changed_after_check"
    if any(event.get("type") == "admitted" and str(event.get("kind", "") or "") != "observe" for event in events[last + 1:]):
        return False, "changed_after_check"
    try:
        if _unresolved(session_id):
            return False, "unresolved_outcome"
    except Exception:  # noqa: BLE001
        return False, "unresolved_outcome"
    return True, ""


def note_decision(task: Any, node_id: str, fields: dict[str, Any], *, card: str = "") -> bool:
    """A person's decision on a desktop step, in the step's own decision chain (the Gateway session the audit view
    reads, ampuly/computer_use/audit.py): ``fields`` are the chain's vocabulary (outcome, reason, suggested_by,
    reviewed_by, fallback_to, action). With ``card``, once per card: a run interrupted before its commit reads the
    same answer again. Ids and vocabulary only; False when it could not be written -- never raises."""
    try:
        session_id = session_for(task, node_id)
        if card and any(event.get("type") == "decision_trail" and event.get("card") == card
                        for event in _events(session_id)):
            return True
        _lease(session_id)
        event = {"type": "decision_trail", **{k: v for k, v in dict(fields).items() if v not in (None, "")},
                 **({"card": card} if card else {})}
        return bool(_gateway().annotate(session_id, event))
    except Exception:  # noqa: BLE001 - the decision stands whether or not it could be journaled
        return False
