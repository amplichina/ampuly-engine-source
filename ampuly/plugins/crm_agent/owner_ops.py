"""Owner-triggerable CRM real-data gate helpers (engine-owned; no external agent).

Parallel to ``ampuly browser shop-home``: Owner can ensure backup-key custody
layout and run a throwaway restore+tombstone drill from Desktop/CLI without
Grok driving each step.

NEVER logs, prints, or returns key material. Receipts only report presence,
byte length, and drill outcomes.
"""
from __future__ import annotations

import json
import os
from ampuly.core.sqlite_open import opened as open_sqlite
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .backup_crypto import (
    key_path,
    load_or_create_project_key,
    open_crm_tree_in_place,
    seal,
)
from .contracts import Scope
from .settings import REAL_DATA_PREREQUISITES_MET, real_data_flag
from .store import CRMStore

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_DRILL_PROJECT = "crm-drill-synthetic"
CUSTODY_DOC_NAME = "CUSTODY.md"
OWNER_CUSTODY_ACK_NAME = "owner-custody-ack.json"
RESTORE_DRILL_LATEST_NAME = "restore-drill-latest.json"
CLEAN_MACHINE_SPOTCHECK_NAME = "clean-machine-spotcheck.json"
SAME_MACHINE_STRICT_SPOTCHECK_NAME = "same-machine-strict-spotcheck.json"
ACK_TEXT = (
    "Owner acknowledges: lost backup key = sealed CRM backups cannot be decrypted "
    "(intentional). Keys never enter chat, logs, git, or receipts."
)


def _now_shanghai() -> str:
    return datetime.now(tz=SHANGHAI).isoformat(timespec="seconds")


def _ampuly_home(env: Mapping[str, str] | None = None) -> Path:
    source = dict(env) if env is not None else dict(os.environ)
    raw = (source.get("AMPULY_HOME") or "").strip()
    if not raw:
        raise ValueError("AMPULY_HOME_required")
    return Path(raw)


def backup_keys_dir(ampuly_home: Path) -> Path:
    return Path(ampuly_home) / "plugins" / "crm" / "backup-keys"


def real_data_admission_dir(ampuly_home: Path) -> Path:
    return Path(ampuly_home) / "plugins" / "crm" / "real-data-admission"


def custody_ack_path(ampuly_home: Path) -> Path:
    return real_data_admission_dir(ampuly_home) / OWNER_CUSTODY_ACK_NAME


def restore_drill_latest_path(ampuly_home: Path) -> Path:
    return real_data_admission_dir(ampuly_home) / RESTORE_DRILL_LATEST_NAME


def clean_machine_spotcheck_path(ampuly_home: Path) -> Path:
    return real_data_admission_dir(ampuly_home) / CLEAN_MACHINE_SPOTCHECK_NAME



def same_machine_strict_spotcheck_path(ampuly_home: Path) -> Path:
    return real_data_admission_dir(ampuly_home) / SAME_MACHINE_STRICT_SPOTCHECK_NAME

def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_custody_ack(ampuly_home: Path) -> dict[str, Any] | None:
    data = _read_json_file(custody_ack_path(ampuly_home))
    if not data:
        return None
    if data.get("kind") != "crm.owner_custody_ack":
        return None
    if data.get("acknowledged") is not True:
        return None
    if data.get("contains_key_material") is True:
        return None
    return data


def record_custody_ack(
    ampuly_home: Path,
    *,
    phrase: str = "确认密钥保管",
    source: str = "owner_chat",
    acknowledged_at: str | None = None,
) -> dict[str, Any]:
    """Persist Owner written ACK that lost key = backups unreadable. No key bytes."""
    ensure_backup_keys_layout(ampuly_home)
    admission = real_data_admission_dir(ampuly_home)
    admission.mkdir(parents=True, exist_ok=True)
    out = custody_ack_path(ampuly_home)
    when = acknowledged_at or _now_shanghai()
    payload = {
        "kind": "crm.owner_custody_ack",
        "acknowledged": True,
        "text": ACK_TEXT,
        "phrase": str(phrase)[:128],
        "source": str(source)[:64],
        "acknowledged_at": when,
        "recorded_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
        "contains_key_material": False,
        "path": str(out),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "capability": "crm.record_custody_ack",
        "owner_trigger": True,
        "external_agent": False,
        "ack_path": str(out),
        "acknowledged": True,
        "acknowledged_at": when,
        "phrase": payload["phrase"],
        "contains_key_material": False,
        "sealed_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
    }


def read_restore_drill_latest(ampuly_home: Path) -> dict[str, Any] | None:
    data = _read_json_file(restore_drill_latest_path(ampuly_home))
    if not data:
        return None
    if data.get("capability") != "crm.restore_drill":
        return None
    if data.get("ok") is not True:
        return None
    if int(data.get("erasure_reapplied_audit") or 0) < 1:
        return None
    return data


def read_clean_machine_spotcheck(ampuly_home: Path) -> dict[str, Any] | None:
    data = _read_json_file(clean_machine_spotcheck_path(ampuly_home))
    if not data:
        return None
    if data.get("kind") != "crm.clean_machine_spotcheck":
        return None
    if data.get("ok") is not True:
        return None
    return data



def read_same_machine_strict_spotcheck(ampuly_home: Path) -> dict[str, Any] | None:
    data = _read_json_file(same_machine_strict_spotcheck_path(ampuly_home))
    if not data:
        return None
    if data.get("kind") != "crm.same_machine_strict_spotcheck":
        return None
    if data.get("ok") is not True:
        return None
    return data


def record_same_machine_strict_spotcheck(
    ampuly_home: Path,
    *,
    install_path: str,
    wheelhouse_path: str,
    headless_ok: bool,
    headed_ok: bool,
    restore_drill_ok: bool,
    host_label: str = "limeng",
    note: str = "",
) -> dict[str, Any]:
    """Evidence for same-machine strict clean env. Does NOT open clean_machine gate."""
    ensure_backup_keys_layout(ampuly_home)
    admission = real_data_admission_dir(ampuly_home)
    admission.mkdir(parents=True, exist_ok=True)
    out = same_machine_strict_spotcheck_path(ampuly_home)
    payload = {
        "kind": "crm.same_machine_strict_spotcheck",
        "ok": bool(headless_ok and headed_ok and restore_drill_ok),
        "same_machine_strict": True,
        "second_physical_machine": False,
        "satisfies_clean_machine_gate": False,
        "host_label": str(host_label)[:64],
        "install_path": str(install_path)[:512],
        "wheelhouse_path": str(wheelhouse_path)[:512],
        "headless_ok": bool(headless_ok),
        "headed_ok": bool(headed_ok),
        "restore_drill_ok": bool(restore_drill_ok),
        "note": (
            note
            or "Same-machine strict only; second physical PC still required for clean_machine gate."
        )[:512],
        "recorded_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
        "contains_key_material": False,
        "path": str(out),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": payload["ok"],
        "capability": "crm.record_same_machine_strict_spotcheck",
        "owner_trigger": True,
        "external_agent": False,
        "satisfies_clean_machine_gate": False,
        "path": str(out),
        "sealed_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
        "contains_key_material": False,
    }


def record_clean_machine_spotcheck(
    ampuly_home: Path,
    *,
    second_physical_pc: bool,
    host_label: str,
    install_path: str,
    wheelhouse_path: str,
    headless_ok: bool,
    headed_ok: bool,
    restore_drill_ok: bool,
    note: str = "",
) -> dict[str, Any]:
    """Record clean_machine gate mark. Refuses unless second_physical_pc is True."""
    if not second_physical_pc:
        raise ValueError(
            "clean_machine_gate_requires_second_physical_pc: "
            "use record_same_machine_strict_spotcheck for limeng same-machine evidence"
        )
    if not (headless_ok and headed_ok and restore_drill_ok):
        raise ValueError("clean_machine_spotcheck_smokes_incomplete")
    ensure_backup_keys_layout(ampuly_home)
    admission = real_data_admission_dir(ampuly_home)
    admission.mkdir(parents=True, exist_ok=True)
    out = clean_machine_spotcheck_path(ampuly_home)
    payload = {
        "kind": "crm.clean_machine_spotcheck",
        "ok": True,
        "same_machine_strict": False,
        "second_physical_machine": True,
        "satisfies_clean_machine_gate": True,
        "host_label": str(host_label)[:64],
        "install_path": str(install_path)[:512],
        "wheelhouse_path": str(wheelhouse_path)[:512],
        "headless_ok": True,
        "headed_ok": True,
        "restore_drill_ok": True,
        "note": (note or "Second physical PC spot-check")[:512],
        "recorded_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
        "contains_key_material": False,
        "path": str(out),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "capability": "crm.record_clean_machine_spotcheck",
        "owner_trigger": True,
        "external_agent": False,
        "satisfies_clean_machine_gate": True,
        "path": str(out),
        "sealed_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
        "contains_key_material": False,
    }


def _persist_restore_drill_receipt(ampuly_home: Path, result: "RestoreDrillResult") -> str | None:
    if not result.ok:
        return None
    admission = real_data_admission_dir(ampuly_home)
    admission.mkdir(parents=True, exist_ok=True)
    out = restore_drill_latest_path(ampuly_home)
    payload = result.to_dict()
    for banned in ("key", "key_bytes_hex", "secret", "wrap_key"):
        payload.pop(banned, None)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(out)



def ensure_backup_keys_layout(ampuly_home: Path) -> dict[str, Any]:
    """Create backup-keys dir + custody doc (no key bytes)."""
    root = backup_keys_dir(ampuly_home)
    root.mkdir(parents=True, exist_ok=True)
    admission = Path(ampuly_home) / "plugins" / "crm" / "real-data-admission"
    admission.mkdir(parents=True, exist_ok=True)
    doc = root / CUSTODY_DOC_NAME
    if not doc.is_file():
        doc.write_text(
            "\n".join(
                [
                    "# CRM backup-key custody (AMPULY_HOME)",
                    "",
                    "Path: `<AMPULY_HOME>/plugins/crm/backup-keys/<project>.key`",
                    "",
                    "## Rules",
                    "1. One 32-byte random wrapping key per project; created exclusively by the engine.",
                    "2. Keys NEVER enter project zips, `backups/projects/`, git, chat, logs, or receipt bodies.",
                    "3. Lost key = sealed backups cannot be opened (intentional).",
                    "4. Real-data admission also needs `REAL_DATA_PREREQUISITES_MET=True` AND an owner mark file under `real-data-admission/`.",
                    "5. Use `ampuly crm ensure-backup-key` / `ampuly crm restore-drill` (Owner commands).",
                    "",
                    f"Generated: {_now_shanghai()} Asia/Shanghai. This file contains no key material.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    key_files = [
        p.name
        for p in sorted(root.iterdir())
        if p.is_file() and p.suffix == ".key" and not p.is_symlink()
    ]
    return {
        "ok": True,
        "capability": "crm.ensure_backup_keys_layout",
        "owner_trigger": True,
        "external_agent": False,
        "backup_keys_dir": str(root),
        "custody_doc": str(doc),
        "custody_doc_present": doc.is_file(),
        "key_file_count": len(key_files),
        # names only — never contents
        "key_file_names": key_files,
        "real_data_admission_dir": str(admission),
        "sealed_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
    }


@dataclass
class EnsureKeyResult:
    ok: bool
    capability: str = "crm.ensure_backup_key"
    project_id: str = ""
    key_path: str = ""
    key_present: bool = False
    key_bytes: int = 0
    created_now: bool = False
    error: str = ""
    sealed_at: str = field(default_factory=_now_shanghai)
    owner_trigger: bool = True
    external_agent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def ensure_project_backup_key(
    ampuly_home: Path,
    project_id: str,
) -> EnsureKeyResult:
    """Create-or-load project backup key; return presence metadata only."""
    layout = ensure_backup_keys_layout(ampuly_home)
    path = key_path(ampuly_home, project_id)
    existed = path.is_file()
    result = EnsureKeyResult(ok=False, project_id=project_id, key_path=str(path))
    try:
        key = load_or_create_project_key(ampuly_home, project_id)
        # Immediately drop key material from locals for receipt safety.
        key_len = len(key)
        del key
        result.key_present = path.is_file()
        result.key_bytes = key_len
        result.created_now = not existed
        result.ok = result.key_present and result.key_bytes == 32
        result.sealed_at = _now_shanghai()
        # attach layout summary without key bytes
        _ = layout
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}:{exc}"
    return result


@dataclass
class RestoreDrillResult:
    ok: bool
    capability: str = "crm.restore_drill"
    project_id: str = ""
    work_dir: str = ""
    sealed_members: int = 0
    opened_members: int = 0
    tombstones_applied: int = 0
    erasure_reapplied_audit: int = 0
    kept_present: bool = False
    erased_absent: bool = False
    error: str = ""
    sealed_at: str = field(default_factory=_now_shanghai)
    owner_trigger: bool = True
    external_agent: bool = False
    synthetic_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def run_restore_drill(
    ampuly_home: Path,
    *,
    project_id: str = DEFAULT_DRILL_PROJECT,
    work_dir: Path | None = None,
) -> RestoreDrillResult:
    """Throwaway seal → open → tombstone reapply drill on synthetic CRM data.

    Uses a disposable directory (default under tempfile). Does not touch the
    owner's live project trees. Never prints key material.
    """
    ensure_backup_keys_layout(ampuly_home)
    result = RestoreDrillResult(ok=False, project_id=project_id)
    owned_tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if work_dir is None:
            owned_tmp = tempfile.TemporaryDirectory(prefix="crm-restore-drill-", ignore_cleanup_errors=True)
            root = Path(owned_tmp.name)
        else:
            root = Path(work_dir)
            root.mkdir(parents=True, exist_ok=True)
        result.work_dir = str(root)

        # Isolate drill AMPULY_HOME slice so live tombstones/keys stay untouched
        # except we DO use the real ampuly_home backup-keys for the drill project id
        # (creates a synthetic project key under backup-keys — intentional Owner step).
        live_home = Path(ampuly_home)
        crm_dir = root / "live" / "plugins" / "crm"
        crm_dir.mkdir(parents=True)
        tomb = root / "tombstones" / f"{project_id}.json"
        tomb.parent.mkdir(parents=True, exist_ok=True)

        scope = Scope(project_id, "customer-drill", "owner-drill", True, ("owner-drill",))
        store = CRMStore(
            crm_dir / "sales.sqlite",
            admission="synthetic_only",
            tombstones=tomb,
        )
        kept = store.save(
            scope,
            kind="company",
            fields={
                "name": "Keep Drill Co",
                "website": "https://example.com",
                "notes": "",
                "owner_id": "owner-drill",
            },
            request_key="drill-keep-1",
        )
        doomed = store.save(
            scope,
            kind="company",
            fields={
                "name": "Erase Drill Co",
                "website": "https://example.net",
                "notes": "",
                "owner_id": "owner-drill",
            },
            request_key="drill-erase-1",
        )
        plain_db = (crm_dir / "sales.sqlite").read_bytes()
        plain_hmac = (crm_dir / "sales.sqlite.hmac-key").read_bytes()
        plan = store.erasure_preview(scope, record_id=doomed["id"], mode="delete")
        store.erase(
            scope,
            token=plan["token"],
            record_id=doomed["id"],
            mode="delete",
            phrase=plan["phrase"],
            confirmed=True,
            request_key="drill-erase-confirm",
        )

        wrap = load_or_create_project_key(live_home, project_id)
        wrap_len = len(wrap)
        restored = root / "restored" / "plugins" / "crm"
        restored.mkdir(parents=True)
        sealed = 0
        for name, body in (("sales.sqlite", plain_db), ("sales.sqlite.hmac-key", plain_hmac)):
            member = f"project/{project_id}/plugins/crm/{name}"
            (restored / name).write_bytes(seal(body, key=wrap, aad=member.encode("utf-8")))
            sealed += 1
        del wrap  # drop key material

        opened = open_crm_tree_in_place(restored, ampuly_home=live_home, project_id=project_id)
        revived = CRMStore(
            restored / "sales.sqlite",
            admission="synthetic_only",
            tombstones=tomb,
        )
        applied = revived.apply_tombstones(scope)
        ids = {r["id"] for r in revived.list(scope, kind="company")}
        with open_sqlite(restored / "sales.sqlite", read_only=True) as conn:
            audit_n = conn.execute(
                "SELECT COUNT(*) FROM crm_audit WHERE action='erasure_reapplied'"
            ).fetchone()[0]

        result.sealed_members = sealed
        result.opened_members = opened
        result.tombstones_applied = int(applied)
        result.erasure_reapplied_audit = int(audit_n)
        result.kept_present = kept["id"] in ids
        result.erased_absent = doomed["id"] not in ids
        result.ok = (
            sealed == 2
            and opened == 2
            and applied >= 1
            and audit_n >= 1
            and result.kept_present
            and result.erased_absent
            and wrap_len == 32
        )
        result.sealed_at = _now_shanghai()
        if result.ok:
            _persist_restore_drill_receipt(live_home, result)
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}:{exc}"
    finally:
        # Windows may keep SQLite WAL handles briefly; ignore cleanup errors.
        for name in ("store", "revived"):
            if name in locals():
                try:
                    obj = locals()[name]
                    close = getattr(obj, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
                try:
                    del obj
                except Exception:
                    pass
        if owned_tmp is not None:
            try:
                owned_tmp.cleanup()
            except Exception:
                pass
    return result


def describe_real_data_gates(ampuly_home: Path | None = None) -> dict[str, Any]:
    """Checklist status for REAL_DATA door — does not flip any flag.

    Prefer top-level summary fields:
    custody_ack / restore_drill / clean_machine / real_data_open.
    REAL_DATA_PREREQUISITES_MET stays False until clean-machine spot-check
    is recorded; encrypted backup path for synthetic data remains available.
    """
    home = Path(ampuly_home) if ampuly_home is not None else None
    layout = None
    key_count = 0
    custody = False
    ack = None
    drill = None
    clean = None
    if home is not None:
        layout = ensure_backup_keys_layout(home)
        key_count = int(layout["key_file_count"])
        custody = bool(layout["custody_doc_present"])
        ack = read_custody_ack(home)
        drill = read_restore_drill_latest(home)
        clean = read_clean_machine_spotcheck(home)

    custody_ack_met = ack is not None
    restore_drill_met = drill is not None
    clean_machine_met = clean is not None
    code_flag_met = bool(REAL_DATA_PREREQUISITES_MET)
    prerequisites_partial = bool(
        custody
        and key_count > 0
        and custody_ack_met
        and restore_drill_met
    )

    checklist = [
        {
            "id": "l4_merged",
            "label": "L4 encrypted backup + retention + tombstone reapply merged",
            "met": True,
            "owner_action": False,
        },
        {
            "id": "custody_doc",
            "label": "backup-keys custody doc present under AMPULY_HOME",
            "met": custody if home is not None else False,
            "owner_action": True,
            "cli": "ampuly crm ensure-backup-keys",
        },
        {
            "id": "key_present_for_projects",
            "label": "At least one project backup key exists (Owner-generated)",
            "met": key_count > 0 if home is not None else False,
            "owner_action": True,
            "cli": "ampuly crm ensure-backup-key --project <id>",
        },
        {
            "id": "restore_drill",
            "label": "Owner ran restore drill with tombstone reapply (synthetic)",
            "met": restore_drill_met,
            "owner_action": True,
            "cli": "ampuly crm restore-drill",
            "note": "Persisted under real-data-admission/restore-drill-latest.json when ok.",
            "receipt_path": str(restore_drill_latest_path(home)) if home is not None else None,
            "receipt_sealed_at": (drill or {}).get("sealed_at") if drill else None,
        },
        {
            "id": "owner_custody_ack",
            "label": "Owner written ACK that lost key = backups unreadable",
            "met": custody_ack_met,
            "owner_action": True,
            "cli": "ampuly crm record-custody-ack",
            "note": "Engine-readable ACK file; no key material.",
            "ack_path": str(custody_ack_path(home)) if home is not None else None,
            "acknowledged_at": (ack or {}).get("acknowledged_at") if ack else None,
        },
        {
            "id": "second_person_clean_env",
            "label": "Second person clean-env spot-check (encrypt→restore→tombstone)",
            "met": clean_machine_met,
            "owner_action": True,
            "note": "Requires a second physical PC. Same-machine strict evidence (same-machine-strict-spotcheck.json) does NOT set this gate. Ordered last.",
            "path_pattern": str(clean_machine_spotcheck_path(home)) if home is not None else None,
        },
        {
            "id": "code_flag",
            "label": "REAL_DATA_PREREQUISITES_MET code flag",
            "met": code_flag_met,
            "owner_action": False,
            "current": code_flag_met,
            "note": "Stays False until clean-machine spot-check is recorded.",
        },
        {
            "id": "admission_mark_file",
            "label": "Per-project real-data-admission mark file (no wire writer)",
            "met": False,
            "owner_action": True,
            "path_pattern": str(
                real_data_flag(home, "<project>") if home is not None else
                "<AMPULY_HOME>/plugins/crm/real-data-admission/<project>.json"
            ),
        },
    ]
    all_met = all(item["met"] for item in checklist)
    real_data_open = False  # hard: never claim open while code flag False
    return {
        "capability": "crm.real_data_status",
        "owner_trigger": True,
        "external_agent": False,
        "REAL_DATA_PREREQUISITES_MET": code_flag_met,
        "custody_ack": custody_ack_met,
        "restore_drill": restore_drill_met,
        "clean_machine": clean_machine_met,
        "real_data_open": real_data_open,
        "prerequisites_partial": prerequisites_partial,
        "door_open": real_data_open,
        "all_gates_met": all_met and code_flag_met,
        "checklist": checklist,
        "layout": layout,
        "sealed_at": _now_shanghai(),
        "zone": "Asia/Shanghai",
        "does_not": [
            "flip REAL_DATA_PREREQUISITES_MET",
            "write per-project admission mark files for real ingest",
            "print key material",
            "start clean-machine install",
        ],
    }


def describe_crm_owner_capability() -> dict[str, Any]:
    return {
        "id": "crm.owner_ops",
        "title": "CRM real-data gate Owner ops (key custody + restore drill)",
        "owner_triggerable": True,
        "external_agent_required": False,
        "cli": [
            "ampuly crm ensure-backup-keys",
            "ampuly crm ensure-backup-key --project <id>",
            "ampuly crm restore-drill",
            "ampuly crm record-custody-ack",
            "ampuly crm real-data-status",
            "ampuly crm record-same-machine-strict-spotcheck",
            "ampuly crm record-clean-machine-spotcheck --second-physical-pc",
        ],
        "REAL_DATA_PREREQUISITES_MET": bool(REAL_DATA_PREREQUISITES_MET),
        "writes": [
            "<AMPULY_HOME>/plugins/crm/backup-keys/<project>.key",
            "<AMPULY_HOME>/plugins/crm/backup-keys/CUSTODY.md",
            "<AMPULY_HOME>/plugins/crm/real-data-admission/owner-custody-ack.json",
            "<AMPULY_HOME>/plugins/crm/real-data-admission/restore-drill-latest.json",
            "<AMPULY_HOME>/plugins/crm/real-data-admission/same-machine-strict-spotcheck.json",
            "<AMPULY_HOME>/plugins/crm/real-data-admission/clean-machine-spotcheck.json (second PC only)",
            "throwaway restore-drill work dir (deleted)",
        ],
        "never": ["print/log/commit key bytes", "flip real-data flag without all gates"],
    }
