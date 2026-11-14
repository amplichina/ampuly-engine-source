"""Where each project's files live, when the owner has said so.

Until 2026-09-23 a project's folder was arithmetic: ``<workplace root>/<project id>``, with the root coming from
``AMPULY_WORKPLACE_ROOT`` or from a default beside the repository. The owner asked to choose the folder themselves --
per project, and a default root for new ones -- so this module holds what they chose.

Three things about the shape are deliberate:

* **It is not part of ``AmpulyConfig``.** The workspace-trust fingerprint hashes that object's authority fields, so a
  mapping kept inside it would change the fingerprint every time the owner moved a folder, and the console would
  refuse to start until the workspace was trusted again. That happened once already, for a smaller reason.
* **It fails open to today.** A file that is missing, unreadable, or of another schema resolves exactly as the
  arithmetic did. A corrupt file must not make every project's files unreachable.
* **Every entry is re-checked on the way in**, by the same rules that refuse a folder when the owner chooses one.
  An entry that fails is dropped -- that project falls back to the default root -- and the drop is reported, by
  code and project id, never by path.

It imports nothing from :mod:`ampuly.core.config`: the caller passes the home directory, so there is no cycle.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

MAPPING_FILE = "project_paths.json"
SCHEMA = 1
# The steady-state cost of resolving a path is a dict lookup plus, at most once a second per process, one stat.
_STAT_INTERVAL_SEC = 1.0
# A path a person can still read in a dialog, and short enough to leave room for an order's own folders under it.
MAX_PATH_CHARS = 150
_CONTROL = {chr(code) for code in range(0, 32)} | {"\x7f"}


@dataclass(frozen=True)
class Mapping:
    """What the owner chose, after every entry was re-checked.

    ``dropped`` carries ``(project id, code)`` for every entry that failed; the default root uses ``""`` as its id,
    so a refused root is distinguishable from one that was never set.
    """

    default_root: Path | None = None
    projects: dict[str, Path] = field(default_factory=dict)
    # Where a project's files were before it was pointed somewhere else, while that folder still holds any. It
    # lives here rather than in the business store so the console can still offer 「导入旧文件」 when that store
    # cannot be read, and so a restart never loses the offer.
    legacy: dict[str, Path] = field(default_factory=dict)
    dropped: tuple[tuple[str, str], ...] = ()
    source: str = "absent"
    stat_key: tuple[int, int, int] = (0, 0, 0)

    def pinned(self, project_id: str) -> Path | None:
        return self.projects.get(str(project_id or "").strip())


def mapping_path(home: Path) -> Path:
    return Path(home) / MAPPING_FILE


# ── Checking a path, without asking the disk what is beside it ───────────────────────────────────────────────────

def target_check(path: Path, *, home: Path, product_root: Path, allow: Path | None = None) -> str:
    """Why this path may not be a project's folder, or ``""``.

    The half of the rule that needs no knowledge of the other projects: it is what a stored entry has to satisfy
    on the way in, and what the chooser checks before it looks at anything else. ``allow`` is the one path that is
    admitted even when it sits inside the home or the product repository -- today's own answer for this project,
    which is inside both on a local install.
    """
    try:
        text = str(path)
    except Exception:  # noqa: BLE001 - a path that cannot even be written down is not a folder
        return "invalid_path"
    if not text.strip():
        return "invalid_path"
    if any(char in text for char in _CONTROL) or "\x00" in text:
        return "invalid_path"
    if len(text) > MAX_PATH_CHARS:
        return "path_too_long"
    if text.startswith("\\\\") or text.startswith("//"):
        # A UNC share: the Windows workspace capability refuses one outright, so a project could not be read there.
        return "network_path"
    candidate = Path(text)
    if not candidate.is_absolute():
        return "invalid_path"
    parts = candidate.parts
    if ".." in parts or "." in parts[1:]:
        return "invalid_path"
    for part in parts[1:]:
        if part.strip() != part or part.rstrip(".") != part:
            return "invalid_path"
    if len(parts) <= 1:
        return "drive_root"
    if allow is not None and _same_path(candidate, allow):
        return ""
    if _inside(candidate, Path(home)):
        return "inside_ampuly_home"
    if _inside(candidate, Path(product_root)):
        return "inside_product_repo"
    return ""


def linkless(path: Path) -> bool:
    """Whether this path and every existing ancestor is itself, and not a link to somewhere else."""
    from ampuly.plugins.office_ui.artifacts_service import redirected

    current = Path(path)
    seen: set[str] = set()
    while True:
        key = os.path.normcase(str(current))
        if key in seen:
            return True
        seen.add(key)
        if os.path.lexists(current) and redirected(current):
            return False
        parent = current.parent
        if parent == current:
            return True
        current = parent


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _inside(candidate: Path, parent: Path) -> bool:
    """Whether ``candidate`` is ``parent`` or sits under it, by name -- no disk access, no resolution."""
    try:
        left = os.path.normcase(os.path.abspath(str(candidate)))
        right = os.path.normcase(os.path.abspath(str(parent)))
    except Exception:  # noqa: BLE001
        return False
    if left == right:
        return True
    return left.startswith(right.rstrip("\\/") + os.sep)


# ── Reading it ───────────────────────────────────────────────────────────────────────────────────────────────────

def _product_root() -> Path:
    """The product's own repository, from this file's place in it."""
    return Path(__file__).resolve().parents[2]


def _stat_key(path: Path) -> tuple[int, int, int]:
    try:
        info = os.stat(path)
    except OSError:
        return (0, 0, 0)
    return (int(info.st_mtime_ns), int(info.st_size), int(getattr(info, "st_ino", 0) or 0))


def load(home: Path, *, workplace_root: Path | None = None, exempt_roots: tuple[Path, ...] | None = None) -> Mapping:
    """Read the mapping and re-check every entry. Never raises: a file it cannot use resolves as today."""
    from ampuly.project_id import is_valid_project_id

    path = mapping_path(home)
    key = _stat_key(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Mapping(source="absent", stat_key=key)
    except OSError:
        return Mapping(source="unreadable", stat_key=key)
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return Mapping(source="unreadable", stat_key=key)
    if not isinstance(data, dict):
        return Mapping(source="bad_schema", stat_key=key)
    try:
        schema = int(data.get("schema", 0) or 0)
    except (TypeError, ValueError):
        # A schema that is not a number is a file we cannot use, not a reason to raise: this function promises
        # never to, because raising here would make every project's files unreachable at once.
        return Mapping(source="bad_schema", stat_key=key)
    if schema != SCHEMA:
        return Mapping(source="bad_schema", stat_key=key)

    product = _product_root()
    dropped: list[tuple[str, str]] = []

    def admit(value: Any, project_id: str) -> Path | None:
        # Not stripped. A name with a space or a dot at either end is a different name on Windows, and repairing it
        # here would admit a path the chooser refused -- the mistake data phase 2 had to fix twice.
        text = str(value or "")
        if not text.strip():
            return None
        candidate = Path(text)
        roots = exempt_roots if exempt_roots is not None else (
            (Path(workplace_root),) if workplace_root else _exempt_roots())
        # Always including the home's own workplace folder: on a local install that is where every project has
        # always been, and unlike the environment variables it is knowable whatever the console was started by.
        roots = tuple(roots) + (Path(home) / "workplace",)
        code = "invalid_path"
        for root in (roots or (None,)):
            allow = (Path(root) / project_id) if (root and project_id) else None
            code = target_check(candidate, home=Path(home), product_root=product, allow=allow)
            if not code:
                break
        if not code and not linkless(candidate):
            code = "refused_link"
        if code:
            dropped.append((project_id, code))
            logger.warning(f"project folder ignored: project={project_id or '(default root)'} reason={code}")
            return None
        return candidate

    projects: dict[str, Path] = {}
    for project_id, value in (data.get("projects") or {}).items() if isinstance(data.get("projects"), dict) else ():
        clean = str(project_id or "").strip()
        if not clean or not is_valid_project_id(clean):
            dropped.append((clean, "invalid_project_id"))
            logger.warning(f"project folder ignored: project={clean!r} reason=invalid_project_id")
            continue
        admitted = admit(value, clean)
        if admitted is not None:
            projects[clean] = admitted

    default_root = admit(data.get("default_root"), "")
    legacy: dict[str, Path] = {}
    for project_id, value in (data.get("legacy") or {}).items() if isinstance(data.get("legacy"), dict) else ():
        clean = str(project_id or "").strip()
        text = str(value or "")
        if not clean or not is_valid_project_id(clean) or not text.strip():
            continue
        candidate = Path(text)
        # A former location is only ever shown and read from, so it is held to the shape rules and the link rule
        # but not to "where a project may live" -- it may well be a place a project may no longer be put.
        if candidate.is_absolute() and ".." not in candidate.parts and linkless(candidate):
            legacy[clean] = candidate
    return Mapping(default_root=default_root, projects=projects, legacy=legacy, dropped=tuple(dropped),
                   source="file", stat_key=key)


def product_owned(home: Path, project_id: str, path: Path) -> bool:
    """Whether this path is exactly ``<one of our own roots>/<project id>`` -- a folder the product made.

    「有没有被钉住」 is not the same question as 「这个文件夹是不是用户自己起的名字」, and using the first as a
    stand-in for the second is a mistake with teeth: ``folder_move.set_default_root`` pins **every** existing
    project in place, at ``<old root>/<pid>``, so on this install one click on 新项目默认根目录 would otherwise
    turn every project's own folder -- inside the product's home -- into 「用户指定的文件夹」. Destroying one
    would then leave our own files standing and report them back to the owner as theirs.

    The roots and the comparison are the loader's own, so this answer and the one ``admit()`` gives cannot drift.
    """
    project = str(project_id or "").strip()
    if not project:
        return False
    roots = tuple(_exempt_roots()) + (Path(home) / "workplace",)
    return any(_same_path(Path(path), Path(root) / project) for root in roots)


_CACHE: dict[str, tuple[Mapping, float]] = {}


def _exempt_roots() -> tuple[Path, ...]:
    """The roots whose ``<root>/<project id>`` a stored pin may name, even though they are otherwise refused.

    Asking the resolver would be circular, so config computes them from the environment and the historical
    default. There is more than one on purpose: the launcher stops pinning ``AMPULY_WORKPLACE_ROOT`` once a default
    root is chosen, and an exemption tied to that one variable would drop every pin the moment it did.
    """
    try:
        from ampuly.core.config import candidate_workplace_roots

        return candidate_workplace_roots()
    except Exception:  # noqa: BLE001 - without them the exemption simply does not apply
        return ()


def mapping(home: Path, *, workplace_root: Path | None = None) -> Mapping:
    """The mapping for this home, cached, revalidated by the file's own stat at most once a second.

    The cache is keyed by the workplace root as well, because that root decides which stored entry is exempt from
    the "not inside the home, not inside the repository" rule.
    """
    roots = (Path(workplace_root),) if workplace_root is not None else _exempt_roots()
    key = os.path.normcase(str(Path(home))) + "|" + "|".join(os.path.normcase(str(r)) for r in roots)
    cached = _CACHE.get(key)
    now = time.monotonic()
    if cached is not None:
        found, checked_at = cached
        if now - checked_at < _STAT_INTERVAL_SEC:
            return found
        if _stat_key(mapping_path(home)) == found.stat_key:
            _CACHE[key] = (found, now)
            return found
    fresh = load(home, exempt_roots=roots)
    _CACHE[key] = (fresh, now)
    return fresh


def invalidate(home: Path | None = None) -> None:
    """Forget what was read. ``None`` forgets every home."""
    if home is None:
        _CACHE.clear()
        return
    prefix = os.path.normcase(str(Path(home))) + "|"
    for key in [item for item in _CACHE if item.startswith(prefix)]:
        _CACHE.pop(key, None)


def save(home: Path, *, default_root: Path | None, projects: dict[str, Path],
         legacy: dict[str, Path] | None = None, actor: str = "owner") -> Mapping:
    """Write the mapping whole, atomically, and return what a fresh read makes of it.

    The reply is the *reloaded* mapping on purpose: what the owner gets back is what the resolver will answer, not
    what was asked for.
    """
    from datetime import datetime

    from ampuly.core.config import replace_with_retry

    target = mapping_path(home)
    payload = {
        "schema": SCHEMA,
        "default_root": str(default_root) if default_root else "",
        "projects": {str(pid): str(path) for pid, path in sorted(projects.items())},
        "legacy": {str(pid): str(path) for pid, path in sorted((legacy or {}).items())},
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "updated_by": str(actor or "owner"),
    }
    Path(home).mkdir(parents=True, exist_ok=True)
    tmp = Path(home) / f".{MAPPING_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    invalidate(home)
    return mapping(home)


def pinned(home: Path, project_id: str) -> Path | None:
    """This project's own folder when the owner named one, else ``None``.

    Asked by the callers that must not create what is not there: a pinned folder that has gone (an unplugged disk,
    a drive letter that now belongs to something else) is refused, never manufactured.
    """
    return mapping(home).pinned(project_id)
