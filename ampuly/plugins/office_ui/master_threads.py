"""总控对话栏 (policy; the module contract 第七节): many 总控 conversations per project.

owner: 「为总控在左侧增加一个跟 Claude Code 或 Codex 相似的项目管理栏，这样所有的对话都可以跟项目可靠对应，可靠管理，
可靠持续了。」

A conversation ("thread") has three parts, each kept where the product already keeps that kind of thing:

* **its identity** is one master-control ``SessionRecord`` in the project's runtime store -- the record a turn's
  execution binding is written on, whose id is the console session id a turn's memory (B0) and a build job (B1) are
  keyed by. The thread id IS that session id. A project may have many.
* **its words** are chat rows in the console's ui_state.db, in one channel (``master_control.master_channel_id``).
* **its entry in the column** -- title, when it began, when it was last active, archived or not, first or later -- is
  one row of ``master_threads`` in the same ui_state.db (``ChatStore``): carried by a backup, put back by a restore,
  moved by a rename and deleted with the project exactly as the project's chat rows are.

Why the entry is not kept in the session record's metadata, which the kernel store could hold: the column lists the
conversations of every project, and reading them there would open every project's runtime store; a title or an archive
written there would be a whole-row replace racing the execution binding written on the same row; and the store's
get-or-create answers with the most recently touched master session once there are several, so the store alone cannot
say which conversation is the first.

**The first conversation.** Every project's existing master session and its channel ``master:{pid}`` ARE its first
thread, titled 「以前的对话」, never moved or copied. It is registered lazily -- the first time the project's threads
are listed (only when there is an old conversation to show) or the first time a message is sent without naming a
thread (today's client). A thread made by ``master_thread_create`` is marked in its session record
(``master_thread: "thread"``), so the first conversation is never mistaken for a later one, even where the index
cannot be read.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from ampuly.application.execution_binding import INTERFACE_KEY, MASTER_CONTROL_INTERFACE, is_master_control_record

LEGACY_TITLE_ZH = "以前的对话"
TITLE_MAX_CHARS = 60
FIRST_WORDS_CHARS = 24
# ``master_thread_list`` without a project: the newest this many of each project (archived ones included, flagged).
LIST_PER_PROJECT = 50
# ``master_thread_list`` for one project: all of its conversations, bounded.
LIST_ONE_PROJECT = 500
# How many of a project's master sessions are looked through for its first one (they are few: one per conversation).
SESSIONS_LOOKED_AT = 500

# In a later conversation's session record: what marks it as not the first one.
THREAD_MARKER_KEY = "master_thread"
THREAD_MARKER = "thread"

# A session id (a uuid) and nothing that could reach into a channel name: no colon, no space, bounded.
_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ThreadRefused(ValueError):
    """A thread request that is refused, with the stable code the ack carries as ``error``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def valid_thread_id(value: Any) -> bool:
    return isinstance(value, str) and _THREAD_ID.fullmatch(value) is not None


def _plain(text: Any) -> str:
    """One line of plain text: every run of whitespace one space, control and format characters dropped."""
    raw = "".join(" " if ch.isspace() else ch for ch in str(text or ""))
    kept = "".join(ch for ch in raw if unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co"})
    return " ".join(kept.split())


def clean_title(raw: Any) -> str:
    """A title the owner typed: plain text, 1 to 60 characters once tidied. Raises ``ThreadRefused("title_invalid")``
    for anything else (not text, empty once tidied, longer than 60)."""
    if not isinstance(raw, str):
        raise ThreadRefused("title_invalid")
    title = _plain(raw)
    if not title or len(title) > TITLE_MAX_CHARS:
        raise ThreadRefused("title_invalid")
    return title


def first_words(content: Any) -> str:
    """The title a conversation takes from its first owner message: that message, tidied, cut to 24 characters."""
    return _plain(content)[:FIRST_WORDS_CHARS].rstrip()


def is_later_thread_record(record: Any) -> bool:
    """Whether a master session was made as a later conversation (never the project's first one)."""
    metadata = dict(getattr(record, "metadata", {}) or {})
    return str(metadata.get(THREAD_MARKER_KEY, "") or "") == THREAD_MARKER


def new_thread_record(project_id: str, *, session_id: str = "", title: str = "总控") -> Any:
    """The session record of a later conversation: a root master-control session, marked as a later one."""
    from ampuly.core.models import SessionRecord

    fields: dict[str, Any] = {
        "project_id": project_id,
        "title": title,
        "metadata": {INTERFACE_KEY: MASTER_CONTROL_INTERFACE, THREAD_MARKER_KEY: THREAD_MARKER},
    }
    if session_id:
        fields["session_id"] = session_id
    return SessionRecord(**fields)


def first_thread_record(records: Any) -> Any | None:
    """The project's first conversation among its master sessions: the oldest root one not made as a later one."""
    candidates = [
        record for record in list(records or [])
        if is_master_control_record(record) and not is_later_thread_record(record)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda record: (str(getattr(record, "created_at", "") or ""),
                                               str(getattr(record, "session_id", "") or "")))


async def first_thread_session(store: Any, project_id: str) -> str:
    """The first conversation's session id in a project's runtime store, found and never made; "" when none."""
    rows = await store.list_sessions(project_id=project_id, interface=MASTER_CONTROL_INTERFACE,
                                     limit=SESSIONS_LOOKED_AT)
    found = first_thread_record(rows)
    return str(getattr(found, "session_id", "") or "") if found is not None else ""


def first_thread_session_readonly(db_path: Path | str, project_id: str) -> str | None:
    """The same, read in the store file of a project whose engine this process does not run (``mode=ro``, closed
    after -- the pattern of ``order_attention.pending_checkpoints_readonly``): a listing never loads a project.
    "" when there is none; None when the file cannot be read (not known -- never taken for "none")."""
    import json

    from ampuly.core.sqlite_open import opened

    try:
        with opened(db_path, read_only=True, timeout=2.0) as conn:
            rows = conn.execute(
                "SELECT session_id, metadata FROM sessions WHERE project_id = ? AND parent_session_id IS NULL "
                "AND json_extract(CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END, '$.interface') = ? "
                "ORDER BY created_at ASC, session_id ASC LIMIT ?",
                (project_id, MASTER_CONTROL_INTERFACE, SESSIONS_LOOKED_AT),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return None
    for session_id, raw in rows:
        try:
            metadata = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            metadata = {}
        if isinstance(metadata, dict) and str(metadata.get(THREAD_MARKER_KEY, "") or "") == THREAD_MARKER:
            continue
        return str(session_id or "")
    return ""


def build_flags(jobs: Any, *, first_thread_id: str, holds: Any) -> dict[str, dict[str, bool]]:
    """Per conversation: a build of it is working now (``running_build``) or waits for the owner (``waiting_owner``).

    From build mode's job records: a job belongs to the conversation that started it (``thread_id``; a job recorded
    before conversations were split, with none, is the first conversation's). Only a job this console runs counts --
    one a stopped console left marked live is not running (``MasterBuildRunner.describe`` says the same).
    """
    flags: dict[str, dict[str, bool]] = {}
    for job in list(jobs or []):
        state = str(getattr(job, "state", "") or "")
        if state not in {"running", "waiting_owner"}:
            continue
        try:
            live = bool(holds(str(getattr(job, "job_id", "") or "")))
        except Exception:  # noqa: BLE001 - a runner that cannot say is taken as not running
            live = False
        if not live:
            continue
        thread = str(getattr(job, "thread_id", "") or "") or first_thread_id
        if not thread:
            continue
        entry = flags.setdefault(thread, {"running_build": False, "waiting_owner": False})
        entry["running_build" if state == "running" else "waiting_owner"] = True
    return flags


def _epoch_ms(seconds: Any) -> int:
    try:
        return int(round(float(seconds or 0) * 1000))
    except (TypeError, ValueError):
        return 0


def thread_payload(row: dict[str, Any], flags: dict[str, dict[str, bool]] | None = None) -> dict[str, Any]:
    """One conversation as every ``master_thread_*`` reply carries it -- the contract the console's column reads:
    ``{thread_id, project_id, channel_id, title, created_at, last_activity_at, archived, legacy, running_build,
    waiting_owner}``. The index keeps seconds, as chat rows do; the reply says epoch milliseconds, as the page's
    clocks do. ``archived`` is a boolean (the index keeps when)."""
    thread_id = str(row.get("thread_id", "") or "")
    mine = dict((flags or {}).get(thread_id) or {})
    return {
        "thread_id": thread_id,
        "project_id": str(row.get("project_id", "") or ""),
        "channel_id": str(row.get("channel_id", "") or ""),
        "title": str(row.get("title", "") or ""),
        "created_at": _epoch_ms(row.get("created_at")),
        "last_activity_at": _epoch_ms(row.get("last_activity_at")),
        "archived": float(row.get("archived_at", 0) or 0) > 0,
        "legacy": bool(row.get("legacy")),
        "running_build": bool(mine.get("running_build")),
        "waiting_owner": bool(mine.get("waiting_owner")),
    }


def conversation_in_words(job_thread: str, *, current: str, first_thread_id: str, titles: dict[str, str]) -> dict[str, Any]:
    """Which conversation a build job belongs to, for the model (master_list_builds) -- words, never the raw id.

    ``job_thread`` "" is a job recorded before conversations were split: the project's first conversation's.
    ``current`` is the conversation the turn runs in (its console session id)."""
    thread = str(job_thread or "") or str(first_thread_id or "")
    here = bool(thread) and thread == str(current or "")
    if here:
        words = "这段对话"
    else:
        title = str(titles.get(thread, "") or "").strip() if thread else ""
        if thread and thread == first_thread_id and not title:
            title = LEGACY_TITLE_ZH
        words = f"另一段对话「{title}」" if title else "另一段对话"
    return {"conversation": words, "in_this_conversation": here}


def latest_thread_id(rows: Any) -> str:
    """The conversation the builder-side 安安 panel works on: the project's most recently active one that is not
    archived ("" when there is none). The panel has no column to pick from, so it follows where the owner last was --
    the same answer every time for the same rows, never a conversation of another project."""
    live = [row for row in list(rows or []) if not float(row.get("archived_at", 0) or 0)]
    if not live:
        return ""
    best = max(live, key=lambda row: (float(row.get("last_activity_at", 0) or 0),
                                      float(row.get("created_at", 0) or 0), str(row.get("thread_id", "") or "")))
    return str(best.get("thread_id", "") or "")
