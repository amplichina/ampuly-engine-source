"""Retention for bound browser profiles: a revoked session's cookies do not outlive it.

A07 in the plan: switching customers, an expired session, a cross-domain
jump -- the previous customer's data and login state must be unreachable.
The registry already refuses a revoked binding and keeps profiles apart per
binding. What it did not do was touch the disk: the profile directory a
revoked or long-expired session logged into stayed where it was, cookies and
all, until someone deleted it by hand.

Two bounded deletions, nothing else:

- ``purge_profile`` removes exactly one profile directory, and only when it
  is a direct child of the product's profile root, named by a binding key,
  a real directory and not a link. Anything else is refused by name.
- ``purge_stale_profiles`` sweeps that root for directories untouched for
  longer than a retention age, skipping the keys a registry says are live.

Neither is a general "clean up": there is no recursion beyond the one
profile directory, no wildcard and no path taken from a conversation.
"""
from __future__ import annotations

import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Iterable

from ampuly.core.config import get_ampuly_home

KEY_RE = re.compile(r"^[0-9a-f]{16}$")
ROOT_PARTS = ("browser", "profiles")


def profiles_root(home: Path | None = None) -> Path:
    return Path(home if home is not None else get_ampuly_home()) / ROOT_PARTS[0] / ROOT_PARTS[1]


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def purgeable(root: Path, key: str) -> tuple[Path | None, str]:
    """The one directory this key may name under this root, or why not."""
    root = Path(root)
    if tuple(root.parts[-2:]) != ROOT_PARTS:
        return None, "root_is_not_the_profile_root"
    if not KEY_RE.fullmatch(str(key or "")):
        return None, "not_a_binding_key"
    target = root / key
    if _is_link(root) or _is_link(target):
        return None, "link_refused"
    if not target.is_dir():
        return None, "no_such_profile"
    try:
        if target.resolve().parent != root.resolve():
            return None, "outside_profile_root"
    except OSError:
        return None, "unresolvable"
    return target, ""


def purge_profile(root: Path, key: str) -> dict[str, Any]:
    """Remove one profile directory, or say why it was left alone."""
    target, reason = purgeable(root, key)
    if target is None:
        return {"key": str(key or ""), "removed": False, "reason": reason}
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return {"key": key, "removed": False, "reason": f"remove_failed:{type(exc).__name__}"}
    return {"key": key, "removed": True, "reason": ""}


def purge_profile_for(binding: Any, home: Path | None = None) -> dict[str, Any]:
    if not getattr(binding, "is_bound", False):
        return {"key": "", "removed": False, "reason": "unbound_session_has_no_profile"}
    return purge_profile(profiles_root(home), binding.key())


def purge_stale_profiles(root: Path, *, max_age_sec: float, now: float | None = None,
                         keep: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Profiles nobody has touched inside the retention window, except live ones."""
    root = Path(root)
    results: list[dict[str, Any]] = []
    if max_age_sec <= 0 or not root.is_dir() or tuple(root.parts[-2:]) != ROOT_PARTS:
        return results
    moment = time.time() if now is None else float(now)
    live = {str(k) for k in keep}
    for child in sorted(root.iterdir()):
        key = child.name
        if not KEY_RE.fullmatch(key) or key in live or not child.is_dir() or _is_link(child):
            continue
        try:
            age = moment - child.stat().st_mtime
        except OSError:
            continue
        if age > max_age_sec:
            results.append({**purge_profile(root, key), "age_sec": int(age)})
    return results
