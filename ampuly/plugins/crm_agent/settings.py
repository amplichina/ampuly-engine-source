"""Per-project CRM enablement and data-admission mode (owner-only, persisted).

CRM is OFF unless an owner explicitly enables it for one project. The file
``<project>/plugins/crm/settings.json`` is the only persisted switch; an
absent file means disabled. Admission defaults to ``synthetic_only`` and the
backend enforces it on every save (see ``contracts.require_synthetic``).

Real-data admission is closed in this build. It would need BOTH an owner-set
flag outside the wire protocol (``<AMPULY_HOME>/plugins/crm/real-data-admission/
<project>.json``; no action writes it) AND ``REAL_DATA_PREREQUISITES_MET``,
which stays False until backups are encrypted, expire and restore re-applies
erasure tombstones (L4). No wire request can switch real data on.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .contracts import canonical, fail, now, text

SETTINGS_FILE = "settings.json"
ADMISSION_SYNTHETIC = "synthetic_only"
ADMISSION_REAL = "real"
REAL_DATA_PREREQUISITES_MET = False


@dataclass(frozen=True)
class CRMSettings:
    enabled: bool = False
    admission: str = ADMISSION_SYNTHETIC
    revision: int = 0
    updated_by: str = ""
    updated_at: str = ""

    def public(self) -> dict:
        return {"enabled": self.enabled, "admission": self.admission, "revision": self.revision,
                "updated_by": self.updated_by, "updated_at": self.updated_at,
                "real_data_admission": "closed"}


def _safe(path: Path, *, is_file: bool = True) -> None:
    for part in (*reversed(path.parents), path):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            fail("settings_unavailable")
        if part == path and is_file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            fail("settings_unavailable")


def real_data_flag(ampuly_home: Path, project_id: str) -> Path:
    return Path(ampuly_home) / "plugins" / "crm" / "real-data-admission" / f"{project_id}.json"


def read_settings(directory: Path) -> CRMSettings:
    path = Path(directory) / SETTINGS_FILE
    _safe(path)
    if not path.exists():
        return CRMSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("settings_invalid")
    if (type(data) is not dict or set(data) != {"enabled", "admission", "revision", "updated_by", "updated_at"}
            or type(data["enabled"]) is not bool or data["admission"] not in (ADMISSION_SYNTHETIC, ADMISSION_REAL)
            or type(data["revision"]) is not int or data["revision"] < 0
            or type(data["updated_by"]) is not str or type(data["updated_at"]) is not str):
        fail("settings_invalid")
    return CRMSettings(**data)


def admit_real_data(ampuly_home: Path, project_id: str) -> None:
    """Raise unless real data may be admitted. Always raises in this build."""
    if not real_data_flag(ampuly_home, project_id).is_file():
        fail("real_data_admission_closed")
    if not REAL_DATA_PREREQUISITES_MET:
        fail("real_data_admission_closed")


def write_settings(directory: Path, *, enabled, admission, expected_revision, actor, ampuly_home, project_id) -> CRMSettings:
    if type(enabled) is not bool or type(expected_revision) is not int:
        fail("input_invalid")
    if admission not in (ADMISSION_SYNTHETIC, ADMISSION_REAL):
        fail("input_invalid")
    actor = text(actor, limit=128, required=True)
    if admission == ADMISSION_REAL:
        admit_real_data(ampuly_home, project_id)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _safe(directory, is_file=False)
    current = read_settings(directory)
    if expected_revision != current.revision:
        fail("settings_revision_conflict")
    updated = CRMSettings(enabled=enabled, admission=admission, revision=current.revision + 1,
                          updated_by=actor, updated_at=now())
    path = directory / SETTINGS_FILE
    temp = directory / f".{SETTINGS_FILE}.{uuid4().hex}.tmp"
    try:
        temp.write_text(canonical({"enabled": updated.enabled, "admission": updated.admission,
                                   "revision": updated.revision, "updated_by": updated.updated_by,
                                   "updated_at": updated.updated_at}), encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return updated
