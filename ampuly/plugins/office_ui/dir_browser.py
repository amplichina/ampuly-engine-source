"""Letting the owner point at a folder on their own machine, and refusing the ones a project may not live in.

This is the first code in the product that looks outside a project's own work area, so it is deliberately narrow:
it lists **directory names only**, never files; it creates a directory only when asked to; and the only thing it
ever writes by itself is a probe file it removes again.

Two checks, not one, and the difference matters:

* :func:`browse_check` is what *looking* needs. A drive root, a very long path, a folder with an odd name -- all
  fine to look inside.
* :func:`check_target` is what *choosing* needs, and it is the whole rule: never a drive root, never through a
  link, never inside the system directory or the product, never overlapping another project, never a folder that
  already holds something, and never a place that cannot actually be written to.

Every refusal is a code. A reply carries absolute paths, because the owner asked where things are; it is owner-only
and this-machine-only at the socket (the same rule the folder-opening action uses).
"""
from __future__ import annotations

import os
import re
import string
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ampuly.core.project_paths import Mapping, linkless
from ampuly.core.project_paths import target_check as _lexical_target_check
from ampuly.plugins.office_ui.artifacts_service import redirected, redirected_entry

SCAN_CAP = 2_000
PAGE = 500
TARGET_PATH_CHARS = 150
LONG_PATH_WARN_CHARS = 100
PROBE_PREFIX = ".ampuly-write-probe"
_NAME = re.compile(r'^[^\x00-\x1f<>:"/\\|?*]{1,64}$')
_CONTROL = {chr(code) for code in range(0, 32)} | {"\x7f"}
# What Windows keeps for devices; a folder with one of these names cannot be addressed properly.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_DRIVE_KINDS = {0: "unknown", 1: "unknown", 2: "removable", 3: "fixed", 4: "remote", 5: "cdrom", 6: "ram"}


class DirRefused(Exception):
    """Something the browser will not do. ``code`` is what the console shows; no path, no OS text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code or "invalid_path")


@dataclass(frozen=True)
class Check:
    """Whether a folder may be a project's own, and what is worth saying about it either way."""

    code: str = ""
    warn: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.code


def product_root() -> Path:
    from ampuly.core.project_paths import _product_root

    return _product_root()


# ── The two checks ───────────────────────────────────────────────────────────────────────────────────────────────

def browse_check(path: str | Path, *, home: Path) -> str:
    """Why this folder may not even be looked into, or ``""``.

    Weaker than :func:`check_target` on purpose: the owner may look into a place no project may live. A drive root
    is where a fresh disk starts, so refusing it here would make every drive a dead end.
    """
    text = str(path or "")
    if not text.strip():
        return "invalid_path"
    if any(char in text for char in _CONTROL):
        return "invalid_path"
    if text.startswith("\\\\") or text.startswith("//"):
        return "network_path"
    candidate = Path(text)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return "invalid_path"
    if _inside(candidate, Path(home)):
        return "inside_ampuly_home"
    if _inside(candidate, product_root()):
        return "inside_product_repo"
    return ""


def target_lexical(path: str | Path, *, home: Path, allow: Path | None = None) -> str:
    """The disk-free half of the choosing rule: shape, length, devices, and the two places nothing may live."""
    code = _lexical_target_check(Path(str(path)), home=Path(home), product_root=product_root(), allow=allow)
    if code:
        return code
    for part in Path(str(path)).parts[1:]:
        if part.split(".", 1)[0].upper() in _RESERVED:
            return "invalid_path"
    return ""


def _inside(candidate: Path, parent: Path) -> bool:
    """``candidate`` is ``parent`` or sits under it -- by name, no disk access."""
    left = os.path.normcase(os.path.abspath(str(candidate)))
    right = os.path.normcase(os.path.abspath(str(parent)))
    return left == right or left.startswith(right.rstrip("\\/") + os.sep)


def _overlaps(candidate: Path, other: Path) -> bool:
    """Either one contains the other. Both directions: a parent of a project's folder is as bad as a child."""
    return _inside(candidate, other) or _inside(other, candidate)


def _same_dir(left: Path, right: Path) -> bool:
    """The same directory on disk, whatever it is spelled like -- Windows says ``D:\\Proj`` is ``D:\\proj``."""
    try:
        return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))
    except OSError:
        return False


# ── Looking ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _drive_kind(letter: str) -> str:
    if os.name != "nt":
        return "fixed"
    try:
        import ctypes

        return _DRIVE_KINDS.get(int(ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")), "unknown")
    except Exception:  # noqa: BLE001 - a drive whose kind cannot be read is still listable
        return "unknown"


def drives() -> list[dict[str, Any]]:
    """The top level of the chooser: what this machine has, plus the owner's home folder."""
    rows: list[dict[str, Any]] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:\\")
            if not root.exists():
                continue
            kind = _drive_kind(letter)
            rows.append({"name": f"{letter}:", "path": str(root), "kind": kind,
                         "refused": "network_path" if kind == "remote" else ""})
    else:
        rows.append({"name": "/", "path": "/", "kind": "fixed", "refused": ""})
    rows.append({"name": "用户目录", "path": str(Path.home()), "kind": "home", "refused": ""})
    return rows


def list_dirs(path: str, *, home: Path, offset: int = 0, page: int = PAGE) -> dict[str, Any]:
    """One folder's subdirectories. Files are never returned; nothing is hidden, but what is refused says so."""
    code = browse_check(path, home=home)
    if code:
        raise DirRefused(code)
    folder = Path(str(path))
    if redirected(folder):
        raise DirRefused("refused_link")
    if not folder.is_dir():
        raise DirRefused("not_found")
    rows: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(folder) as entries:
            for seen, entry in enumerate(entries):
                if seen >= SCAN_CAP:
                    truncated = True
                    break
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child = Path(entry.path)
                rows.append({
                    "name": entry.name,
                    "path": str(child),
                    "link": redirected_entry(entry),
                    "writable": os.access(child, os.W_OK),
                    "refused": browse_check(child, home=home),
                })
    except PermissionError as exc:
        raise DirRefused("not_readable") from exc
    except OSError as exc:
        raise DirRefused("not_readable") from exc
    rows.sort(key=lambda row: str(row["name"]).casefold())
    start = max(0, int(offset))
    shown = rows[start:start + max(1, int(page))]
    parent = folder.parent
    return {
        "kind": "dir",
        "path": str(folder),
        # At a drive root there is nowhere up but the list of drives, which the console shows on its own.
        "parent": "" if parent == folder else str(parent),
        "entries": shown,
        "next_offset": (start + len(shown)) if (start + len(shown)) < len(rows) else 0,
        "truncated": truncated,
        "can_create": os.access(folder, os.W_OK),
    }


# ── Choosing ─────────────────────────────────────────────────────────────────────────────────────────────────────

def check_target(path: str, *, for_scope: str, project_id: str, home: Path, mapping: Mapping,
                 default_root: Path) -> Check:
    """Whether this folder may be a project's own (or the root new projects go under), and why not.

    The order is the point: the cheap refusals come before the ones that touch the disk, and the probe -- the only
    thing that writes -- is last, so a folder that was going to be refused anyway is never written to.
    """
    target = Path(str(path))
    project = str(project_id or "").strip()
    scope = "default" if for_scope == "default" else "project"
    own_default = (Path(default_root) / project) if project else None
    own_mapped = mapping.pinned(project) if project else None

    # 1-2, 5-6: shape, devices, the network, and the two places nothing may live (with this project's own
    # current answer exempted -- on a local install it is inside both).
    exempt = own_default if scope == "project" else None
    code = target_lexical(target, home=home, allow=exempt)
    if code:
        return Check(code)
    # The lexical half only knows "inside"; a folder that *contains* the system directory or the product is just
    # as impossible, and deserves the same word for it.
    if not (exempt is not None and _same_dir(target, exempt)):
        if _inside(Path(home), target):
            return Check("inside_ampuly_home")
        if _inside(product_root(), target):
            return Check("inside_product_repo")
    # 3: never through a link.
    if not linkless(target):
        return Check("refused_link")
    # 4 is inside target_lexical (drive_root).

    if scope == "default" and _same_dir(target, Path(default_root)):
        return Check("")   # the root it already is: nothing to do, and nothing to refuse

    # 7: never overlapping anything else that matters. Both directions, so a parent of a project's folder is
    # refused as firmly as a child of one. In the default scope there is no project being moved, so **every**
    # project's folder counts -- including the one whose id the request happens to carry.
    others: list[Path] = [Path(home), product_root(), Path(default_root)]
    for other_id, other_path in mapping.projects.items():
        if scope == "default" or other_id != project:
            others.append(Path(other_path))
    for other_id in _existing_project_names(Path(default_root)):
        if scope == "default" or other_id != project:
            others.append(Path(default_root) / other_id)
    back_to_default = scope == "project" and own_default is not None and _same_dir(target, own_default)
    if not back_to_default:
        for other in others:
            if scope == "project" and own_mapped is not None and _same_dir(other, own_mapped):
                continue
            if scope == "project" and own_default is not None and _same_dir(other, own_default):
                continue
            if scope == "default" and _same_dir(other, Path(default_root)) and _same_dir(target, Path(default_root)):
                continue   # setting the root to the root it already is changes nothing
            if _overlaps(target, other):
                return Check("inside_project_area")

    if scope == "project":
        # 7': under the shared root only this project's own folder is allowed -- that one is how a project goes
        # back to the default location.
        if not back_to_default and _inside(target, Path(default_root)):
            return Check("inside_project_area")
        # 7''': the folder it already uses, in any spelling, or one inside it.
        current = own_mapped or own_default
        if current is not None and (_same_dir(target, current) or _overlaps(target, current)):
            return Check("same_folder")
        # 8: a project is pointed at an empty folder, so an import can never collide with what is already there.
        if target.is_dir():
            try:
                if next(os.scandir(target), None) is not None:
                    return Check("target_not_empty")
            except OSError:
                return Check("not_readable")
    else:
        # 7'': a root that already holds a folder named like a project this console has would swallow it -- that
        # project would resolve to the owner's own folder. A folder merely *shaped* like a project id ("notes")
        # is nobody's, and refusing it would make most ordinary folders unusable as a root.
        known = set(mapping.projects) | set(_existing_project_names(Path(default_root)))
        taken = tuple(name for name in _existing_project_names(target) if name in known)[:20]
        if taken:
            return Check("root_has_project_names", names=taken)

    warn: list[str] = []
    from ampuly.plugins.office_ui.data_actions import inside_system_dir

    if inside_system_dir(target):
        warn.append("sync_folder")
    if len(str(target)) > LONG_PATH_WARN_CHARS:
        warn.append("long_path")
    current = own_mapped or own_default
    if current is not None and Path(target).anchor.lower() != Path(current).anchor.lower():
        warn.append("different_volume")
    if os.name == "nt" and _drive_kind(str(target)[:1]) == "removable":
        warn.append("removable_drive")

    # 9: the only check that writes. It removes everything it made.
    probe = _probe(target)
    if probe:
        return Check(probe, warn=tuple(warn))
    return Check("", warn=tuple(warn))


def _existing_project_names(root: Path) -> list[str]:
    """Directory names under ``root`` that are valid project ids -- the shape a shared root has."""
    from ampuly.project_id import is_valid_project_id

    found: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False) and is_valid_project_id(entry.name):
                        found.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(found)


def _probe(target: Path) -> str:
    """Prove the folder can actually be written to. Returns a refusal code, or ``""``; leaves nothing behind."""
    made: list[Path] = []
    folder = Path(target)
    try:
        if not folder.exists():
            current = folder
            while not current.exists() and current.parent != current:
                made.append(current)
                current = current.parent
            folder.mkdir(parents=True, exist_ok=True)
        name = folder / f"{PROBE_PREFIX}-{uuid.uuid4().hex}"
        name.write_bytes(b"ok")
        if name.read_bytes() != b"ok":
            return "not_writable"
        name.unlink()
    except OSError:
        return "not_writable"
    finally:
        for path in made:  # deepest first, and only what this probe created
            try:
                path.rmdir()
            except OSError:
                break
    return ""


# ── Asking Windows to ask ────────────────────────────────────────────────────────────────────────────────────────

PICK_TIMEOUT_SEC = 300

# The title and the starting folder travel as environment variables rather than inside the command line: a path
# is data, and the surest way to keep it data is never to put it where a parser can read it as anything else.
#
# FolderBrowserDialog, not Shell.Application.BrowseForFolder: that call's fourth argument is vRootFolder -- the
# TOP of the tree, above which the owner cannot browse -- not an initial selection. Passing the project's own
# folder there rooted the dialog inside it, so every folder the owner could reach was one check_target refuses,
# and 「更换文件夹」 was a dead end. SelectedPath is the parameter that actually means 「start here」.
#
# The answer comes back base64-encoded, and that is not belt-and-braces. Windows PowerShell writes a redirected
# stdout in the machine's OEM code page whatever `[Console]::OutputEncoding` is set to -- measured here, not
# assumed -- so a folder whose name that page cannot spell would arrive as 「?」 and be refused for a reason that
# is not the real one. Base64 is ASCII on the wire, so no code page can touch it.
_PICK_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.Description = $env:AMPULY_PICK_TITLE;"
    "$d.ShowNewFolderButton = $true;"
    "if ($env:AMPULY_PICK_START -and (Test-Path -LiteralPath $env:AMPULY_PICK_START)) "
    "{ $d.SelectedPath = $env:AMPULY_PICK_START };"
    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ Write-Output ([Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($d.SelectedPath))) }"
)


def pick_folder(initial: str = "", *, title: str = "选择文件夹") -> str:
    """Ask the machine's own folder dialog, and return what the owner picked (``""`` when they cancelled).

    The console runs on the owner's machine, so the dialog opens in front of them -- they browse anywhere on the
    machine, create a folder and pick it with the file manager they already know, and nothing here has to imitate
    it. Only the path comes back, and it is then held to exactly the same rules a typed one would be.
    """
    import base64
    import subprocess

    if os.name != "nt":
        raise DirRefused("unsupported")
    env = dict(os.environ)
    env["AMPULY_PICK_START"] = str(initial or "")
    env["AMPULY_PICK_TITLE"] = str(title or "")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", _PICK_SCRIPT],
            capture_output=True, text=True, timeout=PICK_TIMEOUT_SEC, env=env,
            encoding="ascii", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise DirRefused("pick_timed_out") from exc
    except OSError as exc:
        raise DirRefused("unsupported") from exc
    if done.returncode != 0:
        raise DirRefused("unsupported")
    lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    if not lines:
        return ""
    try:
        return base64.b64decode(lines[-1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        # Whatever came back is not what this script writes. Refusing is the honest answer; guessing at a path
        # and then creating a project folder from the guess is not.
        raise DirRefused("unsupported") from exc


# ── Creating ─────────────────────────────────────────────────────────────────────────────────────────────────────

def create_dir(parent: str, name: str, *, home: Path) -> dict[str, Any]:
    """新建文件夹, under a folder the owner is looking at. The only place this module makes a directory."""
    code = browse_check(parent, home=home)
    if code:
        raise DirRefused(code)
    holder = Path(str(parent))
    if redirected(holder):
        raise DirRefused("refused_link")
    if not holder.is_dir():
        raise DirRefused("not_found")
    clean = str(name or "")
    if (not _NAME.fullmatch(clean) or clean in {".", ".."} or clean.strip() != clean
            or clean.rstrip(".") != clean or clean.split(".", 1)[0].upper() in _RESERVED):
        raise DirRefused("invalid_name")
    made = holder / clean
    try:
        made.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise DirRefused("name_taken") from exc
    except OSError as exc:
        raise DirRefused("not_writable") from exc
    if redirected(made):
        # Something replaced it between the call and the check.
        try:
            made.rmdir()
        except OSError:
            pass
        raise DirRefused("refused_link")
    return {"name": clean, "path": str(made), "link": False,
            "writable": os.access(made, os.W_OK), "refused": browse_check(made, home=home)}
