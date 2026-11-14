"""Local worktree helpers for runtime-managed subagents."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ampuly.core.config import AmpulyConfig
from ampuly.layer4_tools.execution_context import build_task_execution_context, venv_python_path


def _product_root() -> Path:
    """The product's own source tree: the folder holding the running ``ampuly`` package."""
    import ampuly

    return Path(ampuly.__file__).resolve().parent.parent


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _guard_candidates() -> list[Path]:
    """Every root the isolation guards, as written -- one list, so the two guards below cannot drift apart.

    A project the owner pointed at another folder is one of these: without it a moved project falls
    outside every guarded root, and a subagent's isolation would treat it as an ordinary outside source -- copied
    whole, or handed to ``git worktree add`` if it happens to be a repository.
    """
    from ampuly.core.config import get_default_workplace_root, get_ampuly_home

    candidates: list[Path] = []
    for resolver in (_product_root, get_ampuly_home, get_default_workplace_root):
        try:
            candidates.append(Path(resolver()))
        except Exception:  # noqa: BLE001 - a root that cannot be read guards nothing
            continue
    try:
        from ampuly.core import project_paths

        # Raw, not resolved: _guard_spellings keeps both forms of each root on purpose.
        candidates.extend(Path(value) for value in project_paths.mapping(get_ampuly_home()).projects.values())
    except Exception:  # noqa: BLE001 - a mapping that cannot be read guards nothing extra
        pass
    return candidates


def _guarded_roots() -> list[Path]:
    """What a subagent's isolation never checks out or copies whole: the product, the AMPULY home, the data area."""
    roots: list[Path] = []
    for candidate in _guard_candidates():
        try:
            root = candidate.resolve()
        except Exception:  # noqa: BLE001 - a root that cannot be read guards nothing
            continue
        if root.exists() and root not in roots:
            roots.append(root)
    return roots


def _mapped_project_roots() -> list[Path]:
    """The folders the owner chose for individual projects, resolved and existing."""
    from ampuly.core.config import get_ampuly_home

    roots: list[Path] = []
    try:
        from ampuly.core import project_paths

        values = list(project_paths.mapping(get_ampuly_home()).projects.values())
    except Exception:  # noqa: BLE001
        return roots
    for value in values:
        try:
            root = Path(value).resolve()
        except Exception:  # noqa: BLE001
            continue
        if root.exists() and root not in roots:
            roots.append(root)
    return roots


# Project-level folders under the data area that belong to no single order (or to many): never copied for a subagent.
_PROJECT_LEVEL_DENY = frozenset({"orders", "delivery-history", "delivery-exports", "portfolio-exports"})
_COPY_SKIP_NAMES = frozenset({".git", ".venv", ".ampuly-venv", "node_modules", "__pycache__", ".pytest_cache"})
COPY_MAX_FILES = 20_000
COPY_MAX_BYTES = 1 << 30
_REPARSE_POINT = 0x400


def _copyable_in_data_area(source: Path) -> bool:
    """A folder inside the data area may be copied only when it is inside one order, or a plain project subfolder.

    The data area is the shared root *and* every folder the owner chose for an individual project: under the shared
    root the project is one level down, under a chosen root the folder itself is the project.
    """
    from ampuly.core.config import get_default_workplace_root

    parts: tuple[str, ...] = ()
    try:
        relative = source.resolve().relative_to(Path(get_default_workplace_root()).resolve())
    except (ValueError, OSError):
        relative = None
    if relative is not None:
        parts = relative.parts
        if len(parts) < 2:
            return False   # the data area itself, or a whole project
        parts = parts[1:]
    else:
        for root in _mapped_project_roots():
            try:
                inside = source.resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            parts = inside.parts
            if not parts:
                return False   # the whole project
            break
        else:
            return False
    project_level = parts[0]
    if project_level == "orders":
        return len(parts) >= 2   # inside one order (orders/<order>/...), never the orders folder itself
    return project_level not in _PROJECT_LEVEL_DENY and not project_level.startswith(".")


def _real_directory(path: Path) -> bool:
    """A directory that is itself (not a symlink, not a junction, not a reparse point)."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    import stat as _stat

    if not _stat.S_ISDIR(status.st_mode) or _stat.S_ISLNK(status.st_mode):
        return False
    if getattr(status, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    is_junction = getattr(path, "is_junction", None)
    return not (callable(is_junction) and is_junction())


def _safe_copy(source: Path, target: Path) -> dict[str, Any]:
    """Copy real files only: no symlink, junction or reparse point is followed, no git or environment folder is taken,
    and the copy stops (and is thrown away) past 20,000 files or 1 GiB. Returns {"copied", "bytes", "skipped"} or
    {"too_large": True}."""
    copied = size = skipped = 0
    stack = [(source, target)]
    while stack:
        here, there = stack.pop()
        there.mkdir(parents=True, exist_ok=True)
        try:
            entries = list(os.scandir(here))
        except OSError:
            skipped += 1
            continue
        for entry in entries:
            name = entry.name
            if name in _COPY_SKIP_NAMES or name.startswith(".ampuly"):
                continue
            try:
                status = entry.stat(follow_symlinks=False)
                junction = bool(getattr(entry, "is_junction", lambda: False)())
            except OSError:
                skipped += 1
                continue
            if entry.is_symlink() or junction or getattr(status, "st_file_attributes", 0) & _REPARSE_POINT:
                skipped += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), there / name))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            copied += 1
            size += int(status.st_size)
            if copied > COPY_MAX_FILES or size > COPY_MAX_BYTES:
                return {"too_large": True}
            try:
                shutil.copy2(entry.path, there / name, follow_symlinks=False)
            except OSError:
                skipped += 1
    return {"copied": copied, "bytes": size, "skipped": skipped}


def _git_config_flags(hooks_dir: str) -> list[str]:
    # A repository's own hooks and filesystem monitor never run for a subagent's checkout.
    return ["-c", f"core.hooksPath={hooks_dir}", "-c", "core.fsmonitor=false"]


async def _git(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec("git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _stderr = await proc.communicate()
    return int(proc.returncode or 0), stdout.decode("utf-8", errors="replace")


async def _outside_repository(source: Path, guarded: list[Path]) -> Path | None:
    """The git root to check out for a source outside every guarded root: its .git a real directory, and its common git
    directory not inside a guarded root either. None otherwise (the source is copied instead)."""
    root = await _find_git_root(source)
    if root is None or not _real_directory(root / ".git"):
        return None
    code, out = await _git("-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = Path(out.strip()) if code == 0 and out.strip() else None
    if common is None or any(_is_within(common, guard) for guard in guarded):
        return None
    return root


def _guard_spellings() -> list[Path]:
    """Each guarded root as written and as resolved, deepest first."""
    spellings: list[Path] = []
    for candidate in _guard_candidates():
        try:
            raw = Path(candidate)
            forms = (Path(os.path.abspath(raw)), raw.resolve())
        except Exception:  # noqa: BLE001 - a root that cannot be read guards nothing
            continue
        for form in forms:
            if form not in spellings:
                spellings.append(form)
    return sorted(spellings, key=lambda path: len(path.parts), reverse=True)


def _redirected_on_the_way(raw: Path) -> bool:
    """Whether a folder between the deepest guarded root and ``raw`` (``raw`` included) is a link, a junction or a
    reparse point.

    Resolving first would follow it and check the target instead: an order's ``output`` made a junction to another
    order passed as that order, and one to a folder outside everything was copied (data phase 1 review). The guarded
    root itself is not examined, so a data area reached through a link (a container mount) still works.
    """
    import stat as _stat

    absolute = Path(os.path.abspath(raw))
    for guard in _guard_spellings():
        try:
            below = absolute.relative_to(guard)
        except ValueError:
            continue
        current = guard
        for part in below.parts:
            current = current / part
            try:
                status = os.lstat(current)
            except OSError:
                return False  # missing: the checks after resolving say so
            if _stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & _REPARSE_POINT:
                return True
            is_junction = getattr(current, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
        return False
    return False


def _empty(source: Path, note: str = "") -> dict[str, Any]:
    temp_dir = Path(tempfile.mkdtemp(prefix="ampuly-native-v2-empty-"))
    info: dict[str, Any] = {"path": str(temp_dir), "git_root": "", "mode": "empty", "source": str(source)}
    if note:
        info["note"] = note
    return info


async def create_worktree(
    base_path: str | None,
    *,
    config: AmpulyConfig | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """An isolated folder for a subagent (data phase 0, hardened after its review).

    Never the product's source, the AMPULY home or the data area as a whole: a source that is one of them, or holds one,
    gets an empty folder. A source inside them is copied (real files only) when it is inside one order or a plain
    project subfolder, else empty -- never checked out with git. A source outside them all is a git worktree only when
    its.git is a real directory whose common git dir is outside them too, with the repository's hooks switched off;
    anything else is copied. A missing source or a failed copy is an empty folder, and no temporary folder is left.
    """
    raw = Path(base_path or os.getcwd())
    if _redirected_on_the_way(raw):
        return await _prepare_execution_environment(_empty(raw, "redirected"), config=config, on_progress=on_progress)
    source = raw.resolve()
    guarded = _guarded_roots()
    if any(_is_within(guard, source) for guard in guarded):
        # The product itself, the home, the data area -- or a folder holding one (no workspace given: the console's own
        # directory stood in). Nothing of it is copied or checked out.
        return await _prepare_execution_environment(_empty(source), config=config, on_progress=on_progress)
    if not source.is_dir():
        return await _prepare_execution_environment(_empty(source, "missing"), config=config, on_progress=on_progress)
    inside_guarded = any(_is_within(source, guard) for guard in guarded)
    if inside_guarded and not _copyable_in_data_area(source):
        return await _prepare_execution_environment(_empty(source), config=config, on_progress=on_progress)
    root = None if inside_guarded else await _outside_repository(source, guarded)
    if root is not None:
        temp_dir = Path(tempfile.mkdtemp(prefix="ampuly-native-v2-"))
        hooks = tempfile.mkdtemp(prefix="ampuly-native-v2-hooks-")
        code, out = await _git(*_git_config_flags(hooks), "-C", str(root), "worktree", "add", "--detach", str(temp_dir), "HEAD")
        if code == 0:
            info = {"path": str(temp_dir), "git_root": str(root), "mode": "git_worktree", "stdout": out, "hooks_dir": hooks}
            return await _prepare_execution_environment(info, config=config, on_progress=on_progress)
        shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(hooks, ignore_errors=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="ampuly-native-v2-copy-"))
    try:
        copied = await asyncio.to_thread(_safe_copy, source, temp_dir)
    except Exception as exc:  # noqa: BLE001 - a copy that fails leaves nothing behind
        shutil.rmtree(temp_dir, ignore_errors=True)
        return await _prepare_execution_environment(_empty(source, f"copy_failed:{type(exc).__name__}"), config=config,
                                                    on_progress=on_progress)
    if copied.get("too_large"):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return await _prepare_execution_environment(_empty(source, "too_large"), config=config, on_progress=on_progress)
    info = {"path": str(temp_dir), "git_root": "", "mode": "copy", "skipped": int(copied.get("skipped", 0))}
    return await _prepare_execution_environment(info, config=config, on_progress=on_progress)


async def cleanup_worktree(info: dict[str, Any] | None) -> None:
    if not info:
        return
    path = Path(str(info.get("path", "") or "")).resolve()
    mode = str(info.get("mode", "") or "")
    git_root = str(info.get("git_root", "") or "")
    hooks = str(info.get("hooks_dir", "") or "")
    if path.exists() and mode == "git_worktree" and git_root:
        flags = _git_config_flags(hooks or tempfile.mkdtemp(prefix="ampuly-native-v2-hooks-"))
        await _git(*flags, "-C", git_root, "worktree", "remove", "--force", str(path))
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if hooks:
        shutil.rmtree(hooks, ignore_errors=True)


async def _find_git_root(path: Path) -> Path | None:
    current = path
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


async def _prepare_execution_environment(
    info: dict[str, Any],
    *,
    config: AmpulyConfig | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    workspace = Path(str(info.get("path", "") or "")).resolve()
    info["execution_context"] = build_task_execution_context(workspace_root=workspace, config=config)
    if config is None:
        return info
    venv_cfg = config.system.native_runtime.execution_environment.worktree_venv
    if not venv_cfg.enabled:
        return info

    provider = _resolve_venv_provider(venv_cfg.provider)
    venv_path = workspace / str(venv_cfg.venv_dir or ".ampuly-venv")
    python_path = venv_python_path(venv_path)
    try:
        if not python_path.exists():
            create_args = _venv_create_args(provider, venv_path, system_site_packages=venv_cfg.system_site_packages)
            await _run_process(create_args, cwd=workspace, on_progress=on_progress)
        sync_commands = _build_sync_commands(
            workspace=workspace,
            python_path=python_path,
            provider=provider,
            editable_project=venv_cfg.editable_project,
            requirements_files=_detect_requirements_files(
                workspace=workspace,
                configured=venv_cfg.requirements_files,
                auto_detect=venv_cfg.auto_detect_requirements,
            ),
        )
        for command in sync_commands:
            await _run_process(command, cwd=workspace, on_progress=on_progress)
        info.update(
            {
                "venv_path": str(venv_path),
                "python_executable": str(python_path),
                "venv_provider": provider,
                "environment_prepared": True,
                "environment_sync_commands": len(sync_commands),
            }
        )
        info["execution_context"] = build_task_execution_context(
            workspace_root=workspace,
            config=config,
            venv_path=venv_path,
            python_executable=python_path,
            venv_provider=provider,
        )
    except Exception as exc:
        error = str(exc)
        info.update(
            {
                "venv_path": str(venv_path),
                "python_executable": str(python_path),
                "venv_provider": provider,
                "environment_prepared": False,
                "environment_error": error,
            }
        )
        info["execution_context"] = build_task_execution_context(
            workspace_root=workspace,
            config=config,
            venv_path=venv_path,
            python_executable=python_path,
            venv_provider=provider,
            preparation_error=error,
        )
        if venv_cfg.fail_if_prepare_fails:
            raise
    return info


def _resolve_venv_provider(provider: str) -> str:
    normalized = str(provider or "auto").strip().lower() or "auto"
    if normalized == "uv":
        return "uv"
    if normalized == "venv":
        return "venv"
    return "uv" if shutil.which("uv") else "venv"


def _venv_create_args(provider: str, venv_path: Path, *, system_site_packages: bool) -> list[str]:
    if provider == "uv":
        return ["uv", "venv", str(venv_path), "--python", sys.executable]
    args = [sys.executable, "-m", "venv", str(venv_path)]
    if system_site_packages:
        args.append("--system-site-packages")
    return args


def _detect_requirements_files(
    *,
    workspace: Path,
    configured: list[str],
    auto_detect: bool,
) -> list[Path]:
    candidates: list[Path] = []
    for entry in configured:
        text = str(entry or "").strip()
        if text:
            candidates.append((workspace / text).resolve())
    if auto_detect:
        for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements-ci.txt"):
            candidate = (workspace / name).resolve()
            if candidate.exists():
                candidates.append(candidate)
    seen: set[str] = set()
    resolved: list[Path] = []
    for item in candidates:
        key = str(item)
        if key in seen or not item.exists():
            continue
        seen.add(key)
        resolved.append(item)
    return resolved


def _build_sync_commands(
    *,
    workspace: Path,
    python_path: Path,
    provider: str,
    editable_project: bool,
    requirements_files: list[Path],
) -> list[list[str]]:
    commands: list[list[str]] = []
    if editable_project and (workspace / "pyproject.toml").exists():
        if provider == "uv" and shutil.which("uv"):
            commands.append(["uv", "pip", "install", "--python", str(python_path), "-e", str(workspace)])
        else:
            commands.append([str(python_path), "-m", "pip", "install", "-e", str(workspace)])
    for item in requirements_files:
        if provider == "uv" and shutil.which("uv"):
            commands.append(["uv", "pip", "install", "--python", str(python_path), "-r", str(item)])
        else:
            commands.append([str(python_path), "-m", "pip", "install", "-r", str(item)])
    return commands


async def _run_process(args: list[str], *, cwd: Path, on_progress: Any = None) -> None:
    if on_progress:
        try:
            await on_progress(f"[worktree-env] {' '.join(args[:4])}")
        except TypeError:
            await on_progress(f"[worktree-env] {' '.join(args[:4])}")
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    detail = stderr_text or stdout_text or f"exit code {proc.returncode}"
    raise RuntimeError(f"{' '.join(args[:4])} failed: {detail}")
