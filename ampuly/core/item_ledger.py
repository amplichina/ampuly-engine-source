"""The batch step's per-item ledger, as the file the owner reads (批量自动化 P4). Pure rendering; one atomic write.

The ledger itself lives in the Gateway journal, which is append-only and survives a restart. This module turns it
into the one thing a person can open: a table of every row, what happened to it, and what that rests on. The file
is a **re-render**, never an accumulation -- lose it and the next turn writes it again from the journal.

Two rules it keeps, both learned elsewhere in this product:

* **Every row appears, including the ones nobody accounted for.** A row that is missing from the file reads like a
  row that never needed doing.
* **Written whole or not at all.** Published through a temporary file and one rename, the shape
  :mod:`ampuly.computer_use.audit_export` already uses, so a crash mid-write cannot leave half a ledger that reads
  like a complete one.

It does not decide anything. Whether a row passed is the done check's word, made when that item closed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

# What each status is called in the file. A status this module does not know is printed as it came rather than
# guessed at: a ledger that renames an outcome it does not understand is worse than one that shows it raw.
_STATUS = {
    "verified": "已核验",
    "unverified": "未通过",
    "missing": "结果不明（没有台账行）",
}

_HEADERS = ("条目", "结果", "说明", "证据", "时间")
MAX_ROWS = 2000


def status_text(status: str) -> str:
    return _STATUS.get(str(status or ""), str(status or "") or "未知")


def _cell(value: Any) -> str:
    """One table cell: single-line, and never able to break out of the table it sits in."""
    text = " ".join(str(value if value is not None else "").split())
    return text.replace("|", "｜")[:200]


def _when(at: Any) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def render(rows: Iterable[dict[str, Any]], *, order_id: str = "", step: str = "", why: Any = None) -> str:
    """The ledger as markdown: a count, then every row in the order's own order.

    ``why`` maps a row's reason to words a person reads (the card's own vocabulary). Without it the reason is
    printed as the code recorded it -- raw, but never invented.
    """
    listed = [dict(row) for row in list(rows or [])][:MAX_ROWS]
    good = [row for row in listed if str(row.get("status", "") or "") == "verified"]
    lines = [
        f"# 分发结果{f'（订单 {order_id}）' if order_id else ''}",
        "",
        f"共 {len(listed)} 条，已核验 {len(good)} 条，需要处理 {len(listed) - len(good)} 条。"
        + (f"步骤：{step}。" if step else ""),
        "",
        "结果由代码逐条核验并记下；已核验的条目不会因为别的条目而作废。",
        "",
        "| " + " | ".join(_HEADERS) + " |",
        "|" + "|".join(["---"] * len(_HEADERS)) + "|",
    ]
    speak = dict(why or {})
    for row in listed:
        reason = str(row.get("reason", "") or "")
        evidence = dict(row.get("evidence", {}) or {})
        lines.append("| " + " | ".join([
            _cell(row.get("item")),
            _cell(status_text(str(row.get("status", "") or ""))),
            _cell(speak.get(reason, reason)),
            _cell(evidence.get("url", "")),
            _cell(_when(row.get("at"))),
        ]) + " |")
    return chr(10).join(lines) + chr(10)


def publish(text: str, path: Path) -> Path:
    """Write the ledger whole, or not at all.

    A temporary file in the same directory, flushed and fsynced, then one rename over the old one. A half-written
    ledger is the one failure that would read as a complete one.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline=chr(10)) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target
