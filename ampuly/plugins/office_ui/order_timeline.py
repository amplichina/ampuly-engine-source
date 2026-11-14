"""One order's story, from the business record, the runtime and the outside world.

The business half (``order_events`` / ``gate_events``) lives in
``business.db`` with a signed envelope and a hash chain; the runtime half
(``delegation_events``) lives in the engine store keyed by ``run_id``; and
what actually left the machine -- a message into a customer group, any other
external effect -- lives in the effect journal beside the runtime.  Each was
readable on its own, which meant nobody could answer "what happened to this
order" without joining three databases by hand.

This projection does that join and says which half each entry came from.
Business entries carry their chain verification; runtime entries do not
claim one, because the runtime store has no chain — labelling them the same
would overstate the evidence.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, tzinfo
from typing import Any

# The runtime's own vocabulary, rendered as plain business language.  An
# unknown type is shown verbatim rather than hidden.
_RUNTIME_LABELS = {
    "work_item_created": "派出工作项",
    "delivery_work_item_created": "派出交付工作项",
    "work_item_claimed": "认领工作项",
    "work_item_completed": "工作项完成",
    "work_item_failed": "工作项失败",
    "report_work_item_created": "派出汇报回合",
    "review_work_item_created": "派出评审回合",
    "gate_published": "发布人工闸门",
    "gate_decided": "闸门已决策",
    # runtime contract: the owner's answer reached its item although its card was lost.
    "owner_answer_kept_card_lost": "这次回答的卡丢了，按已收到的回答执行",
}


# What an effect's life looks like in business language.  Effects are how
# anything reaches the outside world -- a channel message is one of them --
# so this is the part of the timeline a customer dispute turns on.
_EFFECT_LABELS = {
    "effect_proposed": "提出外部动作",
    "effect_approval_requested": "请求批准外部动作",
    "effect_authorized": "外部动作已批准",
    "effect_rejected": "外部动作被拒绝",
    "effect_claimed": "开始执行外部动作",
    "effect_applied": "外部动作已执行",
    "effect_failed": "外部动作失败",
    "effect_outcome_unknown": "外部动作结果不明",
    "effect_simulated": "外部动作已模拟",
    "effect_verified": "外部动作已核验",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(str(value or "") or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# 4.7-AR: the sources stamp time three ways -- the business store and the runtime write the machine's local wall
# clock with no zone, the effect journal writes UTC with its offset, the browser journal writes epoch seconds. Compared
# as text, a submit's UTC stamp sorted about eight hours before the browser decision that led to it (UTC+8). Every
# ``at`` is therefore one instant said with its offset, in the zone the console runs in (the owner's machine), and the
# order is by that instant. A zoneless stamp was written on this machine, so it is read in this machine's zone.
# ``None`` means the system's own rules for each date; a test sets a fixed zone.
_LOCAL_ZONE: tzinfo | None = None


def _instant(value: Any) -> datetime | None:
    """The moment a stamp names -- epoch seconds, a datetime, or ISO text with or without an offset -- or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        try:
            moment = moment.replace(tzinfo=_LOCAL_ZONE) if _LOCAL_ZONE is not None else moment.astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    return moment


def _stamp(value: Any) -> str:
    """``at`` for an entry: ISO 8601 with its offset, in the console's own zone. Text that names no moment is kept."""
    moment = _instant(value)
    if moment is None:
        return str(value or "")
    try:
        return (moment.astimezone(_LOCAL_ZONE) if _LOCAL_ZONE is not None else moment.astimezone()).isoformat()
    except (OverflowError, OSError, ValueError):
        return moment.isoformat()


def _entry(*, at: Any, source: str, kind: str, actor: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {
        "at": _stamp(at),
        "source": source,
        "kind": str(kind or ""),
        "actor": str(actor or ""),
        "detail": str(detail or "")[:400],
        **{k: v for k, v in extra.items() if v not in (None, "")},
    }


def business_entries(business: Any, order_id: str) -> list[dict[str, Any]]:
    """``order_events`` plus the ``gate_events`` of that order's gates."""
    entries: list[dict[str, Any]] = []
    chain_ok = True
    try:
        chain_ok = bool(business.verify_order_event_chain(order_id))
    except Exception:
        chain_ok = False
    for source_sequence, event in enumerate(list(business.events(order_id) or []), start=1):
        note = _as_dict(event.get("note"))
        entries.append(_entry(
            at=event.get("at", ""),
            source="order",
            source_sequence=source_sequence,
            kind=event.get("kind", ""),
            actor=event.get("actor", ""),
            detail=str(note.get("note") or note.get("reason") or note.get("status") or ""),
            event_id=event.get("event_id", ""),
            correlation_id=event.get("correlation_id", ""),
            chain_verified=chain_ok,
        ))
    reader = getattr(business, "gate_cards_for_order", None)
    for card in list((reader(order_id) if callable(reader) else []) or []):
        checkpoint_id = str(card.get("checkpoint_id", "") or "")
        if not checkpoint_id:
            continue
        try:
            gate_chain_ok = bool(business.verify_gate_event_chain(checkpoint_id))
        except Exception:
            gate_chain_ok = False
        for event in list(business.gate_events(checkpoint_id) or []):
            note = _as_dict(event.get("note"))
            entries.append(_entry(
                at=event.get("at", ""),
                source="gate",
                kind=event.get("kind", ""),
                actor=event.get("actor", ""),
                detail=str(note.get("note") or note.get("channel") or ""),
                event_id=event.get("event_id", ""),
                checkpoint_id=checkpoint_id,
                chain_verified=gate_chain_ok,
            ))
    return entries


async def runtime_entries(store: Any, run_ids: list[str]) -> list[dict[str, Any]]:
    """``delegation_events`` for the runs this order launched."""
    entries: list[dict[str, Any]] = []
    reader = getattr(store, "list_delegation_events", None)
    if not callable(reader):
        return entries
    for run_id in [str(r or "").strip() for r in run_ids if str(r or "").strip()]:
        try:
            rows = await reader(run_id)
        except Exception:
            continue
        for row in list(rows or []):
            # ``list_delegation_events`` returns DelegationEvent objects; a
            # dict is accepted too so tests can hand in plain rows.
            def field(name: str) -> Any:
                if isinstance(row, dict):
                    return row.get(name, "")
                return getattr(row, name, "")

            event_type = str(field("event_type") or "")
            created = field("created_at")
            entries.append(_entry(
                at=created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
                source="runtime",
                kind=event_type,
                actor=str(field("role_id") or ""),
                detail=_RUNTIME_LABELS.get(event_type, event_type),
                event_id=str(field("event_id") or ""),
                run_id=run_id,
                work_item_id=str(field("work_item_id") or ""),
            ))
    return entries


def effect_entries(db_path: Any, run_ids: list[str], order_id: str) -> list[dict[str, Any]]:
    """What this order sent outside, from the effect journal.

    Read-only and best-effort: the journal shares the runtime database file,
    so this opens its own connection rather than reaching into the engine's.
    A database without the tables (nothing has ever been sent) yields no
    entries rather than an error.  Effects are matched by the run that raised
    them and by an explicit correlation with the order.
    """
    path = str(db_path or "").strip()
    if not path:
        return []
    runs = [str(r or "").strip() for r in run_ids if str(r or "").strip()]
    order = str(order_id or "").strip()
    if not runs and not order:
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    entries: list[dict[str, Any]] = []
    try:
        connection.row_factory = sqlite3.Row
        clauses, params = [], []
        if runs:
            clauses.append(f"run_id IN ({','.join('?' for _ in runs)})")
            params.extend(runs)
        if order:
            clauses.append("correlation_id = ?")
            params.append(order)
        effects = connection.execute(
            f"SELECT effect_id, provider, kind, state, run_id, customer_id FROM effects WHERE {' OR '.join(clauses)}",
            params,
        ).fetchall()
        if not effects:
            return []
        by_id = {str(row["effect_id"]): row for row in effects}
        ids = list(by_id)
        rows = connection.execute(
            f"SELECT effect_id, event_id, event_type, to_state, created_at FROM effect_events "
            f"WHERE effect_id IN ({','.join('?' for _ in ids)}) ORDER BY sequence",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    for row in rows:
        effect = by_id.get(str(row["effect_id"]))
        if effect is None:
            continue
        event_type = str(row["event_type"] or "")
        provider = str(effect["provider"] or "")
        kind = str(effect["kind"] or "")
        label = _EFFECT_LABELS.get(event_type, event_type)
        entries.append(_entry(
            at=str(row["created_at"] or ""),
            source="effect",
            kind=event_type,
            actor=provider,
            detail=f"{label} · {kind}" if kind else label,
            event_id=str(row["event_id"] or ""),
            effect_id=str(row["effect_id"] or ""),
            run_id=str(effect["run_id"] or ""),
            state=str(row["to_state"] or effect["state"] or ""),
        ))
    return entries


async def run_ids_for_order(store: Any, launched_task_id: str) -> list[str]:
    """The delegation runs behind an order: the root task's session's children."""
    task_id = str(launched_task_id or "").strip()
    if not task_id:
        return []
    get_task = getattr(store, "get_task", None)
    by_session = getattr(store, "get_tasks_by_session_id", None)
    if not callable(get_task) or not callable(by_session):
        return []
    try:
        root = await get_task(task_id)
    except Exception:
        return []
    session_id = str(getattr(root, "session_id", "") or "").strip()
    if not session_id:
        return []
    found: list[str] = []
    try:
        siblings = list(await by_session(session_id) or [])
    except Exception:
        siblings = []
    candidates = list(siblings)
    # Work items live in child sessions (``<root>:<suffix>``) and carry the
    # run id; the root task never does.
    listing = getattr(store, "get_tasks", None)
    if callable(listing):
        project_id = str(getattr(root, "project_id", "") or "") or None
        try:
            for task in list(await listing(project_id=project_id) or []):
                parent = str(getattr(task, "parent_session_id", "") or "")
                own = str(getattr(task, "session_id", "") or "")
                if parent == session_id or own.startswith(session_id + ":"):
                    candidates.append(task)
        except Exception:
            pass
    for task in candidates:
        run_id = str((dict(getattr(task, "metadata", {}) or {})).get("delegation_run_id", "") or "").strip()
        if run_id and run_id not in found:
            found.append(run_id)
    return found


def work_items_of_root(tasks: list[Any], root: Any) -> list[Any]:
    """The work items behind a root task, among tasks already listed: those of the root's session (the root among
    them) and of its child sessions (``<root>:<suffix>``, as above). The rule without the reads, so the order list can
    read a project's tasks once and ask it for each order (S0-B); ``_order_work_items`` is the same rule for one order."""
    session_id = str(getattr(root, "session_id", "") or "").strip()
    if not session_id:
        return []
    return [task for task in tasks
            if str(getattr(task, "parent_session_id", "") or "") == session_id
            or str(getattr(task, "session_id", "") or "") == session_id
            or str(getattr(task, "session_id", "") or "").startswith(session_id + ":")]


async def _order_work_items(store: Any, launched_task_id: str) -> list[Any]:
    """The work items behind an order: the tasks of the root's session and of its child sessions (as above)."""
    task_id = str(launched_task_id or "").strip()
    get_task = getattr(store, "get_task", None)
    listing = getattr(store, "get_tasks", None)
    if not task_id or not callable(get_task) or not callable(listing):
        return []
    try:
        root = await get_task(task_id)
    except Exception:
        return []
    session_id = str(getattr(root, "session_id", "") or "").strip()
    if not session_id:
        return []
    project_id = str(getattr(root, "project_id", "") or "") or None
    try:
        tasks = list(await listing(project_id=project_id) or [])
    except Exception:
        return []
    return work_items_of_root(tasks, root)


async def browser_entries(store: Any, launched_task_id: str) -> list[dict[str, Any]]:
    """4.7-A17: each browser step's decisions -- who suggested, who checked, who has it now -- said in words.

    Read from the browser journal of each work item that carries the browser connector. Read-only and best-effort
    like the rest of this module: a step without a browser, or a journal that cannot be read, adds nothing. The
    journal is append-only and unchained, like the runtime half; only vocabulary ids and the words made from them
    reach the entry, never a page's text.
    """
    connector_items = []
    for task in await _order_work_items(store, launched_task_id):
        node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node")
        if isinstance(node, dict) and isinstance(node.get("connector"), dict):
            connector_items.append(task)
    if not connector_items:
        return []
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.audit import PROVENANCE_KEYS, provenance_text
    from ampuly.layer4_tools.browser_sessions import BrowserBinding

    entries: list[dict[str, Any]] = []
    for task in connector_items:
        try:
            events = browser_bridge.events_for(BrowserBinding.for_task(task))
        except Exception:
            continue
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "decision_trail":
                continue
            # Epoch seconds, said by _entry in the same zone as every other source (4.7-AR).
            try:
                at: Any = float(event.get("at"))
            except (TypeError, ValueError):
                at = ""
            fields = {key: str(event.get(key, "") or "") for key in PROVENANCE_KEYS}
            entries.append(_entry(
                at=at, source="browser", kind="browser.decision", actor="",
                detail=provenance_text({**fields, "outcome": event.get("outcome", ""), "reason": event.get("reason", ""),
                                        "function": event.get("function", "")}),
                work_item_id=str(getattr(task, "id", "") or ""), **fields,
            ))
    return entries


class _TasksListedOnce:
    """The store as one timeline request sees it: the project's task listing is read once and shared by the run ids
    and the browser steps (each would list it again); everything else is the store's own."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._listed: dict[str, list[Any]] = {}

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._store, name)
        if name != "get_tasks" or not callable(value):
            return value

        async def listed(*args: Any, **kwargs: Any) -> list[Any]:
            key = repr((args, sorted(kwargs.items())))
            if key not in self._listed:
                self._listed[key] = list(await value(*args, **kwargs) or [])
            return list(self._listed[key])

        return listed


from ampuly.plugins.office_ui.event_integrity import verify_streams


def _order_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """By the real instant (4.7-AR), never by the text of the stamp; an entry whose time cannot be read goes last."""
    moment = _instant(item.get("at", ""))
    return (
        0 if moment is not None else 1, moment.timestamp() if moment is not None else 0.0, str(item.get("at", "")),
        str(item.get("source", "")), int(item.get("source_sequence", 0)), str(item.get("event_id", "")),
    )


def merge(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Time order, with a stable tie-break so equal stamps never shuffle."""
    return sorted(entries, key=_order_key)


async def build_order_timeline(*, business: Any, store: Any, order: dict[str, Any]) -> dict[str, Any]:
    """The merged timeline plus a short summary of what it is made of."""
    order_id = str(order.get("order_id", "") or "")
    entries = business_entries(business, order_id)
    listed = _TasksListedOnce(store)
    runs = await run_ids_for_order(listed, str(order.get("launched_task_id", "") or ""))
    entries.extend(await runtime_entries(store, runs))
    entries.extend(effect_entries(getattr(store, "db_path", ""), runs, order_id))
    entries.extend(await browser_entries(listed, str(order.get("launched_task_id", "") or "")))
    merged = merge(entries)
    return {
        "order_id": order_id,
        "customer_id": str(order.get("customer_id", "") or ""),
        "run_ids": runs,
        "entries": merged,
        "counts": {
            "order": sum(1 for e in merged if e["source"] == "order"),
            "gate": sum(1 for e in merged if e["source"] == "gate"),
            "runtime": sum(1 for e in merged if e["source"] == "runtime"),
            "effect": sum(1 for e in merged if e["source"] == "effect"),
            "browser": sum(1 for e in merged if e["source"] == "browser"),
        },
        # Only the business half is chained; say so rather than implying the
        # whole timeline is tamper-evident.
        "business_chain_verified": all(e.get("chain_verified", True) for e in merged if e["source"] in {"order", "gate"}),
        "runtime_chain_available": False,
        # The effect journal is append-only but unchained, like the runtime
        # half; it is evidence of what was sent, not proof of what was not.
        "effect_chain_available": False,
        # Stream by stream, for a reader who wants to know which parts of this
        # history could be proven and which are simply what we wrote down.
        "integrity": verify_streams(
            business,
            order_ids=(order_id,),
            checkpoint_ids=tuple(
                str(e.get("checkpoint_id", "") or "") for e in merged if e.get("checkpoint_id")
            ),
        ).to_payload(),
    }
