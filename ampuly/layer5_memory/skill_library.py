"""Skill library — loads and manages SKILL.md format skills (nanobot-compatible)."""

from __future__ import annotations

import hashlib
import os
import stat
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from ampuly.project_id import is_valid_project_id

# No distribution-specific skills are retired in the community edition.
# The explicit filter remains available for downstream distributions.
RETIRED_SKILLS: frozenset[str] = frozenset()

# Every launch installs its flow's skills over the one shared folder (``skills/<name>``), which new orders and the
# console read. An order already running reads the body its launch froze instead (``workflow_contract.skill_sha256``),
# kept once, write-only, under ``skills/.versions/<name>/<fingerprint>/``.
# Nothing here ever removes a kept body: when to clean them up is the owner's call.
SKILL_VERSIONS_DIRNAME = ".versions"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# The contract is pinned on the order's own task only; the step tasks that run it get just these fingerprints, under
# their own key (company_mode, before each step runs). Not the whole contract: flow lookups, the console snapshot and
# run comparisons read ``workflow_contract`` per task, and a step carrying it would count as an order of its own.
RUN_TASK_SKILL_PINS_KEY = "workflow_skill_sha256"


def skill_version_dir(skills_dir: Path, name: str, sha256: str) -> Path | None:
    """Where the body of skill ``name`` whose SKILL.md fingerprints to ``sha256`` is kept; None for a malformed pair.

    ``sha256`` is the fingerprint a launch freezes: SKILL.md's hash with CRLF folded to LF (``skill_md_sha256``).
    """
    clean_name = str(name or "").strip()
    clean_sha = str(sha256 or "").strip().lower()
    if not clean_name or clean_name in {".", ".."} or "/" in clean_name or "\\" in clean_name:
        return None
    if not _SHA256_HEX.fullmatch(clean_sha):
        return None
    return Path(skills_dir) / SKILL_VERSIONS_DIRNAME / clean_name / clean_sha


def skill_md_sha256(path: Path) -> str:
    """SKILL.md's fingerprint as a launch freezes it, or "" when unreadable.

    Must agree with ``workflow_packages.WorkflowPackage._file_sha256`` (which writes the frozen value): CRLF pairs
    folded to LF, binary content (a NUL byte) hashed verbatim.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    if b"\x00" not in raw:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def frozen_skill_sha256(metadata: Any) -> dict[str, str]:
    """The skill fingerprints a task runs under (name -> sha256), or {} when it has none.

    An order's own task has them in its ``workflow_contract``; a step task running for that order has the copy made
    before it runs (``RUN_TASK_SKILL_PINS_KEY``). The contract wins where a task somehow has both.
    """
    if not isinstance(metadata, Mapping):
        return {}
    contract = metadata.get("workflow_contract")
    pins = contract.get("skill_sha256") if isinstance(contract, Mapping) else None
    if not isinstance(pins, Mapping) or not pins:
        pins = metadata.get(RUN_TASK_SKILL_PINS_KEY)
    if not isinstance(pins, Mapping):
        return {}
    return {
        str(name).strip(): str(value).strip().lower()
        for name, value in pins.items()
        if str(name or "").strip() and str(value or "").strip()
    }


def frozen_skill_kwargs(metadata: Any) -> dict[str, Any]:
    """``build_skills_summary``'s ``skill_sha256`` for a task, passed only when it has pins, so a call for any other
    task stays the call it was (and a stand-in skill source without the parameter keeps working)."""
    pins = frozen_skill_sha256(metadata)
    return {"skill_sha256": pins} if pins else {}


@dataclass
class Skill:
    name: str
    description: str = ""
    always: bool = False
    content: str = ""
    source_path: str = ""
    level: str = "system"  # "system" or "project"
    metadata: dict[str, Any] = field(default_factory=dict)
    # Execution modes under which this skill is visible at all. Empty list
    # means "visible everywhere" (backward compat). Non-empty list means
    # the skill is filtered out entirely — body *and* description — when
    # the current execution mode is not in the list. Use this for skills
    # that are only meaningful under a specific runtime context, e.g. a
    # collaboration playbook that only applies in company_mode.
    modes: list[str] = field(default_factory=list)


def parse_skill_text(text: str, *, source_path: str, default_name: str, level: str = "system") -> Skill:
    """Parse already-owned bytes without a second filesystem read."""
    frontmatter = {}; content = text
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            raise ValueError('invalid_skill_frontmatter') from None
        content = text[fm_match.end():]
    if type(frontmatter) is not dict:
        raise ValueError("invalid_skill_frontmatter")
    raw_modes = frontmatter.get("modes", [])
    modes = ([raw_modes.strip()] if raw_modes.strip() else []) if isinstance(raw_modes,str) else (
        [str(m).strip() for m in raw_modes if str(m).strip()] if isinstance(raw_modes,list) else [])
    return Skill(name=frontmatter.get("name",default_name), description=frontmatter.get("description",""),
                 always=frontmatter.get("always",False), content=content.strip(), source_path=source_path,
                 level=level, metadata=frontmatter.get("metadata",{}) or {}, modes=modes)


class SkillLibrary:
    """Manages skills stored as ``<skill-name>/SKILL.md`` directories.

    Two-level loading:
      1. System skills — ``ampuly_home/skills/``  (shared across all projects)
      2. Project skills — ``ampuly_home/projects/<project_id>/skills/``

    Project skills with the same name override system skills.
    """

    def __init__(self, ampuly_home: Path) -> None:
        self.ampuly_home = ampuly_home
        self.system_skills_dir = ampuly_home / "skills"
        self.projects_dir = ampuly_home / "projects"
        self._skills: dict[str, Skill] = {}
        self._loaded_project_id: str | None = None
        # (name, fingerprint) pairs already warned about, so a running order's every turn does not repeat it.
        self._warned_frozen: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_all(self, project_id: str | None = None) -> None:
        """Scan system + project skill directories and load metadata."""
        self._skills.clear()
        self._loaded_project_id = project_id if is_valid_project_id(project_id) else None
        self._scan_dir(self.system_skills_dir, level="system")
        if self._loaded_project_id:
            project_skills_dir = self.projects_dir / self._loaded_project_id / "skills"
            self._scan_dir(project_skills_dir, level="project")
        logger.info(f"Loaded {len(self._skills)} skills")

    def _scan_dir(self, base: Path, level: str) -> None:
        if not base.exists():
            return
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            skill = self._parse_skill_file(skill_md, level=level)
            if skill and self._skill_visible_in_project(skill, self._loaded_project_id):
                self._skills[skill.name] = skill

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        skill = self._skills.get(name)
        return skill if skill and self._skill_visible_in_project(skill, self._loaded_project_id) else None

    def list_skills(self) -> list[Skill]:
        return [skill for skill in self._skills.values() if self._skill_visible_in_project(skill, self._loaded_project_id)]

    def get_skill_path(self, name: str) -> str | None:
        """Return the SKILL.md path for a given skill name."""
        skill = self.get(name)
        return skill.source_path if skill else None

    def _frozen_skill(self, skill: Skill, sha256: str) -> Skill:
        """Read a verified pinned body; a missing/corrupt copy never falls back.

        Legacy pins cover SKILL.md only. Full-tree resource isolation is the
        runtime-generation contract, not something this body hash establishes.
        Parse the same bytes that were verified, rather than a stale load cache.
        """
        if not sha256:
            return skill
        folder = Path(skill.source_path).parent.name if skill.source_path else skill.name
        kept = skill_version_dir(self.system_skills_dir, folder, sha256)
        if kept is None:
            raise RuntimeError("pinned_skill_unavailable")
        candidates = [self.system_skills_dir / folder / "SKILL.md", kept / "SKILL.md"]
        for path in candidates:
            raw = self._read_pinned_skill(path)
            if raw is None:
                continue
            canonical = raw if b"\x00" in raw else raw.replace(b"\r\n", b"\n")
            if hashlib.sha256(canonical).hexdigest() != sha256:
                continue
            try:
                frozen = parse_skill_text(raw.decode("utf-8").replace("\r\n", "\n"),
                    source_path=str(path), default_name=folder, level="system")
            except (ValueError, TypeError, AttributeError, UnicodeError, yaml.YAMLError):
                raise RuntimeError("pinned_skill_unavailable") from None
            return frozen
        raise RuntimeError("pinned_skill_unavailable")

    def _read_pinned_skill(self, path: Path) -> bytes | None:
        # One bounded pinned handle, a final name check, no lexical fallback,
        # no root/lock creation and no private body or filename in the error.
        from ampuly.layer4_tools.workspace_fs import SecureWorkspace, LexicalWorkspace
        try:
            relative = path.relative_to(self.system_skills_dir).as_posix()
            with SecureWorkspace.for_roots(str(self.system_skills_dir), str(self.system_skills_dir)) as workspace:
                if isinstance(workspace, LexicalWorkspace):
                    raise RuntimeError("pinned_skill_unavailable")
                target = workspace.resolve(relative, use_output_root=False)
                fd = workspace.open_read(target)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 512 * 1024:
                        raise RuntimeError("pinned_skill_unavailable")
                    raw = bytearray()
                    while len(raw) <= 512 * 1024:
                        block = os.read(fd, min(64 * 1024, 512 * 1024 + 1 - len(raw)))
                        if not block:
                            break
                        raw.extend(block)
                    current = workspace.open_read(target)
                    try:
                        stamp = lambda info: (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink)
                        if (len(raw) != before.st_size or stamp(before) != stamp(os.fstat(fd))
                                or stamp(before) != stamp(os.fstat(current))):
                            raise RuntimeError("pinned_skill_unavailable")
                    finally:
                        os.close(current)
                    return bytes(raw)
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise RuntimeError("pinned_skill_unavailable") from None

    def list_project_skills(self, project_id: str) -> list[Skill]:
        """List skills belonging to a specific project (for cross-project recommendations)."""
        if not is_valid_project_id(project_id):
            return []
        project_skills_dir = self.projects_dir / project_id / "skills"
        skills: list[Skill] = []
        if not project_skills_dir.exists():
            return skills
        for child in sorted(project_skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            skill = self._parse_skill_file(skill_md, level="project")
            if skill and self._skill_visible_in_project(skill, project_id):
                skills.append(skill)
        return skills

    def list_all_project_ids_with_skills(self) -> list[str]:
        """Return project IDs that have a skills/ directory with at least one skill."""
        result: list[str] = []
        if not self.projects_dir.exists():
            return result
        for child in sorted(self.projects_dir.iterdir()):
            if not child.is_dir():
                continue
            skills_dir = child / "skills"
            if skills_dir.exists() and any(
                (d / "SKILL.md").exists() for d in skills_dir.iterdir() if d.is_dir()
            ):
                result.append(child.name)
        return result

    # ------------------------------------------------------------------
    # Summary builder (for system prompt injection)
    # ------------------------------------------------------------------

    def build_skills_summary(
        self,
        project_id: str | None = None,
        *,
        execution_mode: str | None = None,
        role_id: str | None = None,
        user_facing: bool = False,
        final_decider_role_id: str | None = None,
        skill_sha256: Mapping[str, str] | None = None,
    ) -> str:
        """Build prompt text: always-on skill bodies + summary list for the rest.

        ``execution_mode`` is used to filter out skills whose frontmatter
        declared a restricted ``modes`` list. Skills with a non-empty
        ``modes`` list are hidden completely (both body and description)
        when the current ``execution_mode`` is not in that list. Skills
        with an empty ``modes`` list are always visible.

        ``skill_sha256`` is what the running order froze at launch (``frozen_skill_sha256``): a skill named there is
        offered from the body kept under that fingerprint -- its always-on text and the path the agent reads -- so a
        later launch, improvement or rollback that reinstalls the shared folder does not change it mid-run.
        """
        if project_id:
            self.load_all(project_id)
        elif not self._skills:
            self.load_all()

        always_parts: list[str] = []
        summary_lines: list[str] = []
        pins = dict(skill_sha256 or {})
        if (len(pins)>128 or any(type(name) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",name)
                or type(digest) is not str or not _SHA256_HEX.fullmatch(digest) for name,digest in pins.items())):
            raise RuntimeError("pinned_skill_unavailable")
        offered = list(self._skills.values())
        matched = set()
        for skill in offered:
            keys = {skill.name, Path(skill.source_path).parent.name if skill.source_path else skill.name} & pins.keys()
            if len({pins[k] for k in keys})>1:
                raise RuntimeError("pinned_skill_unavailable")
            matched.update(keys)
        for name in pins.keys() - matched:
            offered.append(Skill(name=name,source_path=str(self.system_skills_dir/name/"SKILL.md")))

        for skill in offered:
            # A launch pins (and keeps) a skill by its folder; the library lists it by the name its SKILL.md gives.
            pin = pins.get(skill.name) or pins.get(Path(skill.source_path).parent.name if skill.source_path else "")
            if pin:
                skill = self._frozen_skill(skill, pin)
            if not self._skill_visible_in_project(skill, project_id):
                continue
            if not self._skill_visible_in_mode(
                skill,
                execution_mode,
                role_id=role_id,
                user_facing=user_facing,
                final_decider_role_id=final_decider_role_id,
            ):
                continue
            if skill.always:
                always_parts.append(f"## Skill: {skill.name}\n{skill.content}")
            else:
                summary_lines.append(
                    f"- **{skill.name}**: {skill.description} [{skill.source_path}]"
                )

        parts: list[str] = []
        if summary_lines or always_parts:
            header = (
                "## Available Skills\n"
                "Below are available skills. To use a skill, read its SKILL.md with `file_read`.\n"
            )
            if summary_lines:
                header += "\n".join(summary_lines)
            parts.append(header)

        for ap in always_parts:
            parts.append(ap)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Mode filtering
    # ------------------------------------------------------------------

    def _skill_visible_in_project(self, skill: Skill, project_id: str | None) -> bool:
        """Learning assets require exact project provenance; ordinary shared/template skills keep their rules.

        This marker is written by EmployeeEvolutionManager, not a defense against a malicious local author.
        Old unmarked project-reflection skills stay on disk awaiting source review and are not offered to models.
        """
        metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
        if metadata.get("built_from") != "project_reflections":
            return True
        if not is_valid_project_id(project_id) or skill.level != "project" or metadata.get("project_id") != project_id:
            return False
        if not skill.source_path:
            return False
        return Path(skill.source_path).parent.parent == self.projects_dir / project_id / "skills"

    @staticmethod
    def _skill_visible_in_mode(
        skill: Skill,
        execution_mode: str | None,
        *,
        role_id: str | None = None,
        user_facing: bool = False,
        final_decider_role_id: str | None = None,
    ) -> bool:
        """Return True if the skill should be visible under ``execution_mode``.

        - Skills with no ``modes`` constraint are visible everywhere.
        - Skills with a ``modes`` list are visible only when the current
          ``execution_mode`` (normalized to a non-empty string) is in
          that list. A ``None`` or empty mode means the caller has not
          supplied a mode yet (e.g. top-level context loading before
          routing), and restricted skills are hidden in that case.
        """
        current = str(execution_mode or "").strip()
        if str(skill.name or "").strip() in RETIRED_SKILLS:
            return False
        if str(skill.name or "").strip() == "memory":
            if current == "task_mode":
                return True
            if current == "company_mode":
                current_role = str(role_id or "").strip()
                final_role = str(final_decider_role_id or "").strip()
                return bool(user_facing and current_role and final_role and current_role == final_role)
            return False

        allowed = [str(m).strip() for m in (skill.modes or []) if str(m).strip()]
        if not allowed:
            return True
        return bool(current) and current in allowed

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_skill_file(self, path: Path, level: str = "system") -> Skill | None:
        try:
            return parse_skill_text(path.read_text(encoding="utf-8"), source_path=str(path),
                                    default_name=path.parent.name, level=level)
        except Exception as e:
            logger.warning(f"Failed to parse skill {path}: {e}")
            return None

    # ------------------------------------------------------------------
    # Persistence helpers (for skill evolution / creation)
    # ------------------------------------------------------------------

    def save_skill(self, skill: Skill, project_id: str | None = None) -> None:
        """Save a skill to disk. Project skills go under projects/<id>/skills/."""
        if project_id:
            target_dir = self.projects_dir / project_id / "skills" / skill.name
        else:
            target_dir = self.system_skills_dir / skill.name
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "SKILL.md"

        fm: dict[str, Any] = {"name": skill.name, "description": skill.description}
        if skill.always:
            fm["always"] = True
        if skill.modes:
            fm["modes"] = list(skill.modes)
        if skill.metadata:
            fm["metadata"] = skill.metadata

        text = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n{skill.content}"
        path.write_text(text, encoding="utf-8")
        skill.source_path = str(path)
        skill.level = "project" if project_id else "system"
        self._skills[skill.name] = skill
        logger.info(f"Skill saved: {path}")

    def delete_skill(self, name: str) -> bool:
        skill = self._skills.get(name)
        if not skill or not skill.source_path:
            return False
        path = Path(skill.source_path)
        if not path.exists():
            return False
        import shutil
        skill_dir = path.parent
        shutil.rmtree(skill_dir, ignore_errors=True)
        self._skills.pop(name, None)
        logger.info(f"Skill deleted: {skill_dir}")
        return True
