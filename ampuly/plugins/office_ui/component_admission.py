"""组件准入: a tested skill from the trial area enters the internal library, on the owner's word.

The flow the owner set : search the market, download into the trial area, test, admit by
hand. This module holds the last two steps to what they mean:

* **A test is a record, not a feeling.** Marking a trial passed or failed writes who, when, what they
  checked and the digest of the files they checked; the files on disk are re-verified first, so a
  trial edited after download cannot be marked as if it were the download.
* **Only a passed test can be admitted, and only what was tested lands.** Admission re-verifies every
  file against the trial's recorded sha256 list, then copies exactly those bytes.
* **Where SkillHub's file list and its package disagree, the owner says the package is meant.** The
  package is what was downloaded, tested and lands (policy); a trial whose package differed
  from SkillHub's own list is admitted only with the owner's explicit acknowledgement of those very
  differences (the fingerprint the page showed them under), and they are written into the admission record.
* **What the catalog blocks stays blocked.** Per-use paid skills (no budget or payment channel), a
  license that restricts redistribution, a skill without its own metadata, anything the secret scan
  flags. A skill with no declared license may be admitted -- the owner decided a tested, hand-admitted
  skill may ship -- but only with the owner saying, in words, that they have the right.
* **The skill's own files are not edited.** What the owner decided, and on what evidence, is written
  next to them in ``ADMISSION.json``, with the success criteria the owner gives at admission. That record
  completes the skill's release contract (workflow_packages.load_skill_contract reads it), and its
  ``admission_record`` check re-verifies the admitted bytes, so a testing package can lock the skill and a
  later edit to it shows. A ``.gitattributes`` beside them keeps git from rewriting their line endings.
* **弃用 is a decision, not a deletion.** A discarded trial keeps its files and record; 重新试用 re-verifies
  the files and puts it back to be tested.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ampuly.core.config import replace_with_retry
from ampuly.plugins.office_ui.skill_market import TRIAL_FILE, MarketError, load_trial, trial_folder, trial_listing_mismatch
from ampuly.plugins.office_ui.skill_market import listing_mismatch_key as current_mismatch_key

ADMISSION_FILE = "ADMISSION.json"
# Written next to an admitted skill: its bytes are recorded by sha256, so git must store and check them out as
# they are (the repository's text=auto and eol rules would otherwise rewrite line endings).
GITATTRIBUTES_FILE = ".gitattributes"
GITATTRIBUTES_TEXT = "# Admitted from a skill market: files are kept byte for byte (ADMISSION.json lists their sha256).\n* -text\n"
OUR_FILES = (ADMISSION_FILE, GITATTRIBUTES_FILE)
MAX_SUCCESS_CRITERIA = 20
MAX_CRITERION_CHARS = 300
# The package loader's skill id rule; a name outside it could never be declared by a package.
SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
MIN_NOTE = 8
REFUSING_BLOCKERS = {
    "owner_budget_approval": "按次付费的技能暂不能准入：还没有预算与支付通道",
    "license_restricts_redistribution": "许可限制再分发，不能进入会随交付发出的内部库",
    "frontmatter_missing": "技能没有自己的元数据（frontmatter），先补齐再准入",
}


class AdmissionError(ValueError):
    """A refusal already in the owner's words. ``reason`` names the few the page acts on itself
    (``listing_mismatch_unacknowledged`` and ``listing_mismatch_changed`` open the confirmation that names the trial's
    current differences, carried with their key); the rest leave it empty."""

    def __init__(
        self, message: str = "", *, reason: str = "", listing_mismatch: dict[str, Any] | None = None, listing_mismatch_key: str = "",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.listing_mismatch = listing_mismatch
        self.listing_mismatch_key = listing_mismatch_key


def _now(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _note(note: str) -> str:
    text = " ".join(str(note or "").split())
    if len(text) < MIN_NOTE:
        raise AdmissionError("写一句说明（至少 8 个字）：看过什么、怎么测的")
    return text[:2000]


def _success_criteria(criteria: Any) -> list[str]:
    """What a step using the skill must achieve, as the owner wrote it: the release contract's success criteria."""
    items = [" ".join(str(item or "").split()) for item in (criteria if isinstance(criteria, (list, tuple)) else [])]
    items = [item for item in items if item]
    if not items:
        raise AdmissionError("至少写一条成功标准：用这个技能的步骤要做到什么")
    if len(items) > MAX_SUCCESS_CRITERIA:
        raise AdmissionError(f"成功标准最多 {MAX_SUCCESS_CRITERIA} 条")
    if any(len(item) > MAX_CRITERION_CHARS for item in items):
        raise AdmissionError(f"每条成功标准最多 {MAX_CRITERION_CHARS} 字")
    return items


def git_ignored(repo: Path, paths: list[str]) -> list[str]:
    """The paths (relative to ``repo``) git would silently leave out of a commit.

    An admitted skill ships through git: a file matching the repository's .gitignore (``data/``, ``*.log``,
    ``*.manifest`` ...) stays on this disk, the admission check passes here, and a fresh clone or a release
    archive lacks it. Outside a git checkout (or without git) nothing can be said, and nothing is refused.
    """
    if not paths or not (Path(repo) / ".git").exists():
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--no-index", "--stdin", "-z"],
            input="\0".join(paths) + "\0", capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode not in (0, 1):
        return []
    return sorted(item for item in result.stdout.split("\0") if item)


def _trial_folder(ampuly_home: Path, slug: str, version: str, source: str = "skillhub") -> Path:
    try:
        folder = trial_folder(ampuly_home, slug, version, source)
    except MarketError as exc:
        raise AdmissionError("试用区里没有这个技能版本") from exc
    if not (folder / TRIAL_FILE).is_file():
        raise AdmissionError("试用区里没有这个技能版本")
    return folder


_LOCKS_GUARD = threading.Lock()
_TRIAL_LOCKS: dict[str, threading.Lock] = {}


def _trial_lock(folder: Path) -> threading.Lock:
    """One lock per trial: a test marked while admission runs is either before it (and seen) or after it (and refused)."""
    key = str(folder.resolve()).casefold()
    with _LOCKS_GUARD:
        return _TRIAL_LOCKS.setdefault(key, threading.Lock())


def _refetch_hint(record: dict[str, Any]) -> str:
    """How to get a fresh copy: a market skill is downloaded again, a local library one discarded and imported again."""
    if record.get("source") == "catalog":
        return "请先弃用，再在组件库的本地资源库目录里点「调用测试」重新导入"
    return "请重新下载再测试"


def _verified_files(folder: Path, record: dict[str, Any]) -> dict[str, bytes]:
    """The trial's files, each matching the sha256 recorded at download, and nothing else."""
    expected = record.get("files_sha256") if isinstance(record.get("files_sha256"), dict) else {}
    root = folder / "files"
    present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if present != set(expected):
        raise AdmissionError("试用区里的文件和下载时记录的不一致（被改过或缺失），" + _refetch_hint(record))
    contents: dict[str, bytes] = {}
    for key, digest in expected.items():
        path = root / PurePosixPath(key)
        if path.is_symlink():
            raise AdmissionError("试用区里出现了链接文件，不能准入")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise AdmissionError(f"{key} 和下载时的摘要值不一致，" + _refetch_hint(record))
        contents[key] = data
    return contents


def _files_digest(record: dict[str, Any]) -> str:
    listing = "\n".join(f"{key} {value}" for key, value in sorted(dict(record.get("files_sha256") or {}).items()))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def _write_record(folder: Path, record: dict[str, Any]) -> None:
    temporary = folder / f".{TRIAL_FILE}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replace_with_retry(temporary, folder / TRIAL_FILE)


def mark_trial(
    ampuly_home: Path, slug: str, version: str, *, outcome: str, note: str, actor: str, now: datetime | None = None, source: str = "skillhub",
) -> dict[str, Any]:
    """Record a test of one trial as passed or failed."""
    if outcome not in {"passed", "failed"}:
        raise AdmissionError("测试结果只能是通过或不通过")
    if not str(actor or "").strip():
        raise AdmissionError("测试记录需要写明是谁测的")
    folder = _trial_folder(ampuly_home, slug, version, source)
    text = _note(note)
    with _trial_lock(folder):
        record = load_trial(folder)
        if record.get("status") == "admitted":
            raise AdmissionError("这个版本已经准入，不再改测试结论")
        if record.get("status") == "discarded":
            raise AdmissionError("这个版本已经弃用：先点「重新试用」再测试")
        _verified_files(folder, record)
        test = {"outcome": outcome, "note": text, "by": str(actor).strip(), "at": _now(now), "files_digest": _files_digest(record)}
        record["tests"] = [*list(record.get("tests") or []), test]
        record["status"] = "test_passed" if outcome == "passed" else "test_failed"
        _write_record(folder, record)
    return record


def admit_trial(
    repo: Path,
    ampuly_home: Path,
    slug: str,
    version: str,
    *,
    actor: str,
    note: str,
    license_confirmed: bool = False,
    success_criteria: Any = None,
    now: datetime | None = None,
    source: str = "skillhub",
    listing_mismatch_acknowledged: bool = False,
    listing_mismatch_key: str = "",
) -> dict[str, Any]:
    """Copy one tested trial into distribution/skills with the owner's decision beside it.

    ``listing_mismatch_acknowledged`` is the owner's yes to admitting a SkillHub package that differed from SkillHub's
    own file list; only ``True`` counts, as for ``license_confirmed``. ``listing_mismatch_key`` is the fingerprint of
    the differences the owner was shown (skill_market.listing_mismatch_key); it must be the trial's current one.
    """
    if not str(actor or "").strip():
        raise AdmissionError("准入需要记录是谁批准的")
    folder = _trial_folder(ampuly_home, slug, version, source)
    with _trial_lock(folder):
        return _admit_locked(
            repo, folder, slug, version, actor=actor, note=note, license_confirmed=license_confirmed is True,
            success_criteria=success_criteria, now=now, listing_mismatch_acknowledged=listing_mismatch_acknowledged is True,
            listing_mismatch_key=listing_mismatch_key if isinstance(listing_mismatch_key, str) else "",
        )


def discard_trial(ampuly_home: Path, slug: str, version: str, *, actor: str, now: datetime | None = None, source: str = "skillhub") -> dict[str, Any]:
    """弃用: the trial stops being offered. Its files and test history stay, so the decision can be looked at later."""
    if not str(actor or "").strip():
        raise AdmissionError("弃用需要记录是谁决定的")
    folder = _trial_folder(ampuly_home, slug, version, source)
    with _trial_lock(folder):
        record = load_trial(folder)
        status = str(record.get("status") or "")
        if status == "admitted":
            raise AdmissionError("这个版本已经准入内部库，不能在试用区弃用")
        if status == "discarded":
            return record
        record["status"] = "discarded"
        # Every discard stays on the record (as every restore does), so the decisions can be read back in order.
        record["discards"] = [*list(record.get("discards") or []), {"by": str(actor).strip(), "at": _now(now), "from_status": status}]
        _write_record(folder, record)
    return record


def restore_trial(ampuly_home: Path, slug: str, version: str, *, actor: str, now: datetime | None = None, source: str = "skillhub") -> dict[str, Any]:
    """重新试用: a discarded trial comes back to be tested again, once its files still match the download."""
    if not str(actor or "").strip():
        raise AdmissionError("重新试用需要记录是谁决定的")
    folder = _trial_folder(ampuly_home, slug, version, source)
    with _trial_lock(folder):
        record = load_trial(folder)
        if record.get("status") != "discarded":
            raise AdmissionError("只有弃用的版本才需要重新试用")
        try:
            _verified_files(folder, record)
        except AdmissionError as exc:
            raise AdmissionError("试用区里的文件和下载时记录的不一致，不能直接恢复：" + ("在组件库的本地资源库目录里对它再点「调用测试」重新导入" if source == "catalog" else "在「在线市场」里点「重新下载」取一份新的")) from exc
        # Tested again from the start: an earlier pass was given before it was discarded.
        record["status"] = "downloaded"
        record["restored"] = [*list(record.get("restored") or []), {"by": str(actor).strip(), "at": _now(now)}]
        _write_record(folder, record)
    return record


def _admit_locked(
    repo: Path, folder: Path, slug: str, version: str, *, actor: str, note: str, license_confirmed: bool, success_criteria: Any,
    now: datetime | None, listing_mismatch_acknowledged: bool = False, listing_mismatch_key: str = "",
) -> dict[str, Any]:
    from ampuly.cli.distribution import check_secrets
    from ampuly.plugins.office_ui.component_catalog import skill_governance

    record = load_trial(folder)
    if record.get("status") != "test_passed":
        raise AdmissionError("只有测试通过的版本才能准入")
    if record.get("paid") is not False:
        # Download refuses a paid skill and records paid: false; a record without that was not made by it.
        raise AdmissionError("这份试用记录没有写明技能免费，" + _refetch_hint(record))
    last_test = (record.get("tests") or [{}])[-1]
    if last_test.get("files_digest") != _files_digest(record):
        raise AdmissionError("测试记录对应的文件和现在的不一致，请重新测试")
    if not SKILL_ID_RE.match(slug):
        raise AdmissionError(f"技能名 {slug} 不符合流程包的技能命名（小写字母开头，只含小写字母、数字、- 和 _）")
    governance = skill_governance(dict(record.get("facts") or {}))
    for blocker in governance["admission_blockers"]:
        if blocker in REFUSING_BLOCKERS:
            raise AdmissionError(REFUSING_BLOCKERS[blocker])
    if "license_unconfirmed" in governance["admission_blockers"] and not license_confirmed:
        raise AdmissionError("技能没有声明许可：需要你确认有权使用并随交付分发它")
    text = _note(note)
    criteria = _success_criteria(success_criteria)
    mismatch = trial_listing_mismatch(record)
    if mismatch:
        current_key = current_mismatch_key(mismatch)
        if not listing_mismatch_acknowledged:
            # policy: the package is what was tested and what lands; where SkillHub's own file list says
            # otherwise, the owner confirms the package is the one meant. The page turns this reason into that confirmation.
            raise AdmissionError(
                "SkillHub 的文件清单和下载包不一致：确认按下载包录入后才能准入", reason="listing_mismatch_unacknowledged",
                listing_mismatch=mismatch, listing_mismatch_key=current_key,
            )
        if listing_mismatch_key != current_key:
            # The yes is to the differences the owner was shown: none named (an older page), or others than the trial's
            # now (downloaded again since), admits nothing. The page reopens the confirmation with these.
            raise AdmissionError(
                "SkillHub 清单和下载包的差异和你确认时看到的不一样：请按现在的差异重新确认", reason="listing_mismatch_changed",
                listing_mismatch=mismatch, listing_mismatch_key=current_key,
            )
    contents = _verified_files(folder, record)
    for ours in OUR_FILES:
        if any(key.casefold() == ours.casefold() for key in contents):
            # Case-insensitive: on Windows a downloaded admission.json keeps its name when ours is written over it.
            raise AdmissionError(f"技能自带了 {ours}，会和准入记录冲突，不能准入")
    if any(part.casefold() == ".git" for key in contents for part in key.split("/")):
        # Filed before names like this were refused: git never commits a .git path, so the record would list missing files.
        raise AdmissionError("技能带有 .git 目录，提交时会变成嵌入仓库、别的机器上缺文件，不能准入")
    target = Path(repo) / "distribution" / "skills" / slug
    if target.exists():
        raise AdmissionError(f"内部库已有同名技能 {slug}")
    ignored = git_ignored(Path(repo), [f"distribution/skills/{slug}/{key}" for key in sorted(contents)])
    if ignored:
        raise AdmissionError(
            "这些文件会被仓库的 .gitignore 忽略、不会随提交进入内部库，录入后在别的机器上会缺文件：" + "、".join(item.rsplit(f"{slug}/", 1)[-1] for item in ignored[:5])
        )

    staging = Path(repo) / ".tmp" / f"admission-{slug}-{uuid.uuid4().hex[:8]}"
    try:
        for key, data in contents.items():
            destination = staging / PurePosixPath(key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        # Every file, whatever its name: these were written by someone else. Findings name file and line only.
        hits = check_secrets(staging.parent, roots=(staging.name,), every_file=True)
        if hits:
            raise AdmissionError("技能文件里有疑似密钥，不能进入内部库：" + "；".join(hit.removeprefix(f"{staging.name}/") for hit in hits[:5]))
        facts = dict(record.get("facts") or {})
        from_catalog = record.get("source") == "catalog"
        admission = {
            "schema_version": 1,
            "skill_id": slug,
            "decision": "admitted",
            "admitted_by": str(actor).strip(),
            "admitted_at": _now(now),
            "note": text,
            "license": {**dict(facts.get("license") or {}), "owner_confirmed": bool(license_confirmed)},
            "source": {
                "market": record.get("source"), "slug": slug, "version": version, "zip_sha256": record.get("zip_sha256"),
                # The market's category: the internal library files the skill where the market did.
                "category": "" if from_catalog else str(record.get("category") or ""),
                # A local library's category is its folder's name, not one of the market's keys, so it is kept apart
                # and internal_categories.yaml files the skill; with where in the library it came from.
                **({
                    "library_category": str(record.get("category") or ""),
                    "library_path": str(record.get("library_path") or ""),
                    "catalog_skill_md_sha256": str(record.get("catalog_skill_md_sha256") or ""),
                } if from_catalog else {}),
            },
            "test": {key: last_test.get(key) for key in ("outcome", "note", "by", "at", "files_digest")},
            # How the SkillHub package differed from SkillHub's own file list, and that the owner admitted the package
            # anyway ({} when they agreed, and for a local library skill).
            "listing_mismatch": {**mismatch, "owner_acknowledged": True} if mismatch else {},
            # With the source version, provenance and this record's own check, these complete the release contract.
            "success_criteria": criteria,
            "governance_at_admission": governance,
            "files_sha256": dict(record.get("files_sha256") or {}),
        }
        (staging / ADMISSION_FILE).write_text(json.dumps(admission, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        (staging / GITATTRIBUTES_FILE).write_text(GITATTRIBUTES_TEXT, encoding="utf-8", newline="\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_with_retry(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    record["status"] = "admitted"
    record["admitted"] = {
        "by": str(actor).strip(), "at": admission["admitted_at"], "target": f"distribution/skills/{slug}",
        **({"listing_mismatch_acknowledged": True} if mismatch else {}),
    }
    _write_record(folder, record)
    return {"skill_id": slug, "version": version, "target": f"distribution/skills/{slug}", "files": len(contents), "governance": governance, "needs_commit": True}
