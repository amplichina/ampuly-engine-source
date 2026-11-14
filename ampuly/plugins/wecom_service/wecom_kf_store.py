"""企业微信「微信客服」客户通道的四张表（业务库第 4 版，policy「按推荐做」）。

Plain reads and writes over ``BusinessStore``'s own connection (``extension_connection``), nothing else:

* ``wecom_kf_accounts`` which 客服账号 (open_kfid) feeds which project and which flow, the start fields only the
                             owner fills (never asked of a customer), the welcome text, who takes a handed-off customer,
                             and whether the account is switched on (and since when: messages sent before that are
                             recorded, never answered);
* ``wecom_kf_cursors`` where ``sync_msg`` stopped, per account. Deliberately **no project**: it is the position in
                             企业微信's message stream for that account; deleting a project must not rewind it, or the
                             next sync would fetch -- and could answer -- the old messages again;
* ``wecom_kf_conversations`` one per (project, account, customer): stage, the collected start fields, the order it
                             became, the send window (last customer message, messages sent since, the session state) and
                             how many AI turns it had today;
* ``wecom_kf_messages`` every message in and out, deduplicated by (account, msgid), text capped, media ids, the
                             screening flags, the order it belongs to, and whether it has been acted on.

A database still below version 4 has none of these tables: every read here answers empty, every write refuses in the
owner's words, and nothing here creates a table (only ``ampuly db-version --apply`` does).

No secrets live here: the relay's address and shared token stay in the environment (see ``wecom_relay_client``).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable

from ampuly.plugins.office_ui.business_store import OrderTransitionError

ACCOUNTS = "wecom_kf_accounts"
CURSORS = "wecom_kf_cursors"
CONVERSATIONS = "wecom_kf_conversations"
MESSAGES = "wecom_kf_messages"
TABLES = (ACCOUNTS, CURSORS, CONVERSATIONS, MESSAGES)

STAGES = ("greeting", "asking", "ordered", "handoff", "closed")
STAGE_WORDS = {"greeting": "刚进来", "asking": "在问需求", "ordered": "已建工单", "handoff": "转人工了", "closed": "会话已结束"}
DIRECTIONS = ("in", "out")
# Outbound outcomes: written ``pending`` before the send, settled after. A row left ``pending`` (the process died
# mid-send) reads as outcome unknown and is never sent again.
OUT_PENDING = "pending"
OUT_SENT = "sent"
OUT_REFUSED = "refused"
OUT_FAILED = "failed"
OUT_UNKNOWN = "outcome_unknown"

# How much of one message is kept. A longer one is cut here and marked; nothing is refused for its length.
TEXT_MAX_CHARS = 2000
WELCOME_MAX_CHARS = 600
ACCOUNT_NAME_MAX_CHARS = 60
_ID_MAX = 256


class WecomKfStoreError(OrderTransitionError):
    """A write this store refuses, said in the owner's words."""


def _now() -> str:
    return datetime.now().isoformat()


def _clean_id(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > _ID_MAX or any(ch.isspace() for ch in text) or not text.isascii():
        raise WecomKfStoreError(f"{what}不像一个企业微信给的编号")
    return text


def not_ready_text() -> str:
    from ampuly.database.schema_version import OWNER_COMMAND

    return (f"企业微信客户通道还没启用：这台机器的业务库还是旧的数据格式，系统不会自己去改它。需要你先停掉控制台，运行"
            f" `{OWNER_COMMAND}`（数据页上有带目录的完整命令；改之前会自动备份，出错会自动退回），再重新打开控制台。")


def _loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(str(text or ""))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


class WecomKfStore:
    """The four tables, over one ``BusinessStore``. Cheap to build; holds no connection."""

    def __init__(self, business: Any) -> None:
        self.business = business

    def _connect(self):
        # The store's own connection, as it offers it to an extension's tables (committed when the block ends, rolled
        # back on an error, always closed): nothing of the store's own is reached past it.
        return self.business.extension_connection()

    def ready(self) -> bool:
        """Whether this database has all four tables (business schema version 4). Read only."""
        with self._connect() as conn:
            names = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?)", TABLES)}
        return names == set(TABLES)

    def _require_ready(self) -> None:
        if not self.ready():
            raise WecomKfStoreError(not_ready_text())

    # ── accounts ──

    @staticmethod
    def _account_row(row: Any) -> dict[str, Any]:
        return {
            "open_kfid": str(row["open_kfid"]),
            "project_id": str(row["project_id"]),
            "workflow_id": str(row["workflow_id"]),
            "account_name": str(row["account_name"] or ""),
            "owner_only_fields": [str(item) for item in _loads(row["owner_only_fields_json"], []) if str(item)],
            "welcome_text": str(row["welcome_text"] or ""),
            "servicer_userid": str(row["servicer_userid"] or ""),
            "enabled": bool(row["enabled"]),
            "enabled_at": int(row["enabled_at"] or 0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "updated_by": str(row["updated_by"] or ""),
        }

    def account(self, open_kfid: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {ACCOUNTS} WHERE open_kfid = ?", (str(open_kfid or "").strip(),)).fetchone()
        return self._account_row(row) if row is not None else None

    def accounts(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        with self._connect() as conn:
            if project_id is None:
                rows = conn.execute(f"SELECT * FROM {ACCOUNTS} ORDER BY project_id, open_kfid").fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {ACCOUNTS} WHERE project_id = ? ORDER BY open_kfid",
                                    (str(project_id or "").strip(),)).fetchall()
        return [self._account_row(row) for row in rows]

    def set_account(
        self, open_kfid: str, *, project_id: str, workflow_id: str, account_name: str = "",
        owner_only_fields: Iterable[str] = (), welcome_text: str = "", servicer_userid: str = "",
        enabled: bool = False, actor: str, now: float,
    ) -> dict[str, Any]:
        """Bind one 客服账号 to one project and flow (whole row). An account already bound to another project is
        refused: one account feeds one project, and moving it is the owner's explicit unbind first."""
        self._require_ready()
        kfid = _clean_id(open_kfid, "客服账号")
        project = str(project_id or "").strip()
        flow = str(workflow_id or "").strip()
        if not project or not flow:
            raise WecomKfStoreError("要写明这个客服账号接哪个项目、哪条业务流")
        if not str(actor or "").strip():
            raise WecomKfStoreError("认不出是谁在操作，请重新登录后再试")
        name = str(account_name or "").strip()
        if len(name) > ACCOUNT_NAME_MAX_CHARS:
            raise WecomKfStoreError(f"客服账号的名字最多 {ACCOUNT_NAME_MAX_CHARS} 个字")
        welcome = str(welcome_text or "").replace("\r\n", "\n").strip()
        if len(welcome) > WELCOME_MAX_CHARS:
            raise WecomKfStoreError(f"欢迎语最多 {WELCOME_MAX_CHARS} 个字")
        servicer = str(servicer_userid or "").strip()
        if servicer:
            servicer = _clean_id(servicer, "接待人员")
        if isinstance(owner_only_fields, str):
            owner_only_fields = [part for part in owner_only_fields.replace("，", ",").split(",")]
        owner_only = list(dict.fromkeys(str(item).strip() for item in owner_only_fields if str(item).strip()))
        at = _now()
        with self._connect() as conn:
            held = conn.execute(f"SELECT project_id, enabled, enabled_at FROM {ACCOUNTS} WHERE open_kfid = ?", (kfid,)).fetchone()
            if held is not None and str(held["project_id"]) != project:
                raise WecomKfStoreError("这个客服账号已经绑在别的项目上了；一个客服账号只接一个项目，先在那个项目里解绑再绑到这里")
            # Switching on (again) moves the line: messages sent before it are recorded, not answered.
            was_on = held is not None and bool(held["enabled"])
            enabled_at = int(held["enabled_at"] or 0) if (was_on and enabled) else (int(now) if enabled else 0)
            conn.execute(
                f"""INSERT INTO {ACCOUNTS} (open_kfid, project_id, workflow_id, account_name, owner_only_fields_json,
                    welcome_text, servicer_userid, enabled, enabled_at, created_at, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(open_kfid) DO UPDATE SET workflow_id = excluded.workflow_id,
                    account_name = excluded.account_name, owner_only_fields_json = excluded.owner_only_fields_json,
                    welcome_text = excluded.welcome_text, servicer_userid = excluded.servicer_userid,
                    enabled = excluded.enabled, enabled_at = excluded.enabled_at, updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by""",
                (kfid, project, flow, name, json.dumps(owner_only, ensure_ascii=False), welcome, servicer,
                 1 if enabled else 0, enabled_at, at, at, str(actor).strip()),
            )
        return self.account(kfid) or {}

    def remove_account(self, open_kfid: str, *, project_id: str) -> bool:
        """Unbind an account from this project. Its conversations and messages stay: they are the customer record."""
        self._require_ready()
        with self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM {ACCOUNTS} WHERE open_kfid = ? AND project_id = ?",
                                  (str(open_kfid or "").strip(), str(project_id or "").strip()))
        return bool(cursor.rowcount)

    # ── cursors ──

    def cursor(self, open_kfid: str) -> str:
        if not self.ready():
            return ""
        with self._connect() as conn:
            row = conn.execute(f"SELECT next_cursor FROM {CURSORS} WHERE open_kfid = ?", (str(open_kfid or ""),)).fetchone()
        return str(row["next_cursor"] or "") if row is not None else ""

    def has_cursor(self, open_kfid: str) -> bool:
        if not self.ready():
            return False
        with self._connect() as conn:
            return conn.execute(f"SELECT 1 FROM {CURSORS} WHERE open_kfid = ?", (str(open_kfid or ""),)).fetchone() is not None

    def save_cursor(self, open_kfid: str, next_cursor: str) -> None:
        """Only called after the page it ends has been recorded (``record_page``)."""
        self._require_ready()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {CURSORS} (open_kfid, next_cursor, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(open_kfid) DO UPDATE SET next_cursor = excluded.next_cursor, updated_at = excluded.updated_at",
                (str(open_kfid), str(next_cursor or ""), _now()),
            )

    # ── conversations ──

    @staticmethod
    def _conversation_row(row: Any) -> dict[str, Any]:
        return {
            "conversation_id": str(row["conversation_id"]),
            "project_id": str(row["project_id"]),
            "open_kfid": str(row["open_kfid"]),
            "external_userid": str(row["external_userid"]),
            "workflow_id": str(row["workflow_id"] or ""),
            "customer_id": str(row["customer_id"] or ""),
            "contact_id": str(row["contact_id"] or ""),
            "stage": str(row["stage"] or "greeting"),
            "collected": _loads(row["collected_json"], {}),
            "order_id": str(row["order_id"] or ""),
            "order_claimed_at": str(row["order_claimed_at"] or ""),
            "last_customer_message_at": int(row["last_customer_message_at"] or 0),
            "sent_since_customer_message": int(row["sent_since_customer_message"] or 0),
            "service_state": int(row["service_state"] or 0),
            "ai_turns_day": str(row["ai_turns_day"] or ""),
            "ai_turns_today": int(row["ai_turns_today"] or 0),
            "handoff_reason": str(row["handoff_reason"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {CONVERSATIONS} WHERE conversation_id = ?", (str(conversation_id or ""),)).fetchone()
        return self._conversation_row(row) if row is not None else None

    def conversation_for(self, project_id: str, open_kfid: str, external_userid: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {CONVERSATIONS} WHERE project_id = ? AND open_kfid = ? AND external_userid = ?",
                (str(project_id), str(open_kfid), str(external_userid)),
            ).fetchone()
        return self._conversation_row(row) if row is not None else None

    def conversation_for_order(self, order_id: str) -> dict[str, Any] | None:
        if not self.ready() or not str(order_id or "").strip():
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {CONVERSATIONS} WHERE order_id = ?", (str(order_id).strip(),)).fetchone()
        return self._conversation_row(row) if row is not None else None

    def conversations(self, project_id: str) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {CONVERSATIONS} WHERE project_id = ? ORDER BY created_at, conversation_id",
                                (str(project_id or ""),)).fetchall()
        return [self._conversation_row(row) for row in rows]

    def ensure_conversation(self, *, project_id: str, open_kfid: str, external_userid: str, workflow_id: str) -> dict[str, Any]:
        self._require_ready()
        found = self.conversation_for(project_id, open_kfid, external_userid)
        if found is not None:
            return found
        at = _now()
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO {CONVERSATIONS} (conversation_id, project_id, open_kfid, external_userid, workflow_id,
                    created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, open_kfid, external_userid) DO NOTHING""",
                ("kfc-" + uuid.uuid4().hex, str(project_id), str(open_kfid), str(external_userid), str(workflow_id or ""), at, at),
            )
        return self.conversation_for(project_id, open_kfid, external_userid) or {}

    _UPDATABLE = {
        "workflow_id", "customer_id", "contact_id", "stage", "order_id", "order_claimed_at",
        "last_customer_message_at", "sent_since_customer_message", "service_state", "ai_turns_day", "ai_turns_today",
        "handoff_reason",
    }

    def update_conversation(self, conversation_id: str, *, collected: dict[str, Any] | None = None,
                            only_if_order_empty: bool = False, **values: Any) -> bool:
        """Write the named columns (and the collected fields, when given). ``only_if_order_empty`` makes the write a
        claim: it happens only while the conversation has no order and no claim yet, and says whether it did."""
        self._require_ready()
        unknown = set(values) - self._UPDATABLE
        if unknown:
            raise ValueError(f"not a conversation column: {sorted(unknown)}")
        if "stage" in values and values["stage"] not in STAGES:
            raise ValueError(f"not a stage: {values['stage']!r}")
        sets = [f"{name} = ?" for name in values]
        params: list[Any] = list(values.values())
        if collected is not None:
            sets.append("collected_json = ?")
            params.append(json.dumps(collected, ensure_ascii=False, sort_keys=True))
        sets.append("updated_at = ?")
        params.append(_now())
        where = "conversation_id = ?"
        params.append(str(conversation_id))
        if only_if_order_empty:
            where += " AND order_id = '' AND order_claimed_at = ''"
        with self._connect() as conn:
            cursor = conn.execute(f"UPDATE {CONVERSATIONS} SET {', '.join(sets)} WHERE {where}", tuple(params))
        return bool(cursor.rowcount)

    def ai_turns_on(self, open_kfid: str, day: str) -> int:
        """Every AI turn this account's conversations had on ``day`` (the per-account daily cap reads this)."""
        if not self.ready():
            return 0
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(ai_turns_today), 0) FROM {CONVERSATIONS} WHERE open_kfid = ? AND ai_turns_day = ?",
                (str(open_kfid), str(day)),
            ).fetchone()
        return int(row[0] or 0) if row is not None else 0

    # ── messages ──

    @staticmethod
    def _message_row(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "project_id": str(row["project_id"]),
            "conversation_id": str(row["conversation_id"] or ""),
            "open_kfid": str(row["open_kfid"]),
            "external_userid": str(row["external_userid"] or ""),
            "msgid": str(row["msgid"]),
            "direction": str(row["direction"]),
            "origin": int(row["origin"] or 0),
            "msgtype": str(row["msgtype"] or ""),
            "event_type": str(row["event_type"] or ""),
            "send_time": int(row["send_time"] or 0),
            "text": str(row["text"] or ""),
            "text_cut": bool(row["text_cut"]),
            "media_id": str(row["media_id"] or ""),
            "screen": [str(flag) for flag in _loads(row["screen_json"], [])],
            "outcome": str(row["outcome"] or ""),
            "order_id": str(row["order_id"] or ""),
            "handled_at": str(row["handled_at"] or ""),
            "recorded_at": str(row["recorded_at"] or ""),
        }

    @staticmethod
    def cap_text(text: Any) -> tuple[str, bool]:
        value = str(text or "")
        return (value[:TEXT_MAX_CHARS], True) if len(value) > TEXT_MAX_CHARS else (value, False)

    def record_inbound(self, rows: list[dict[str, Any]]) -> list[int | None]:
        """Record synced messages in one transaction; a (account, msgid) already here is skipped, never overwritten.
        Returns, row for row, the new row's id or None for one that was already on record. Each row: project_id,
        conversation_id, open_kfid, external_userid, msgid, origin, msgtype, event_type, send_time, text, media_id,
        screen (list), handled (bool)."""
        self._require_ready()
        new_ids: list[int | None] = []
        at = _now()
        with self._connect() as conn:
            for row in rows:
                text, cut = self.cap_text(row.get("text", ""))
                try:
                    cursor = conn.execute(
                        f"""INSERT INTO {MESSAGES} (project_id, conversation_id, open_kfid, external_userid, msgid,
                            direction, origin, msgtype, event_type, send_time, text, text_cut, media_id, screen_json,
                            handled_at, recorded_at) VALUES (?, ?, ?, ?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(row["project_id"]), str(row.get("conversation_id", "") or ""), str(row["open_kfid"]),
                         str(row.get("external_userid", "") or ""), str(row["msgid"]), int(row.get("origin", 0) or 0),
                         str(row.get("msgtype", "") or ""), str(row.get("event_type", "") or ""),
                         int(row.get("send_time", 0) or 0), text, 1 if cut else 0, str(row.get("media_id", "") or ""),
                         json.dumps(list(row.get("screen", []) or []), ensure_ascii=False),
                         at if row.get("handled") else "", at),
                    )
                except sqlite3.IntegrityError:
                    new_ids.append(None)
                    continue
                new_ids.append(int(cursor.lastrowid))
        return new_ids

    def has_message(self, open_kfid: str, msgid: str) -> bool:
        if not self.ready():
            return False
        with self._connect() as conn:
            return conn.execute(f"SELECT 1 FROM {MESSAGES} WHERE open_kfid = ? AND msgid = ?",
                                (str(open_kfid), str(msgid))).fetchone() is not None

    def unhandled(self, open_kfid: str) -> list[dict[str, Any]]:
        """Inbound rows of this account not yet acted on, oldest first."""
        if not self.ready():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {MESSAGES} WHERE open_kfid = ? AND direction = 'in' AND handled_at = '' ORDER BY id",
                (str(open_kfid),),
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def claim_messages(self, ids: list[int]) -> list[int]:
        """Mark these inbound rows acted on, before acting (at most once: a crash after this acts on none of them).
        Returns the ids this call claimed."""
        self._require_ready()
        claimed: list[int] = []
        at = _now()
        with self._connect() as conn:
            for message_id in ids:
                cursor = conn.execute(f"UPDATE {MESSAGES} SET handled_at = ? WHERE id = ? AND handled_at = ''",
                                      (at, int(message_id)))
                if cursor.rowcount:
                    claimed.append(int(message_id))
        return claimed

    def record_outbound(self, *, project_id: str, conversation_id: str, open_kfid: str, external_userid: str,
                        msgid: str, msgtype: str, text: str, kind: str = "", order_id: str = "",
                        send_time: int = 0) -> None:
        """Write the outgoing message as ``pending`` before it is sent: a send whose answer never came stays on record."""
        self._require_ready()
        body, cut = self.cap_text(text)
        at = _now()
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO {MESSAGES} (project_id, conversation_id, open_kfid, external_userid, msgid, direction,
                    origin, msgtype, event_type, send_time, text, text_cut, outcome, order_id, handled_at, recorded_at)
                    VALUES (?, ?, ?, ?, ?, 'out', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(project_id), str(conversation_id), str(open_kfid), str(external_userid), str(msgid),
                 str(msgtype), str(kind or ""), int(send_time or 0), body, 1 if cut else 0, OUT_PENDING,
                 str(order_id or ""), at, at),
            )

    def settle_outbound(self, open_kfid: str, msgid: str, outcome: str) -> None:
        self._require_ready()
        with self._connect() as conn:
            conn.execute(f"UPDATE {MESSAGES} SET outcome = ? WHERE open_kfid = ? AND msgid = ? AND direction = 'out'",
                         (str(outcome), str(open_kfid), str(msgid)))

    def mark_send_failed(self, open_kfid: str, msgid: str) -> bool:
        """企业微信 said one of our messages did not arrive (msg_send_fail)."""
        self._require_ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {MESSAGES} SET outcome = ? WHERE open_kfid = ? AND msgid = ? AND direction = 'out'",
                (OUT_FAILED, str(open_kfid), str(msgid)),
            )
        return bool(cursor.rowcount)

    def messages(self, conversation_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The newest ``limit`` messages of a conversation, oldest first."""
        if not self.ready():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {MESSAGES} WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (str(conversation_id), max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._message_row(row) for row in reversed(rows)]

    def lane_facts(self, project_id: str, *, delivery_kinds: Iterable[str] = ("delivery", "delivery_file")) -> dict[str, Any]:
        """What the project centre's 客户确认 lane reads of this project's 微信客服 records (W3b). Read only.

        ``accounts`` / ``enabled_accounts``: the 客服账号 bound to the project; ``customer_messages``: when each customer
        message arrived (ISO, oldest first); ``deliveries``: each delivery message we tried to send, ``{"at", "outcome"}``
        (oldest first; a row still ``pending`` reads as outcome unknown)."""
        facts: dict[str, Any] = {"accounts": 0, "enabled_accounts": 0, "customer_messages": [], "deliveries": []}
        if not self.ready():
            return facts
        project = str(project_id or "").strip()
        accounts = self.accounts(project)
        facts["accounts"] = len(accounts)
        facts["enabled_accounts"] = sum(1 for row in accounts if row["enabled"])
        kinds = [str(kind) for kind in delivery_kinds]
        marks = ",".join("?" for _ in kinds) or "''"
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT direction, origin, event_type, outcome, send_time, recorded_at FROM {MESSAGES}
                    WHERE project_id = ? AND ((direction = 'in' AND origin = 3)
                    OR (direction = 'out' AND event_type IN ({marks}))) ORDER BY id""",
                (project, *kinds),
            ).fetchall()
        for row in rows:
            if str(row["direction"]) == "in":
                sent = int(row["send_time"] or 0)
                at = datetime.fromtimestamp(sent).isoformat(timespec="seconds") if sent else str(row["recorded_at"] or "")
                facts["customer_messages"].append(at)
            else:
                facts["deliveries"].append({"at": str(row["recorded_at"] or ""), "outcome": str(row["outcome"] or "")})
        facts["customer_messages"].sort()
        return facts

    def link_messages_to_order(self, conversation_id: str, order_id: str) -> None:
        self._require_ready()
        with self._connect() as conn:
            conn.execute(f"UPDATE {MESSAGES} SET order_id = ? WHERE conversation_id = ? AND order_id = ''",
                         (str(order_id), str(conversation_id)))
