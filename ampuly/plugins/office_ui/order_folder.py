"""Which files on disk a deliverable names -- one rule for the whole console (C5-1, policy).

A step declares its deliverables relative to the order's shared output tree -- ``brief/需求单.md``, ``board/*.png``,
``round1/`` -- and the owner's receipt, the Feishu card's precheck, the console's gate panel, the data page's order view
and the node trial each used to answer "which files is that" their own way. They answer it here, one way:

* a plain path names that one file;
* ``dir/`` names the files directly inside that folder -- not its sub-folders, and never a ``*.prompt.json`` beside a
  picture, which is a record, not a deliverable;
* ``*``, ``?`` and ``[...]`` match within one name and never across ``/``: ``board/*.png`` is the board's pictures, not
  the ones in ``board/draft/``. A whole ``**`` segment is any number of folders, as pathlib has it;
* names compare the way the machine compares them: without regard to case on Windows, exactly elsewhere;
* a hidden name (``.x``, the runtime's own ``.ampuly-*`` files, ``.order-steps.json``) and a link of any kind -- a symlink,
  a junction, a file with a second name -- never count, and a folder behind a link is never entered;
* every scan is bounded; a pattern whose scan ran out says so (``truncated``) instead of claiming what it did not see.

``match`` answers under one root. ``resolve`` answers for an order: its ``output/`` tree first, then the order folder
itself, where an order delivered before the output tree existed (legacy); a pattern that already starts with ``output/``
is read from the order folder. The legacy look at the order folder never enters what the runtime keeps there -- the
output tree it has just looked at, ``steps/``, ``inputs/``, ``audit/``, the old ``browser-downloads/`` -- nor takes the
launch's own files (``订单资料.md``, the step table): a ``*.md`` or a ``**`` is a deliverable, not the order's brief or a
step's scratch (C5-2). ``names`` is the same rule without the disk, for paths already found (the gate panel's missing
list, a node trial's record).

The step table frozen at launch (``.order-steps.json``) is read here too, with one validity rule.

Plugin layer. Reads only: nothing here creates, writes or removes anything.
"""
from __future__ import annotations

import fnmatch
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ampuly.core.order_layout import LEGACY_DOWNLOADS, OUTPUT, RESERVED_ORDER_NAMES, STEP_TABLE
from ampuly.plugins.office_ui.artifacts_service import _hidden, multiply_linked, redirected, redirected_entry

# The order's intake the launch writes at the order root (ws_handler._write_order_brief; the kernel points delegated
# work at it as collaboration.ORDER_BRIEF_FILE). Named here, not imported: this module stays clear of the kernel's tools.
ORDER_BRIEF = "订单资料.md"
STEP_TABLE_BYTES = 64 * 1024
# What one pattern may look at, in folder entries, before it answers "there was more".
SCAN_CAP = 5_000
RECORD_SUFFIX = ".prompt.json"

# What reading the step table can find.
TABLE_OK = "ok"
TABLE_MISSING = "missing"
TABLE_INVALID = "invalid"
TABLE_TOO_LARGE = "too_large"

# Why a pattern names nothing at all, whatever is on disk.
OUTSIDE = "outside"
MALFORMED = "malformed"

_MAGIC = frozenset("*?[")
_REPARSE_POINT = 0x400
# How the machine compares names: case-folded on Windows, as they are elsewhere.
_fold = os.path.normcase
# What the legacy look at an order folder leaves alone, at the folder's top: the runtime's areas and the launch's files.
_LEGACY_SKIP = frozenset(_fold(name) for name in (OUTPUT, *RESERVED_ORDER_NAMES, LEGACY_DOWNLOADS, ORDER_BRIEF))


# ── the pattern ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pattern:
    """One declared deliverable, read once."""

    text: str                        # as declared, trimmed, backslashes turned into slashes
    parts: tuple[str, ...]
    folder: bool = False             # ``dir/``: the files directly inside
    problem: str = ""                # "" | OUTSIDE | MALFORMED

    @property
    def wildcard(self) -> bool:
        """Whether it can name more than one file: a folder, or a name with a wildcard in it."""
        return self.folder or any(_magic(part) for part in self.parts)


def _magic(part: str) -> bool:
    return part == "**" or any(char in _MAGIC for char in part)


def parse(pattern: Any) -> Pattern:
    """Read one declared deliverable. Never raises: what cannot name a file inside the root says why."""
    if isinstance(pattern, Pattern):
        return pattern
    text = str(pattern or "").strip().replace("\\", "/")
    folder = text.endswith("/")
    if text.startswith("/"):
        return Pattern(text, (), folder, OUTSIDE)
    if not text or "\x00" in text:
        return Pattern(text, (), folder, MALFORMED)
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    if not parts:
        return Pattern(text, (), folder, MALFORMED)
    if any(part == ".." or ":" in part for part in parts):
        # A parent step, a drive or a stream name: somewhere other than inside the root.
        return Pattern(text, parts, folder, OUTSIDE)
    if any("**" in part and part != "**" for part in parts):
        return Pattern(text, parts, folder, MALFORMED)
    return Pattern(text, parts, folder)


def _hidden_name(name: str) -> bool:
    return _hidden(Path(name))


def _canonical(part: str) -> bool:
    # A name with a space or a dot at either end is another name on Windows; it is never the file asked for.
    return part.strip() == part and part.rstrip(".") == part


def _same(part: str, name: str) -> bool:
    if _magic(part):
        # A name that really carries ASCII brackets (报告[终稿].md) is also named by its own text: ``[`` is a wildcard
        # here, but a file declared under its real name must not read as missing.
        return fnmatch.fnmatchcase(_fold(name), _fold(part)) or ("[" in part and _fold(name) == _fold(part))
    return _fold(name) == _fold(part)


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


# ── the rule without the disk ────────────────────────────────────────────────


def names(pattern: Any, relative: str) -> bool:
    """Whether ``pattern`` names this path (relative to the same root, as a listing already found it).

    The same rule as ``match`` except for what only the disk can tell (a link, a folder that is really a file): the
    paths handed in are ones a bounded, link-free listing already found.
    """
    spec = parse(pattern)
    if spec.problem:
        return False
    path = tuple(part for part in str(relative or "").replace("\\", "/").split("/") if part not in ("", "."))
    if not path or any(_hidden_name(part) for part in path):
        return False
    if spec.folder:
        if _fold(path[-1]).endswith(RECORD_SUFFIX):
            return False
        return _parts_match((*spec.parts, "*"), path)
    if not spec.wildcard and not all(_canonical(part) for part in spec.parts):
        return False
    return _parts_match(spec.parts, path)


def _parts_match(wanted: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not wanted:
        return not path
    head = wanted[0]
    if head == "**":
        # Any number of folders, never the file itself.
        return any(_parts_match(wanted[1:], path[skip:]) for skip in range(len(path)))
    if not path or not _same(head, path[0]):
        return False
    return _parts_match(wanted[1:], path[1:])


def named_by_any(pattern: Any, paths: Iterable[str]) -> bool:
    return any(names(pattern, path) for path in paths)


# ── the rule on the disk ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Match:
    """The files one pattern names under one root."""

    pattern: Pattern
    root: Path
    files: tuple[str, ...] = ()      # relative to ``root``, POSIX, in the order a listing sorts them
    truncated: bool = False          # the scan ran out: there may be more than ``files``
    where: tuple[str, ...] = ()      # ``root`` below the order folder, for ``resolve`` (``("output",)`` or ``()``)

    @property
    def problem(self) -> str:
        return self.pattern.problem

    @property
    def paths(self) -> list[Path]:
        return [self.root.joinpath(*relative.split("/")) for relative in self.files]


class _Scan:
    """What one pattern may still look at: its own cap, and the caller's budget when it has one."""

    def __init__(self, cap: int, budget: Any = None) -> None:
        self.cap = max(0, int(cap))
        self.budget = budget
        self.seen = 0
        self.stopped = False

    def take(self) -> bool:
        if self.stopped:
            return False
        self.seen += 1
        if self.seen > self.cap or (self.budget is not None and not self.budget.take()):
            self.stopped = True
        return not self.stopped


def _plain_folder(path: Path) -> bool:
    try:
        return path.is_dir() and not redirected(path)
    except OSError:
        return False


def match(root: Any, pattern: Any, *, cap: int = SCAN_CAP, budget: Any = None) -> Match:
    """The files ``pattern`` names under exactly this root. Never raises; a root that is not a plain folder has none."""
    return _match(root, parse(pattern), cap, budget, frozenset())


def _match(root: Any, spec: Pattern, cap: int, budget: Any, skip: frozenset[str]) -> Match:
    """``match``, leaving alone the names in ``skip`` (folded) at the root's top."""
    base = Path(root)
    if spec.problem or not str(root or "").strip() or not _plain_folder(base):
        return Match(spec, base)
    scan = _Scan(cap, budget)
    found: dict[tuple[str, ...], None] = {}
    if spec.wildcard:
        _walk(base, (), 0, spec, scan, found, skip)
    elif _fold(spec.parts[0]) not in skip:
        _look_up(base, spec.parts, scan, found)
    ordered = sorted(found, key=lambda parts: tuple(_fold(part) for part in parts))
    return Match(spec, base, tuple("/".join(parts) for parts in ordered), scan.stopped)


def resolve(order_root: Any, pattern: Any, *, cap: int = SCAN_CAP, budget: Any = None) -> Match:
    """The files ``pattern`` names for one order: its ``output/`` tree first, then the order folder (legacy).

    A pattern that starts with ``output/`` names a path from the order folder. With nothing found anywhere, the answer
    is the output tree's (empty) one, so a caller that shows where the file belongs shows ``output/``. The legacy look at
    the order folder leaves the runtime's areas and the launch's own files alone (``_LEGACY_SKIP``).
    """
    spec = parse(pattern)
    base = Path(order_root)
    if spec.problem or not _plain_folder(base):
        # Nothing is named inside an order folder that is not there, or is reached through a link.
        return Match(spec, base / OUTPUT, where=(OUTPUT,))
    if _fold(spec.parts[0]) == _fold(OUTPUT):
        return match(base, spec, cap=cap, budget=budget)
    delivered = match(base / OUTPUT, spec, cap=cap, budget=budget)
    delivered = Match(delivered.pattern, delivered.root, delivered.files, delivered.truncated, (OUTPUT,))
    if delivered.files or delivered.truncated:
        return delivered
    legacy = _match(base, spec, cap, budget, _LEGACY_SKIP)
    return legacy if (legacy.files or legacy.truncated) else delivered


def _look_up(base: Path, parts: tuple[str, ...], scan: _Scan, found: dict[tuple[str, ...], None]) -> None:
    """A plain path is looked up by name, component by component, the way a file is opened -- without following one."""
    current = base
    for index, part in enumerate(parts):
        if _hidden_name(part) or not _canonical(part) or not scan.take():
            return
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return
        if _linked(info):
            return
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                return
        elif not stat.S_ISREG(info.st_mode) or int(info.st_nlink) != 1:
            return
    found[parts] = None


def _entries(folder: Path, scan: _Scan) -> list[os.DirEntry]:
    """The folder's own entries, hidden names and links left out, within the scan's allowance."""
    kept: list[os.DirEntry] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not scan.take():
                    break
                if _hidden_name(entry.name) or redirected_entry(entry):
                    continue
                kept.append(entry)
    except OSError:
        pass
    return kept


def _is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file(follow_symlinks=False) and not multiply_linked(Path(entry.path))
    except OSError:
        return False


def _is_folder(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _walk(folder: Path, rel: tuple[str, ...], index: int, spec: Pattern, scan: _Scan,
          found: dict[tuple[str, ...], None], skip: frozenset[str] = frozenset()) -> None:
    """Names are read from the folder's own listing, so a match carries the name as it is on disk. ``skip`` (folded
    names) is left alone at the root's top only."""
    if scan.stopped:
        return
    parts = spec.parts

    def listing() -> list[os.DirEntry]:
        entries = _entries(folder, scan)
        return [entry for entry in entries if _fold(entry.name) not in skip] if skip and not rel else entries

    if index == len(parts):
        if spec.folder:
            for entry in listing():
                if _is_file(entry) and not _fold(entry.name).endswith(RECORD_SUFFIX):
                    found[(*rel, entry.name)] = None
        return  # a file pattern that ran out of names here names a folder, not a file
    part = parts[index]
    if part == "**":
        _walk(folder, rel, index + 1, spec, scan, found, skip)
        for entry in listing():
            if _is_folder(entry):
                _walk(Path(entry.path), (*rel, entry.name), index, spec, scan, found, skip)
        return
    last = index == len(parts) - 1 and not spec.folder
    for entry in listing():
        if not _same(part, entry.name):
            continue
        if last:
            if _is_file(entry):
                found[(*rel, entry.name)] = None
        elif _is_folder(entry):
            _walk(Path(entry.path), (*rel, entry.name), index + 1, spec, scan, found, skip)


def folder_of(pattern: Any) -> str:
    """The folder a deliverable's files go in, as far as it is spelled out: ``board/*.png`` and ``board/`` give
    ``board/``, ``brief/需求单.md`` gives ``brief/``; "" when no folder before the file is a plain name."""
    spec = parse(pattern)
    if spec.problem:
        return ""
    lead: list[str] = []
    for part in spec.parts if spec.folder else spec.parts[:-1]:
        if _magic(part):
            break
        lead.append(part)
    return "/".join(lead) + "/" if lead else ""


# ── the step table ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepTable:
    """What the order's frozen step table says, or why it says nothing."""

    state: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == TABLE_OK

    @property
    def steps(self) -> list[dict[str, Any]]:
        raw = self.data.get("steps")
        return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def read_step_table(order_root: Any, order_id: str) -> StepTable:
    """The step table frozen when the order was launched, under one rule: a plain file with one name, no larger than
    ``STEP_TABLE_BYTES``, a JSON object of schema 1 naming this order. Never raises."""
    folder = Path(order_root)
    path = folder / STEP_TABLE
    if not _plain_folder(folder):
        return StepTable(TABLE_MISSING)  # no order folder, or one reached through a link
    try:
        info = os.lstat(path)
    except OSError:
        return StepTable(TABLE_MISSING)
    if _linked(info) or not stat.S_ISREG(info.st_mode):
        return StepTable(TABLE_MISSING)  # a link, or a folder by that name, is not this order's table
    if int(info.st_nlink) != 1:
        return StepTable(TABLE_INVALID)  # a second name can carry a file in from somewhere else
    if int(info.st_size) > STEP_TABLE_BYTES:
        return StepTable(TABLE_TOO_LARGE)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return StepTable(TABLE_MISSING)
    try:
        opened = os.fstat(fd)
        # What was opened is what was looked at: still a plain file with one name, the same size, written at the same
        # moment. (Not the file id: Windows may spell it two ways between a look by name and one by handle.)
        if (not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1
                or (opened.st_size, opened.st_mtime_ns) != (info.st_size, info.st_mtime_ns)):
            return StepTable(TABLE_INVALID)  # swapped between the look and the open
        chunks: list[bytes] = []
        read = 0
        while read <= STEP_TABLE_BYTES:
            chunk = os.read(fd, min(65536, STEP_TABLE_BYTES + 1 - read))
            if not chunk:
                break
            chunks.append(chunk)
            read += len(chunk)
    except OSError:
        return StepTable(TABLE_MISSING)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > STEP_TABLE_BYTES:
        return StepTable(TABLE_TOO_LARGE)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return StepTable(TABLE_INVALID)
    if not isinstance(data, dict):
        return StepTable(TABLE_INVALID)
    schema = data.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
        return StepTable(TABLE_INVALID)
    if not isinstance(data.get("order_id"), str) or data["order_id"] != str(order_id or ""):
        return StepTable(TABLE_INVALID)
    return StepTable(TABLE_OK, data)


def deliverables_of(step: Any) -> list[str]:
    """What one step (a table row or a package node) declares it delivers, as non-empty strings."""
    if not isinstance(step, dict):
        return []
    raw = step.get("deliverables")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [text for item in raw if not isinstance(item, (dict, list, tuple)) and item is not None
            and (text := str(item).strip())]


def step_deliverables(steps: Iterable[Any] | None, step_id: str) -> list[str]:
    """The deliverables of the step with this id; [] when there is none."""
    wanted = str(step_id or "")
    for step in list(steps or []):
        if isinstance(step, dict) and str(step.get("id", "") or "") == wanted:
            return deliverables_of(step)
    return []


def last_deliverables(steps: Iterable[Any] | None) -> tuple[str, list[str]]:
    """``(step name, deliverables)`` of the last step that names deliverables.

    A flow's last step is often a person's review that names none: what is handed over is then the deliverables of
    the last step that names some. When no step names any, the last step's name and no deliverables.
    """
    name, deliverables = "", []
    for node in list(steps or []):
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "") or node.get("id", "") or "").strip()
        found = deliverables_of(node)
        if found:
            name, deliverables = node_name, found
        elif not deliverables and node_name:
            name = node_name
    return name, deliverables
