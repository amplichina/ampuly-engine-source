"""本地资源库 → 试用区: 调用测试 on a skill the component catalog lists from the local resource library.

The catalog (``distribution/component_catalog/skills.yaml``) holds only metadata scanned read-only from the owner's
local skill library, with each skill's SKILL.md sha256. Trying one (policy: the external library's
components are tried and admitted like the market's) copies its folder into the trial area, where the same test,
discard and admission road the market uses takes over.

What this holds to:

* **The library is named, never guessed.** Its folder comes from ``SUPERONES_SKILL_LIBRARY_ROOT``; the folder's name
  must be the library the catalog was scanned from. Without it nothing is imported, and the page says so.
* **What lands is what the catalog saw.** The SKILL.md must still hash to the catalog's sha256; a library copy that
  changed since the scan is refused rather than tried as if it were the skill the catalog describes.
* **The same limits as a market download.** Plain relative file names (no links, no device names, no case twins),
  at most 500 files and 20 MB; paid skills are refused before any file is read.
* **The library is only read.** Nothing in it is written, moved or deleted.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ampuly.plugins.office_ui.skill_market import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    SLUG_RE,
    VERSION_RE,
    TRIAL_FILE,
    MarketError,
    _case_collision,
    _safe_member,
    file_trial,
    load_trial,
    shown_name,
    trial_folder,
)

LIBRARY_ROOT_ENV = "SUPERONES_SKILL_LIBRARY_ROOT"
SOURCE = "catalog"


def library_root() -> Path | None:
    """The local resource library folder the owner named, if any (the environment, never a guess)."""
    value = str(os.environ.get(LIBRARY_ROOT_ENV, "") or "").strip()
    return Path(value) if value else None


def catalog_library_name(repo: Path) -> str:
    from ampuly.plugins.office_ui.component_catalog import CATALOG_DIR, SKILLS_FILE, _read_yaml

    # The same reader load_catalog uses: a file it cannot read is a listed problem there, and simply no name here.
    data = _read_yaml(Path(repo) / CATALOG_DIR / SKILLS_FILE, [])
    return str(data.get("library") or "") if isinstance(data, dict) else ""


def library_status(repo: Path, root: Path | None = None, expected: str | None = None) -> dict[str, Any]:
    """Whether catalog skills can be tried on this machine, and if not, why (shown on the page, told to master control).

    ``expected`` is the catalog's library name when the caller has already loaded the catalog.
    """
    root = root if root is not None else library_root()
    expected = catalog_library_name(repo) if expected is None else expected
    if root is None:
        return {"ready": False, "reason": "library_not_configured", "library": expected}
    if not expected:
        # A catalog that does not say which folder it was scanned from cannot vouch for any folder.
        return {"ready": False, "reason": "library_unknown", "library": expected}
    if not root.is_dir():
        return {"ready": False, "reason": "library_missing", "library": expected}
    if root.name != expected:
        return {"ready": False, "reason": "library_mismatch", "library": expected}
    return {"ready": True, "reason": "", "library": expected}


def _entry(repo: Path, skill_id: str) -> dict[str, Any]:
    from ampuly.plugins.office_ui.component_catalog import load_catalog

    for skill in load_catalog(Path(repo)).skills:
        if str(skill.get("id") or "") == skill_id:
            return dict(skill)
    raise MarketError("本地资源库目录里没有这个技能", reason="not_found")


def _paid(entry: dict[str, Any]) -> bool:
    from ampuly.plugins.office_ui.component_catalog import skill_governance

    if dict(entry.get("pricing") or {}).get("paid"):
        return True
    try:
        return "owner_budget_approval" in skill_governance(entry)["admission_blockers"]
    except Exception:  # noqa: BLE001 - an entry nobody can read is not let through as free
        return True


def _read_folder(folder: Path) -> dict[str, bytes]:
    """Every file under a library skill folder, by plain relative name, within the market's limits; links refused.

    Git metadata is left behind as the catalog scan leaves it (it is never part of a skill, and git would never commit
    it); a folder that cannot be listed refuses the import rather than filing a partial copy as the whole skill.
    """
    from ampuly.plugins.office_ui.component_catalog import _is_link

    def _unreadable(error: OSError) -> None:
        raise MarketError("资源库里的技能有读不到的目录，不导入不完整的副本", reason="unreadable") from error

    contents: dict[str, bytes] = {}
    total = 0
    for current, dirs, files in os.walk(folder, onerror=_unreadable):
        base = Path(current)
        dirs[:] = [name for name in dirs if name.casefold() != ".git"]
        for name in list(dirs):
            if _is_link(base / name):
                raise MarketError(f"资源库里的技能目录含链接：{(base / name).relative_to(folder).as_posix()}", reason="unsafe_archive")
        for name in files:
            if name.casefold() == ".git":
                continue  # a submodule's or worktree's pointer file
            path = base / name
            rel = path.relative_to(folder).as_posix()
            if _is_link(path) or _safe_member(rel) is None:
                raise MarketError(f"资源库里的技能含不安全的文件名或链接：{shown_name(rel)}", reason="unsafe_archive")
            size = path.stat().st_size
            total += size
            if len(contents) + 1 > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise MarketError("资源库里的这个技能超过 500 个文件或 20 MB，不导入", reason="too_large")
            contents[rel] = path.read_bytes()
    if _case_collision(contents):
        raise MarketError("资源库里的技能有只差大小写的两个文件", reason="unsafe_archive")
    return contents


def import_catalog_trial(
    repo: Path,
    ampuly_home: Path,
    skill_id: str,
    *,
    actor: str,
    now: datetime | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Copy one catalog skill from the local resource library into the trial area, checked against the catalog."""
    if not SLUG_RE.match(str(skill_id or "")):
        raise MarketError("这不是本地资源库目录里的技能名", reason="bad_request")
    entry = _entry(repo, skill_id)
    if _paid(entry):
        raise MarketError("按次付费的技能暂不导入：还没有预算与支付通道", reason="paid")
    status = library_status(repo, root)
    if not status["ready"]:
        messages = {
            "library_not_configured": f"本机没有配置本地资源库路径（环境变量 {LIBRARY_ROOT_ENV}），不能从资源库导入",
            "library_missing": "配置的本地资源库路径不存在",
            "library_mismatch": f"配置的本地资源库不是目录扫描时的「{status['library']}」",
            "library_unknown": "组件库目录没有记录扫描时的本地资源库名：先重新生成组件库目录再导入",
        }
        raise MarketError(messages[status["reason"]], reason=status["reason"])
    library = (root if root is not None else library_root())
    assert library is not None
    source = dict(entry.get("source") or {})
    relative = str(source.get("path") or "")
    if _safe_member(relative) is None:
        raise MarketError("目录里记录的技能路径不是资源库里的相对路径", reason="unsafe_archive")
    folder = library / PurePosixPath(relative)
    try:
        folder.resolve().relative_to(library.resolve())
    except ValueError as exc:
        raise MarketError("目录里记录的技能路径指到了资源库外面", reason="unsafe_archive") from exc
    from ampuly.plugins.office_ui.component_catalog import _is_link

    if _is_link(folder) or not (folder / "SKILL.md").is_file():
        raise MarketError("资源库里找不到这个技能（或它是链接）", reason="not_found")
    contents = _read_folder(folder)
    if "SKILL.md" not in contents:
        # On a case-insensitive disk skill.md answers to SKILL.md; the skill's own file has to carry the exact name.
        raise MarketError("资源库里的技能根目录没有名为 SKILL.md 的文件（文件名大小写必须一致）", reason="not_a_skill")
    skill_md_sha256 = hashlib.sha256(contents["SKILL.md"]).hexdigest()
    if skill_md_sha256 != str(source.get("skill_md_sha256") or ""):
        # The library changed since the catalog was scanned: what would be tried is not the skill the catalog describes.
        raise MarketError("资源库里的 SKILL.md 和目录扫描时不一致：先重新生成组件库目录再导入", reason="catalog_mismatch")
    version = str(entry.get("version") or "")
    if not VERSION_RE.match(version) or ".." in version:
        version = f"sha-{skill_md_sha256[:12]}"
    filed = trial_folder(ampuly_home, skill_id, version, SOURCE)
    if (filed / TRIAL_FILE).is_file():
        existing = load_trial(filed)
        fresh = {key: hashlib.sha256(data).hexdigest() for key, data in sorted(contents.items())}
        # A market version never changes; a library skill can be edited without its version moving. The trial filed
        # under this version is then not this skill, and handing it back would test and admit the old bytes.
        if existing.get("status") != "discarded" and dict(existing.get("files_sha256") or {}) != fresh:
            if existing.get("status") == "admitted":
                message = "内部库里录入的是资源库改动前的内容（版本号没变）：在 SKILL.md 里升版本号、重新生成组件库目录后再导入"
            else:
                message = "试用区里这个版本是资源库改动前导入的（版本号没变）：先在试用卡片上「弃用」它，再点「调用测试」重新导入"
            raise MarketError(message, reason="trial_outdated")
    return file_trial(
        ampuly_home, SOURCE, skill_id, version, contents, name=str(entry.get("name") or skill_id), category=str(entry.get("category") or ""),
        actor=actor, now=now,
        # Where it came from inside the library (relative, as the catalog records it) and the SKILL.md the catalog saw.
        extra={"library_path": relative, "catalog_skill_md_sha256": skill_md_sha256},
    )
