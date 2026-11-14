"""Project-backup expiry / rotation cleanup (CRM L4).

Keeps the newest ``keep_min`` archives always, then drops files older than
``keep_days``. Never deletes into an empty backups/projects folder when fewer
than ``keep_min`` archives exist. Matches the spirit of backup_list / health
pruning used elsewhere in office_ui.
"""
from __future__ import annotations

import time
from pathlib import Path

DEFAULT_KEEP_DAYS = 30
DEFAULT_KEEP_MIN = 3


def prune_project_backups(
    backups_dir: Path,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    keep_min: int = DEFAULT_KEEP_MIN,
    now: float | None = None,
) -> dict:
    """Delete expired ``*.zip`` under ``backups_dir``. Returns a small summary."""
    root = Path(backups_dir)
    keep_days = max(0, int(keep_days))
    keep_min = max(0, int(keep_min))
    summary = {
        "dir": str(root),
        "keep_days": keep_days,
        "keep_min": keep_min,
        "considered": 0,
        "kept": 0,
        "deleted": 0,
        "deleted_names": [],
    }
    if not root.is_dir():
        return summary
    files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    summary["considered"] = len(files)
    if not files:
        return summary
    cutoff = (now if now is not None else time.time()) - (keep_days * 86400)
    # Always retain the newest keep_min regardless of age.
    protected = set(files[:keep_min])
    deleted_names: list[str] = []
    for path in files:
        if path in protected:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink()
            deleted_names.append(path.name)
        except OSError:
            continue
    summary["deleted"] = len(deleted_names)
    summary["deleted_names"] = deleted_names
    summary["kept"] = summary["considered"] - summary["deleted"]
    return summary
