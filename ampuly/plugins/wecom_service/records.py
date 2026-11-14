"""The 企业微信客服 tables in a project's records: purged with the project, carried in its backup, put back by a restore.

Business schema version 4 (policy). All three tables that name a project are purged with it and carried in its
backup. The conversations and the messages are the customer record: a restore puts them back, and sends nothing -- no
row here is a message waiting to go out. The account binding comes back switched OFF (a restored project must not start
answering customers on its own), and only when no project here holds that account now. The sync cursor
(``wecom_kf_cursors``) has no project on purpose: it is the account's place in 企业微信's stream, and rewinding it with a
project would fetch old messages again, so it is never carried. Absent below version 4; every path checks.

These are the console's project-records seam (``extensions.ProjectRecords``); ``business_store`` calls them inside its
own transaction, on its own connection, in the places its own tables are handled.
"""

from __future__ import annotations

from typing import Any, Mapping

from ampuly.plugins.office_ui.extensions import ProjectRecords

WECOM_KF_PROJECT_TABLES = ("wecom_kf_accounts", "wecom_kf_conversations", "wecom_kf_messages")
# The order each table is exported in.
_EXPORT_ORDER = (("wecom_kf_accounts", "open_kfid"), ("wecom_kf_conversations", "created_at, conversation_id"),
                 ("wecom_kf_messages", "id"))


def _exists(conn: Any, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def purge(conn: Any, project_id: str) -> dict[str, int]:
    """The project's account bindings, conversations and messages go with it, where the tables exist. The account's
    sync cursor stays (it carries no project)."""
    removed: dict[str, int] = {}
    for table in WECOM_KF_PROJECT_TABLES:
        removed[table] = 0
        if _exists(conn, table):
            removed[table] = int(conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,)).rowcount or 0)
    return removed


def export(conn: Any, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """The account bindings, conversations and messages, where the tables exist. A restore puts the conversations and
    messages back and the bindings back switched off."""
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, order in _EXPORT_ORDER:
        if _exists(conn, table):
            tables[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE project_id = ? ORDER BY {order}", (project_id,))]
    return tables


def check(project_id: str, rows_of: Mapping[str, list[dict[str, Any]]]) -> None:
    """Before anything is written: every row is this project's, and names what identifies it."""
    from ampuly.plugins.office_ui.business_store import OrderTransitionError

    if any(str(row.get("project_id", "") or "") != project_id for name in WECOM_KF_PROJECT_TABLES for row in rows_of[name]):
        raise OrderTransitionError("a 企业微信 binding, conversation or message in the export belongs to another project")
    if (any(not str(row.get("open_kfid", "") or "") for name in WECOM_KF_PROJECT_TABLES for row in rows_of[name])
            or any(not str(row.get("conversation_id", "") or "") for row in rows_of["wecom_kf_conversations"])
            or any(not str(row.get("msgid", "") or "") for row in rows_of["wecom_kf_messages"])):
        raise OrderTransitionError("the export carries a 企业微信 row without its id")


def prepare_import(conn: Any, project_id: str, rows_of: dict[str, list[dict[str, Any]]]) -> None:
    """Inside the restore's transaction, before anything goes in: never over a row already here, never onto a database
    that has not got the tables yet; a binding comes back switched off, and only for an account no project here holds
    now (``rows_of`` is changed to say so)."""
    from ampuly.plugins.office_ui.business_store import OrderTransitionError

    for table in WECOM_KF_PROJECT_TABLES:
        if not rows_of[table]:
            continue
        if not _exists(conn, table):
            raise OrderTransitionError(f"{table} is not in this database yet (business schema version 4); run `ampuly db-version --apply` first")
        if conn.execute(f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1", (project_id,)).fetchone():
            raise OrderTransitionError(f"{table} already holds a row of this project; a restore never overwrites")
    conversation_ids = [str(row["conversation_id"]) for row in rows_of["wecom_kf_conversations"]]
    if conversation_ids and conn.execute(
        f"SELECT 1 FROM wecom_kf_conversations WHERE conversation_id IN ({','.join('?' for _ in conversation_ids)}) LIMIT 1",
        tuple(conversation_ids),
    ).fetchone():
        raise OrderTransitionError("wecom_kf_conversations already holds a row of this export; a restore never overwrites")
    for row in rows_of["wecom_kf_messages"]:
        if conn.execute("SELECT 1 FROM wecom_kf_messages WHERE open_kfid = ? AND msgid = ?",
                        (str(row["open_kfid"]), str(row["msgid"]))).fetchone():
            raise OrderTransitionError("wecom_kf_messages already holds a message of this export; a restore never overwrites")
    # A binding comes back switched off, and only for an account no project here holds now.
    taken_accounts = {str(item[0]) for item in conn.execute("SELECT open_kfid FROM wecom_kf_accounts").fetchall()} \
        if rows_of["wecom_kf_accounts"] else set()
    rows_of["wecom_kf_accounts"] = [
        {**row, "enabled": 0, "enabled_at": 0} for row in rows_of["wecom_kf_accounts"]
        if str(row["open_kfid"]) not in taken_accounts
    ]


def discard(conn: Any, project_id: str, tables: Mapping[str, Any]) -> None:
    """Take back out the rows an import put in (the import refused if the project already had any here)."""
    for table in WECOM_KF_PROJECT_TABLES:
        if tables.get(table) and _exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))


PROJECT_RECORDS = ProjectRecords(
    tables=WECOM_KF_PROJECT_TABLES,
    export=export,
    purge=purge,
    check=check,
    prepare_import=prepare_import,
    inserts=(("wecom_kf_accounts", ()), ("wecom_kf_conversations", ()), ("wecom_kf_messages", ("id",))),
    discard=discard,
)
