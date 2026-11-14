"""Execution context helpers for isolated runtime tool execution."""

from __future__ import annotations

import contextvars
import functools
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.core.config import AmpulyConfig
from ampuly.core.native_permissions import native_sandbox_profile, normalize_native_approval_level


_EXECUTION_CONTEXT_OVERRIDE: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("ampuly_tool_execution_context_override", default=None)
)


def install_execution_context_override(
    override: dict[str, Any],
) -> contextvars.Token:
    """Install a coroutine-local override for one tool invocation."""

    return _EXECUTION_CONTEXT_OVERRIDE.set(dict(override or {}))


def reset_execution_context_override(token: contextvars.Token) -> None:
    _EXECUTION_CONTEXT_OVERRIDE.reset(token)


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def venv_python_path(venv_path: str | Path) -> Path:
    base = Path(venv_path)
    if platform_key() == "windows":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def build_task_execution_context(
    *,
    workspace_root: str | Path | None,
    output_root: str | Path | None = None,
    comms_root: str | Path | None = None,
    config: AmpulyConfig | None = None,
    venv_path: str | Path | None = None,
    python_executable: str | Path | None = None,
    venv_provider: str = "",
    preparation_error: str = "",
    native_approval_level: str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root or os.getcwd()).resolve()
    output = Path(output_root).resolve() if output_root else workspace
    resolved_comms_root = Path(comms_root).resolve() if comms_root else (workspace / ".ampuly-comms")
    resolved_venv = Path(venv_path).resolve() if venv_path else None
    resolved_python = Path(python_executable).resolve() if python_executable else None
    return {
        "workspace_root": str(workspace),
        "output_root": str(output),
        "comms_root": str(resolved_comms_root),
        "venv_path": str(resolved_venv) if resolved_venv else "",
        "python_executable": str(resolved_python) if resolved_python else "",
        "venv_provider": str(venv_provider or "").strip(),
        "preparation_error": str(preparation_error or "").strip(),
        "sandbox": resolve_sandbox_config(config, native_approval_level=native_approval_level),
        "env_allowlist": subprocess_env_allowlist_enabled(config),
    }


def subprocess_env_allowlist_enabled(config: AmpulyConfig | None = None) -> bool:
    """Whether tool children inherit only the allow-listed environment.

    The switch exists for rollback only; without a config the new behaviour
    applies, so a task that never went through ``build_task_execution_context``
    is confined the same way as one that did.
    """
    if config is None:
        return True
    try:
        return bool(config.system.native_runtime.execution_environment.subprocess_env_allowlist)
    except AttributeError:
        return True


def ensure_task_execution_context(task: Any, config: AmpulyConfig | None = None) -> dict[str, Any]:
    metadata = getattr(task, "metadata", {}) or {}
    existing = dict(metadata.get("_execution_context", {}) or {})
    if existing:
        level = metadata.get("native_approval_level")
        if level not in (None, ""):
            existing["sandbox"] = resolve_sandbox_config(
                config, native_approval_level=str(level),
            )
            metadata["_execution_context"] = existing
            setattr(task, "metadata", metadata)
        return existing
    workspace_root = (
        str(metadata.get("workspace_root", "") or "").strip()
        or str(metadata.get("comms_workspace_root", "") or "").strip()
        or str(metadata.get("target_output_dir", "") or "").strip()
        or os.getcwd()
    )
    output_root = (
        str(metadata.get("output_root", "") or "").strip()
        or str(metadata.get("target_output_dir", "") or "").strip()
        or workspace_root
    )
    comms_root = (
        str(metadata.get("comms_root", "") or "").strip()
        or (str(Path(workspace_root).resolve() / ".ampuly-comms") if workspace_root else "")
    )
    context = build_task_execution_context(
        workspace_root=workspace_root,
        output_root=output_root,
        comms_root=comms_root,
        config=config,
        native_approval_level=str(metadata.get("native_approval_level", "") or "") or None,
    )
    metadata["_execution_context"] = context
    setattr(task, "metadata", metadata)
    return context


def resolve_task_execution_context(
    task: Any = None,
    *,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if task is None:
        return {}
    metadata = getattr(task, "metadata", {}) or {}
    context = dict(metadata.get("_execution_context", {}) or {})
    if not context:
        workspace_root = (
            str(metadata.get("workspace_root", "") or "").strip()
            or str(metadata.get("comms_workspace_root", "") or "").strip()
            or str(metadata.get("target_output_dir", "") or "").strip()
        )
        if workspace_root:
            output_root = (
                str(metadata.get("output_root", "") or "").strip()
                or str(metadata.get("target_output_dir", "") or "").strip()
                or workspace_root
            )
            context = {
                "workspace_root": str(Path(workspace_root).resolve()),
                "output_root": str(Path(output_root).resolve()),
                "comms_root": str(Path(workspace_root).resolve() / ".ampuly-comms"),
            }

    inherited = metadata.get("inherited_environment")
    if isinstance(inherited, dict):
        env_vars = inherited.get("env_vars")
        if isinstance(env_vars, dict) and env_vars:
            existing = dict(context.get("inherited_env_vars", {}) or {})
            existing.update(env_vars)
            context["inherited_env_vars"] = existing
    manifest = metadata.get("environment_manifest")
    if isinstance(manifest, dict):
        env_vars = manifest.get("env_vars")
        if isinstance(env_vars, dict) and env_vars:
            existing = dict(context.get("inherited_env_vars", {}) or {})
            existing.update(env_vars)
            context["inherited_env_vars"] = existing

    if not context.get("sandbox") and sandbox_wrapper_available():
        # Tasks that carry only workspace metadata never went through
        # build_execution_context, so nothing attached a sandbox profile. Without
        # one the caller falls back to mode "off" and runs unconfined even though
        # the permission layer already counted the sandbox as available. The
        # profile is only attached where confinement can actually be applied:
        # attaching one on a host with no wrapper turns every command into a
        # fail-closed error instead of the approval prompt that guards it there.
        level = metadata.get("native_approval_level")
        if level not in (None, ""):
            context["sandbox"] = resolve_sandbox_config(
                native_approval_level=str(level),
            )

    # Applied after the derived sandbox above, so an approved per-call grant wins
    # over the default we just filled in. Parallel tool calls each carry their own
    # grant, so the override is coroutine-local and nothing is written back onto
    # the shared Task.
    active_override = _EXECUTION_CONTEXT_OVERRIDE.get()
    for values in (active_override, override):
        if not values:
            continue
        for key, value in values.items():
            context[str(key)] = dict(value) if isinstance(value, dict) else value

    return context


def resolve_sandbox_config(
    config: AmpulyConfig | None = None,
    *,
    native_approval_level: str | None = None,
) -> dict[str, Any]:
    platform = platform_key()
    if native_approval_level not in (None, ""):
        profile = native_sandbox_profile(
            normalize_native_approval_level(native_approval_level)
        )
        profile["platform"] = platform
        return profile
    if config is None:
        return {
            "platform": platform,
            "enabled": False,
            "mode": "off",
            "wrapper": "none",
            "fail_if_unavailable": False,
            "allow_direct_fallback": True,
            "allow_network": True,
        }
    sandbox_cfg = config.system.native_runtime.execution_environment.sandbox
    platform_cfg = getattr(sandbox_cfg, platform)
    mode = platform_cfg.mode if platform_cfg.mode != "inherit" else sandbox_cfg.default_mode
    wrapper = platform_cfg.wrapper
    if not sandbox_cfg.enabled:
        mode = "off"
        wrapper = "none"
    return {
        "platform": platform,
        "enabled": bool(sandbox_cfg.enabled),
        "mode": mode,
        "wrapper": wrapper,
        "fail_if_unavailable": bool(sandbox_cfg.fail_if_unavailable),
        "allow_direct_fallback": bool(sandbox_cfg.allow_direct_fallback),
        "allow_network": bool(sandbox_cfg.allow_network),
    }


# What a tool child may inherit from the engine's own environment. Everything
# else stays out: the engine process carries the operator's model keys, the
# notification channel secrets (AMPULY_NOTIFY_FEISHU_APP_SECRET and friends) and
# whatever the shell that started it happened to hold, and until 2026-10-07 all
# of it was copied into every shell_exec / python_exec child. Names are matched
# upper-cased, so ``http_proxy`` and ``HTTP_PROXY`` both pass. The external
# agent adapters (adapters/base.py) already launch their Providers this way;
# this is the same line drawn for layer4 tools. Low-integrity confinement keeps
# its suffix deny list (windows_lowil._without_secrets) as a second line behind
# this one.
_INHERITED_ENV_NAMES = frozenset({
    # Process start-up: the interpreter, the shell, temp and the user profile.
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "USERNAME", "USER", "LOGNAME", "COMPUTERNAME", "OS",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PSMODULEPATH",
    "SHELL", "TERM", "TZ",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    # Locale. Enumerated rather than ``LC_*``: LC_ is not an authentication
    # boundary, and a LC_PRIVATE_TOKEN would ride along on a prefix match.
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES",
    "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "LC_ADDRESS", "LC_IDENTIFICATION",
    "LC_MEASUREMENT", "LC_NAME", "LC_PAPER", "LC_TELEPHONE",
    # Proxies.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    # Python / virtualenv: exact launch names only. Arbitrary PYTHON*, UV_*
    # or PIP_* names can hold customer credentials. Index URLs/configuration
    # (which may embed authentication) are not inherited implicitly; an
    # explicitly authorized task env_vars grant remains a separate overlay.
    "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    "PYENV_ROOT", "PYENV_VERSION",
    "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "UV_PROJECT_ENVIRONMENT", "UV_VIRTUAL_ENV",
    # The real broker/CLI collaboration contract, enumerated rather than
    # matching a whole family. The token identifies this runtime RPC channel,
    # not a provider/customer credential; other token-looking names stay out.
    "AMPULY_COLLAB_RPC_PATH", "AMPULY_COLLAB_RPC_TOKEN", "AMPULY_COLLAB_RPC_TRANSPORT",
    "AMPULY_COLLAB_RPC_HOST", "AMPULY_COLLAB_RPC_PORT",
    "AMPULY_COMMS_FROM", "AMPULY_COMMS_PROJECT", "AMPULY_COMMS_SESSION", "AMPULY_COMMS_ROOT",
    # Runtime context a child running ``ampuly`` / ``ampuly-collab`` needs to find
    # the same home/project. Channel secrets (AMPULY_NOTIFY_*, AMPULY_WECOM_RELAY_TOKEN,
    # AMPULY_FEISHU_*) are deliberately not here.
    "AMPULY_HOME", "AMPULY_WORKPLACE_ROOT", "AMPULY_WORKPLACE_ROOT_DEFAULT",
    "AMPULY_RESPONSE_LANGUAGE",
    "AMPULY_COLLAB_PROFILE", "AMPULY_COLLAB_CLI", "AMPULY_ALLOWED_COLLAB_TOOLS",
    "AMPULY_MAILBOX_MODE",
    "AMPULY_WORKSPACE_ROOT", "AMPULY_OUTPUT_ROOT", "AMPULY_PROJECT_DB_PATH",
    "AMPULY_TASK_ID", "AMPULY_RUNTIME_TASK_ID", "AMPULY_WORK_ITEM_ID",
    "AMPULY_MEMORY_ROOT", "AMPULY_GLOBAL_MEMORY_PATH", "AMPULY_PROJECT_MEMORY_PATH",
})
# SUPERONES_<X> is the product's spelling of AMPULY_<X> (ampuly/core/env_alias.py);
# whatever is allowed under the old name is allowed under the new one.
_PRODUCT_ENV_PREFIX = "SUPERONES_"
_UPSTREAM_ENV_PREFIX = "AMPULY_"


def subprocess_env_inherits(name: str) -> bool:
    """Whether one variable of the engine's environment reaches a tool child."""
    upper = str(name or "").strip().upper()
    if not upper:
        return False
    if upper.startswith(_PRODUCT_ENV_PREFIX):
        upper = _UPSTREAM_ENV_PREFIX + upper[len(_PRODUCT_ENV_PREFIX):]
    return upper in _INHERITED_ENV_NAMES


def inherited_subprocess_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """The allow-listed slice of ``source`` (the engine's environment by default)."""
    base = source if source is not None else os.environ
    return {str(name): str(value) for name, value in base.items() if subprocess_env_inherits(name)}


def build_subprocess_env(context: dict[str, Any] | None = None) -> dict[str, str]:
    context = dict(context or {})
    # ``env_allowlist`` is set by build_task_execution_context from the config
    # switch; a context without it (bare workspace metadata, a direct call)
    # gets the confined behaviour, not the old copy of everything.
    if bool(context.get("env_allowlist", True)):
        env = inherited_subprocess_env()
    else:
        env = dict(os.environ)

    inherited_env = dict(context.get("inherited_env_vars", {}) or {})
    if inherited_env:
        env.update({str(k): str(v) for k, v in inherited_env.items()})

    venv_path = str(context.get("venv_path", "") or "").strip()
    python_path = str(context.get("python_executable", "") or "").strip()
    if not venv_path or not python_path:
        return env
    if not Path(python_path).exists():
        return env
    bin_dir = str(Path(python_path).resolve().parent)
    env["VIRTUAL_ENV"] = venv_path
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env.setdefault("UV_PROJECT_ENVIRONMENT", venv_path)
    return env


def _is_usable_interpreter(candidate: str | None) -> bool:
    """Whether a PATH hit is a real interpreter rather than a placeholder.

    Windows ships app-execution aliases: zero-length reparse points in
    WindowsApps that open the Store instead of running anything. `which` finds
    them first, so `python3` resolves to a stub that cannot be stat'd — which
    surfaces much later as "company executable is unavailable" and rejects
    every python_exec call on an otherwise healthy machine.
    """
    if not candidate:
        return False
    try:
        resolved = Path(candidate).resolve(strict=True)
        return resolved.is_file() and resolved.stat().st_size > 0
    except OSError:
        return False


def resolve_python_executable(context: dict[str, Any] | None = None) -> str:
    context = dict(context or {})
    configured = str(context.get("python_executable", "") or "").strip()
    if configured and Path(configured).exists():
        return configured
    for candidate in (shutil.which("python3"), shutil.which("python")):
        if _is_usable_interpreter(candidate):
            return candidate  # type: ignore[return-value]
    return sys.executable


def wrap_command_for_context(
    args: list[str],
    *,
    cwd: str,
    context: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    context = dict(context or {})
    sandbox = dict(context.get("sandbox", {}) or {})
    mode = str(sandbox.get("mode", "off") or "off").strip().lower() or "off"
    requested_wrapper = str(sandbox.get("wrapper", "auto") or "auto").strip().lower() or "auto"
    platform = str(sandbox.get("platform", "") or platform_key()).strip() or platform_key()
    meta = {
        "platform": platform,
        "requested_mode": mode,
        "effective_mode": mode,
        "requested_wrapper": requested_wrapper,
        "effective_wrapper": "none",
        "available": True,
        "fallback_used": False,
    }
    if mode in {"", "off", "elevated"}:
        return args, meta
    wrapper = requested_wrapper
    if wrapper in {"", "auto"}:
        if platform == "linux" and shutil.which("bwrap"):
            wrapper = "bwrap"
        elif platform == "macos" and shutil.which("sandbox-exec"):
            wrapper = "sandbox-exec"
        elif platform == "windows":
            wrapper = _windows_wrapper(context, cwd)
        else:
            wrapper = "none"
    meta["effective_wrapper"] = wrapper
    if wrapper == "bwrap":
        return _wrap_with_bwrap(args, cwd=cwd, context=context, meta=meta), meta
    if wrapper == "sandbox-exec":
        return _wrap_with_sandbox_exec(args, cwd=cwd, context=context, meta=meta), meta
    if wrapper in {"appcontainer", "lowil"}:
        # Both Windows backends launch the command themselves through
        # CreateProcessAsUserW rather than by prefixing an argv, so the args
        # pass through unchanged and the caller dispatches on the wrapper name.
        meta["guarantees"] = windows_guarantees(wrapper)
        return args, meta
    return _handle_unavailable_sandbox(args, sandbox=sandbox, meta=meta)


def windows_backend() -> str:
    """Which Windows confinement backend to use: lowil, appcontainer, or off.

    Low integrity is the default because it is the only one of the two that
    keeps reads honest. AppContainer confines more — it is the only mechanism
    on Windows that can also deny the network — but it denies reads outside the
    workspace as part of the same access check, and a confined PowerShell there
    reports 0 entries, exit 0, for a directory holding 28 files. Wrong answers
    are worse for an agent than a refusal, so it is available only on request.
    """
    setting = str(os.environ.get("AMPULY_WINDOWS_SANDBOX", "")).strip().lower()
    if setting in {"appcontainer", "container"}:
        return "appcontainer"
    if setting in {"0", "false", "no", "off", "none"}:
        return "off"
    return "lowil"


def windows_guarantees(wrapper: str) -> dict[str, bool]:
    """What a Windows backend actually enforces.

    Callers waive their human approval prompt on the strength of confinement,
    and neither backend delivers all three guarantees, so they are reported
    separately rather than collapsed into one boolean.
    """
    if wrapper == "appcontainer":
        return {"filesystem": True, "lifetime": True, "network": True}
    if wrapper == "lowil":
        return {"filesystem": True, "lifetime": True, "network": False}
    return {"filesystem": False, "lifetime": False, "network": False}


@functools.lru_cache(maxsize=4)
def _lowil_confines(_gate: str) -> bool:
    """Whether low-integrity confinement actually holds on this host.

    Proven once per process by running a confined write inside and outside a
    throwaway workspace and looking at the disk, not at exit codes. Reporting a
    sandbox that does not confine is worse than reporting none, because the
    permission layer waives its approval prompt on the strength of the report.
    """
    try:
        from ampuly.layer4_tools.windows_lowil import lowil_supported, selftest_confinement
    except ImportError:
        return False
    if not lowil_supported():
        return False
    probe = Path(tempfile.mkdtemp(prefix="ampuly-confinement-"))
    try:
        report = selftest_confinement(str(probe))
    except Exception as exc:  # a host we cannot confine on is not a crash
        logger.warning(f"confinement self-test failed: {exc}")
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    if not report.get("filesystem"):
        logger.warning(f"confinement self-test did not confine: {report.get('detail')}")
        return False
    return True


def _windows_wrapper(context: dict[str, Any], cwd: str) -> str:
    """Resolve which Windows backend can be applied to this command."""
    backend = windows_backend()
    if backend == "appcontainer":
        return "appcontainer" if _appcontainer_ready(context, cwd) else "none"
    if backend == "lowil" and _lowil_confines(backend):
        workspace = str(context.get("workspace_root") or cwd or "").strip()
        try:
            from ampuly.layer4_tools.windows_lowil import prepare_workspace
        except ImportError:
            return "none"
        if workspace and prepare_workspace(workspace):
            return "lowil"
    return "none"


def sandbox_wrapper_available() -> bool:
    """Whether this host can actually confine a command.

    The permission layer waives its approval prompt precisely when a sandbox
    exists, so it and the execution layer have to answer this question the
    same way: a sandbox reported here but not applied at execution time would
    waive approval and confine nothing.
    """
    if sys.platform.startswith("linux"):
        return bool(shutil.which("bwrap"))
    if sys.platform == "darwin":
        return bool(shutil.which("sandbox-exec"))
    if sys.platform == "win32":
        backend = windows_backend()
        if backend == "appcontainer":
            try:
                from ampuly.layer4_tools.windows_sandbox import sandbox_supported
            except ImportError:
                return False
            return sandbox_supported()
        return backend == "lowil" and _lowil_confines(backend)
    return False


def _appcontainer_ready(context: dict[str, Any], cwd: str) -> bool:
    """Whether AppContainer confinement is usable, granting the workspace if so."""
    if windows_backend() != "appcontainer":
        return False
    try:
        from ampuly.layer4_tools.windows_sandbox import (
            ensure_system_read_access,
            grant_workspace,
            sandbox_supported,
        )
    except ImportError:
        return False
    if not sandbox_supported():
        return False
    workspace = str(context.get("workspace_root") or cwd or "").strip()
    if not workspace:
        return False
    try:
        ensure_system_read_access()
        return grant_workspace(workspace)
    except Exception:
        return False


def _handle_unavailable_sandbox(
    args: list[str],
    *,
    sandbox: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    meta["available"] = False
    allow_direct = bool(sandbox.get("allow_direct_fallback", True))
    if bool(sandbox.get("fail_if_unavailable", False)) and not allow_direct:
        raise RuntimeError(
            f"Sandbox mode `{meta['requested_mode']}` is unavailable on {meta['platform']} and direct fallback is disabled."
        )
    meta["fallback_used"] = True
    meta["effective_mode"] = "off"
    meta["effective_wrapper"] = "none"
    return args, meta


def _wrap_with_bwrap(
    args: list[str],
    *,
    cwd: str,
    context: dict[str, Any],
    meta: dict[str, Any],
) -> list[str]:
    workspace = str(Path(context.get("workspace_root") or cwd).resolve())
    wrapped = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
    ]
    sandbox = dict(context.get("sandbox", {}) or {})
    if str(sandbox.get("mode", "") or "").strip().lower() != "read-only":
        wrapped.extend(["--bind", workspace, workspace])
    for raw_path in list(sandbox.get("additional_write_paths", []) or []):
        try:
            write_path = str(Path(raw_path).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        if write_path == workspace or write_path.startswith(f"{workspace}{os.sep}"):
            continue
        wrapped.extend(["--bind", write_path, write_path])
    wrapped.extend([
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        cwd,
    ])
    if not bool(sandbox.get("allow_network", True)):
        wrapped.append("--unshare-net")
    wrapped.extend(args)
    meta["available"] = True
    return wrapped


def _wrap_with_sandbox_exec(
    args: list[str],
    *,
    cwd: str,
    context: dict[str, Any],
    meta: dict[str, Any],
) -> list[str]:
    sandbox = dict(context.get("sandbox", {}) or {})
    workspace = str(Path(context.get("workspace_root") or cwd).resolve())
    escaped_workspace = workspace.replace("\\", "\\\\").replace('"', '\\"')
    allow_network = bool(sandbox.get("allow_network", True))
    profile_lines = [
        "(version 1)",
        "(deny default)",
        "(import \"system.sb\")",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow file-read*)",
    ]
    if str(sandbox.get("mode", "") or "").strip().lower() == "read-only":
        profile_lines.append('(allow file-write* (subpath "/tmp") (subpath "/private/tmp"))')
    else:
        profile_lines.append(
            f'(allow file-write* (subpath "{escaped_workspace}") (subpath "/tmp") (subpath "/private/tmp"))'
        )
    for raw_path in list(sandbox.get("additional_write_paths", []) or []):
        try:
            write_path = str(Path(raw_path).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        escaped_path = write_path.replace("\\", "\\\\").replace('"', '\\"')
        profile_lines.append(f'(allow file-write* (subpath "{escaped_path}"))')
    if allow_network:
        profile_lines.append("(allow network*)")
    meta["available"] = True
    return ["sandbox-exec", "-p", "\n".join(profile_lines), *args]
