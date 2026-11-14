"""C1: Task Space artifact layout for capture / receipt.

Directory roles (Stage C of the hybrid browser plan):

- accounts / browser profiles = identity
  Login cookies and grants live under AMPULY_HOME/browser/accounts and
  AMPULY_HOME/browser/profiles (or a dedicated Ego profile). They outlive a task.
- spaces = task lease + evidence
  BrowserSpaces Task Space directories hold captures, per-round receipts, and
  scripts. One open space per (profile_id, task_id); evidence stays with the task.

EgoBackend already writes captures/ and receipt-NNNN.json under its Space.
This module is the shared layout for the built-in PlaywrightBackend path so
both legs land evidence in the same relative places under a Task Space.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ampuly.layer4_tools.browser_spaces import BrowserSpaces, Space

# Relative layout under Space.directory (kept in sync with EgoBackend).
CAPTURES_SUBDIR = "captures"
RECEIPT_PREFIX = "receipt-"


@dataclass
class SpaceArtifactLayout:
    """Capture/receipt sink bound to one BrowserSpaces Task Space."""

    space: Space
    _round: int = field(default=0, repr=False)

    @property
    def directory(self) -> Path:
        return Path(self.space.directory)

    @property
    def captures_dir(self) -> Path:
        return self.directory / CAPTURES_SUBDIR

    @property
    def space_id(self) -> str:
        return self.space.space_id

    def ensure_dirs(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.captures_dir.mkdir(parents=True, exist_ok=True)

    def next_capture_path(self, *, suffix: str = ".png") -> Path:
        self.ensure_dirs()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        return self.captures_dir / f"{int(time.time() * 1000)}{suffix}"

    def resolve_capture_path(self, requested: str = "") -> Path:
        """Map a capture target into this Space (absolute paths kept as-is)."""
        self.ensure_dirs()
        raw = (requested or "").strip()
        if not raw:
            return self.next_capture_path()
        path = Path(raw)
        if path.is_absolute():
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        # Relative with a suffix -> under space root; bare name -> captures/
        if path.suffix:
            target = self.directory / path
        else:
            target = self.captures_dir / f"{path.name}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_receipt(self, payload: Mapping[str, Any]) -> Path:
        """Append receipt-NNNN.json under the Space (Ego-compatible naming)."""
        self.ensure_dirs()
        self._round += 1
        path = self.directory / f"{RECEIPT_PREFIX}{self._round:04d}.json"
        body = dict(payload)
        body.setdefault("round", self._round)
        body.setdefault("space_id", self.space.space_id)
        body.setdefault("profile_id", self.space.profile_id)
        body.setdefault("task_id", self.space.task_id)
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


def open_space_artifacts(
    spaces: BrowserSpaces,
    profile_id: str,
    task_id: str,
) -> SpaceArtifactLayout:
    """Reuse an open space for the task, or open a new one."""
    open_spaces = [
        s
        for s in spaces.list_spaces(profile_id=profile_id)
        if s.task_id == task_id and s.state == "open"
    ]
    space = open_spaces[0] if open_spaces else spaces.open_space(profile_id, task_id)
    layout = SpaceArtifactLayout(space=space)
    layout.ensure_dirs()
    return layout


def resolve_builtin_spaces_root(env: Mapping[str, str] | None = None) -> Path | None:
    """Spaces root for built-in (and shared) Task Spaces.

    Order: AMPULY_BROWSER_SPACES_ROOT -> AMPULY_EGO_SPACES_ROOT ->
    AMPULY_HOME/browser/spaces. Returns None when none are set (caller may skip
    Space binding; Playwright still works with an explicit artifact_dir).
    """
    source = env if env is not None else os.environ
    for key in ("AMPULY_BROWSER_SPACES_ROOT", "AMPULY_EGO_SPACES_ROOT"):
        raw = str(source.get(key) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    ampuly_home = str(source.get("AMPULY_HOME") or "").strip()
    if ampuly_home:
        return (Path(ampuly_home).expanduser() / "browser" / "spaces").resolve()
    return None


def describe_directory_roles() -> dict[str, str]:
    """Diagnostic helper: accounts = identity; spaces = task evidence."""
    return {
        "accounts": "identity (login cookies, grants) under AMPULY_HOME/browser/accounts|profiles",
        "spaces": "task lease + evidence (captures, receipts) under BrowserSpaces root",
        "captures_subdir": CAPTURES_SUBDIR,
        "receipt_prefix": RECEIPT_PREFIX,
    }
