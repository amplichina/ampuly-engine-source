"""What a console conversation is bound to while it executes.

The master-control conversation has never had an identity. It is addressed as
the chat channel ``master:{project_id}`` and nothing else: no session id, no
run id, no per-user state. History survives a refresh because chat rows do;
the turn survives nothing, because a turn is a bare asyncio task with no
durable record. Two owners on one project share the same conversation, and a
socket drop orphans whatever was running.

That is the gap this module closes, and it closes it by writing into stores
that already exist rather than adding another one. A console conversation is a
``SessionRecord`` -- the shape the Secretary page already uses -- and its
binding to an execution session lives in that record's metadata. The plan's
rule for this is explicit: a new binding is written into the existing
authoritative store, and cross-domain notification goes through the outbox
rather than keeping two copies of "current state".

The vocabulary here is the plan's, mapped onto what the product already owns:

    ConsoleSession     the conversation. A SessionRecord, owned by the store.
    ExecutionBinding   which execution session that conversation is driving.
                       Owned by the same record; the Harness registry remains
                       the authority on the execution session itself.

Three rules this module exists to hold:

**Identity is server-side.** A project id arriving on the wire is a request,
not a fact. Callers resolve scope through the handler's own authority and pass
the result in; nothing here reads a client field.

**The binding names a session, it does not own one.** A provider fingerprint
is recorded so a stale binding can be recognised after the executor changes,
but the Harness registry stays the authority on whether that session is alive.
Asking here whether a session can take a turn would be a second answer to a
question that already has one.

**A binding is per conversation, never per socket.** Every per-websocket map
in the console is cleared when the socket closes, and the master page is the
one page whose work is expected to outlive the tab. Keying on the socket would
look right in manual testing and evaporate on refresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The metadata key that marks which product surface a SessionRecord belongs to.
# The Secretary page established it; master control now uses the same one so a
# session can be found again after a restart without a second index.
INTERFACE_KEY = "interface"

MASTER_CONTROL_INTERFACE = "master_control"

# Where the execution binding lives inside that record.
_BINDING_KEY = "execution_binding"


@dataclass(frozen=True)
class ConsoleSession:
    """A console conversation that can drive execution.

    ``customer_id`` and ``environment_id`` are the resolved scope, not what a
    client asked for. They are carried because a bound execution session must
    be opened inside the same scope, and because switching customers has to
    establish a new context rather than inherit the previous one's.
    """

    session_id: str
    project_id: str
    interface: str = MASTER_CONTROL_INTERFACE
    customer_id: str = ""
    environment_id: str = ""
    title: str = ""

    def is_master_control(self) -> bool:
        return self.interface == MASTER_CONTROL_INTERFACE


@dataclass(frozen=True)
class ExecutionBinding:
    """Which execution session a console conversation is currently driving.

    ``provider`` is a fingerprint, not a handle. When the executor behind a
    conversation changes, an old binding must be recognisable as belonging to
    the previous one instead of being silently reused -- that is the whole
    reason the field is here.

    ``task_id`` / ``run_id`` / ``order_id`` are optional on purpose. A console
    conversation legitimately has none of them: a question about the system is
    not a work order, and the plan says a query may exist without one.
    """

    console_session_id: str
    harness_session_id: str
    provider: str = ""
    task_id: str = ""
    run_id: str = ""
    order_id: str = ""
    bound_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        payload = {
            "harness_session_id": self.harness_session_id,
            "provider": self.provider,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "order_id": self.order_id,
            "bound_at": self.bound_at,
        }
        payload.update(dict(self.extra or {}))
        return {key: value for key, value in payload.items() if value not in ("", None)}

    @classmethod
    def from_metadata(cls, console_session_id: str, payload: Any) -> ExecutionBinding | None:
        """Read a binding back, or None when there is not one.

        A record with no binding and a record whose binding is malformed are
        the same answer here: nothing is bound. Returning a half-built binding
        would hand the caller a session id that may name nothing.
        """
        if not isinstance(payload, dict):
            return None
        harness_session_id = str(payload.get("harness_session_id", "") or "").strip()
        if not harness_session_id:
            return None
        known = {"harness_session_id", "provider", "task_id", "run_id", "order_id", "bound_at"}
        return cls(
            console_session_id=str(console_session_id or ""),
            harness_session_id=harness_session_id,
            provider=str(payload.get("provider", "") or ""),
            task_id=str(payload.get("task_id", "") or ""),
            run_id=str(payload.get("run_id", "") or ""),
            order_id=str(payload.get("order_id", "") or ""),
            bound_at=str(payload.get("bound_at", "") or ""),
            extra={k: v for k, v in payload.items() if k not in known},
        )

    def belongs_to(self, provider: str) -> bool:
        """Whether this binding was made by the executor now asking about it.

        An unrecorded provider matches nothing. A binding whose origin was
        never written down cannot be shown to belong to the current executor,
        and guessing that it does is how one executor picks up another's
        session.
        """
        mine = str(provider or "").strip()
        return bool(self.provider) and bool(mine) and self.provider == mine


def console_session_from_record(record: Any, *, customer_id: str = "", environment_id: str = "") -> ConsoleSession:
    """Read a stored session row as a console conversation."""
    metadata = dict(getattr(record, "metadata", {}) or {})
    return ConsoleSession(
        session_id=str(getattr(record, "session_id", "") or ""),
        project_id=str(getattr(record, "project_id", "") or "default"),
        interface=str(metadata.get(INTERFACE_KEY, "") or ""),
        customer_id=customer_id,
        environment_id=environment_id,
        title=str(getattr(record, "title", "") or ""),
    )


def binding_of(record: Any) -> ExecutionBinding | None:
    """The execution binding carried by a stored session row, if any."""
    metadata = dict(getattr(record, "metadata", {}) or {})
    return ExecutionBinding.from_metadata(
        str(getattr(record, "session_id", "") or ""),
        metadata.get(_BINDING_KEY),
    )


def with_binding(metadata: dict[str, Any] | None, binding: ExecutionBinding | None) -> dict[str, Any]:
    """The session metadata to store, with the binding set or removed.

    Everything else in the record is carried through untouched. A session row
    is not this module's to own -- it belongs to whichever surface created it,
    and rewriting the rest of it here would make a binding change look like an
    edit to the conversation.
    """
    payload = dict(metadata or {})
    if binding is None:
        payload.pop(_BINDING_KEY, None)
        return payload
    payload[_BINDING_KEY] = binding.to_metadata()
    return payload


def is_master_control_record(record: Any) -> bool:
    """Whether a stored session row is the master-control conversation.

    Used to find the conversation again after a restart. It is deliberately a
    metadata match and not a title match: a title is copy and can be
    translated or renamed, and a conversation that cannot be found again is a
    conversation that silently forks into a second one.
    """
    metadata = dict(getattr(record, "metadata", {}) or {})
    if str(metadata.get(INTERFACE_KEY, "") or "") != MASTER_CONTROL_INTERFACE:
        return False
    return not getattr(record, "parent_session_id", None)
