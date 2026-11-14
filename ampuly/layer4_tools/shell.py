"""Structured shell execution tools."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

from ampuly.layer4_tools.execution_context import (
    build_subprocess_env,
    resolve_task_execution_context,
    wrap_command_for_context,
)
from ampuly.layer4_tools.opaque_execution import (
    OpaqueExecutionEnvelopeError,
    OpaqueExecutionPlan,
    current_opaque_execution_plan,
    verify_opaque_execution_plan,
)
from ampuly.layer4_tools.registry import COMPANY_EFFECT_OPAQUE_EXACT, ToolDefinition
from ampuly.layer2_organization.work_item_identity import work_item_turn_type_from_metadata


_STDOUT_LIMIT = 50_000
_STDERR_LIMIT = 20_000
_SETUP_STAGE_DEFAULT_TIMEOUT = 1800
_DEFAULT_SHELL_TIMEOUT = 300
_POWERSHELL_CMD_SEPARATOR = " ; "
_BASH_CMD_SEPARATOR = " && "
_STREAM_READ_SIZE = 8192


def _resolve_working_directory(
    working_directory: str | None = None,
    task: Any | None = None,
) -> str:
    cwd = str(working_directory or "").strip()
    if not cwd and task is not None:
        metadata = getattr(task, "metadata", {}) or {}
        execution_context = dict(metadata.get("_execution_context", {}) or {})
        candidates = [
            str(execution_context.get("workspace_root", "") or "").strip(),
            str(execution_context.get("output_root", "") or "").strip(),
            str(metadata.get("workspace_root", "") or "").strip(),
            str(metadata.get("comms_workspace_root", "") or "").strip(),
            str(metadata.get("output_root", "") or "").strip(),
            str(metadata.get("target_output_dir", "") or "").strip(),
        ]
        fallback = ""
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.exists() and path.is_dir():
                return str(path)
            if not fallback:
                fallback = str(path)
        cwd = fallback
    return cwd or os.getcwd()


def _shell_binary(preferred: str, fallback: str) -> str:
    return shutil.which(preferred) or fallback


async def _run_shell_command(
    *,
    shell_name: str,
    command: str,
    args: list[str],
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    task: Any | None = None,
    on_progress: Any = None,
    frozen_plan: OpaqueExecutionPlan | None = None,
) -> dict[str, Any]:
    if frozen_plan is not None:
        resolved_cwd = frozen_plan.cwd
        context = frozen_plan.context
        command = frozen_plan.effective_command
        args = list(frozen_plan.argv)
        env = frozen_plan.environment
        sandbox_meta = frozen_plan.sandbox
        timeout = frozen_plan.timeout
        preparation_error = frozen_plan.preparation_error
    else:
        cwd = _resolve_working_directory(working_directory, task)
        cwd_path = Path(cwd).expanduser()
        resolved_cwd = str(cwd_path.resolve(strict=False))
        preparation_error = ""
        if not cwd_path.exists() or not cwd_path.is_dir():
            preparation_error = f"Working directory does not exist: {resolved_cwd}"
        if task is not None:
            meta = getattr(task, "metadata", {}) or {}
            override = meta.get("shell_timeout_override")
            if override is not None:
                try:
                    timeout = max(int(override), timeout)
                except (ValueError, TypeError):
                    pass
            elif work_item_turn_type_from_metadata(meta, fallback="") == "setup":
                timeout = max(timeout, _SETUP_STAGE_DEFAULT_TIMEOUT)
            shell_prefix = ""
            shell_prefix_win = ""
            inherited = meta.get("inherited_environment")
            if isinstance(inherited, dict):
                shell_prefix = str(inherited.get("shell_prefix", "") or "").strip()
                shell_prefix_win = str(inherited.get("shell_prefix_win", "") or "").strip()
            if not shell_prefix:
                manifest = meta.get("environment_manifest")
                if isinstance(manifest, dict):
                    shell_prefix = str(manifest.get("shell_prefix", "") or "").strip()
                    shell_prefix_win = str(manifest.get("shell_prefix_win", "") or "").strip()
            is_powershell = shell_name == "powershell"
            active_prefix = shell_prefix_win if (is_powershell and shell_prefix_win) else shell_prefix
            if active_prefix and active_prefix not in command:
                separator = _POWERSHELL_CMD_SEPARATOR if is_powershell else _BASH_CMD_SEPARATOR
                command = f"{active_prefix}{separator}{command}"
                # Replace only the trailing command argument. PowerShell args are
                # [exe, "-NoProfile", "-Command", command] (4 elements); the previous
                # ``[args[0], args[1], command]`` dropped the "-Command" flag and left
                # PowerShell unable to interpret the prefixed command. Bash args are
                # [bash, "-lc", command] (3 elements), handled identically here.
                args = [*args[:-1], command] if len(args) >= 1 else args
        context = resolve_task_execution_context(task)
        if resolved_cwd and not context.get("workspace_root"):
            context["workspace_root"] = resolved_cwd
        env = build_subprocess_env(context)
        try:
            args, sandbox_meta = wrap_command_for_context(
                args,
                cwd=resolved_cwd,
                context=context,
            )
        except RuntimeError as exc:
            preparation_error = str(exc)
            sandbox_cfg = dict(context.get("sandbox", {}) or {})
            sandbox_meta = {
                "platform": str(sandbox_cfg.get("platform", "") or ""),
                "requested_mode": str(sandbox_cfg.get("mode", "") or ""),
                "effective_mode": "off",
                "available": False,
                "fallback_used": False,
            }

    if preparation_error:
        return {
            "success": False,
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": False,
            "error": preparation_error,
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }
    if frozen_plan is not None:
        try:
            verify_opaque_execution_plan(frozen_plan)
        except OpaqueExecutionEnvelopeError as exc:
            return {
                "success": False,
                "shell": shell_name,
                "command": command,
                "cwd": resolved_cwd,
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "timed_out": False,
                "error": str(exc),
                "sandbox": sandbox_meta,
                "execution_context": _context_preview(context),
            }
    wrapped_args = list(args)

    # Windows confinement runs through AppContainer, which needs CreateProcessW
    # with security attributes and therefore cannot go through
    # create_subprocess_exec. The wrapper reports itself here so the result
    # still carries an honest sandbox record.
    effective_wrapper = str(sandbox_meta.get("effective_wrapper", ""))
    if effective_wrapper in {"appcontainer", "lowil"}:
        if effective_wrapper == "lowil":
            from ampuly.layer4_tools.windows_lowil import (
                ConfinementUnavailable as SandboxUnavailable,
                run_shell_confined,
            )

            def _launch() -> dict[str, Any]:
                # The argv the shell layer already resolved — [powershell,
                # -NoProfile, -Command, cmd] or [bash, -lc, cmd] — is passed
                # through. Re-wrapping it in cmd.exe here would silently swap
                # the shell the runtime chose, which is how `find` once became
                # Windows' string-search find.exe while the result still
                # reported that PowerShell had run it.
                return run_shell_confined(
                    wrapped_args, cwd=resolved_cwd, timeout=timeout, env=env,
                    # Match the direct asyncio pipe path (utf-8) and the PowerShell
                    # UTF-8 preamble: never decode confined stdout as the ANSI codepage.
                    encoding="utf-8",
                    **({"sealed_environment": frozen_plan.lowil_environment_seal}
                       if frozen_plan is not None else {}),
                )
        else:
            from ampuly.layer4_tools.windows_sandbox import (  # type: ignore[assignment]
                SandboxUnavailable,
                run_shell_confined,
            )

            def _launch() -> dict[str, Any]:
                # The same allow-listed environment the direct and lowil
                # paths get; without it the container inherited the
                # engine's whole environment.
                return run_shell_confined(command, cwd=resolved_cwd, timeout=timeout, env=env)

        try:
            confined = await asyncio.to_thread(_launch)
        except SandboxUnavailable as exc:
            return {
                "success": False,
                "shell": shell_name,
                "command": command,
                "cwd": resolved_cwd,
                "stdout": "",
                "stderr": str(exc),
                "exit_code": -1,
                "timed_out": False,
                "error": f"sandbox unavailable: {exc}",
                "sandbox": sandbox_meta,
                "execution_context": _context_preview(context),
            }
        # The low-integrity backend reads stdout and stderr through separate
        # pipes; the AppContainer one has a single combined capture.
        output = str(confined.get("stdout", confined.get("output", "")))[:_STDOUT_LIMIT]
        errors = str(confined.get("stderr", ""))[:_STDOUT_LIMIT]
        if on_progress and output:
            try:
                await on_progress(output.rstrip("\r\n"), stream="stdout")
            except TypeError:
                await on_progress(output.rstrip("\r\n"))
        return {
            "success": confined.get("exit_code") == 0 and not confined.get("timed_out"),
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": output,
            "stderr": errors,
            "exit_code": int(confined.get("exit_code", -1)),
            "timed_out": bool(confined.get("timed_out")),
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *wrapped_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
            env=env,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        async def _pump(stream: Any, bucket: list[str], stream_name: str, limit: int) -> None:
            async for chunk in _iter_stream_lines(stream):
                text = chunk.decode("utf-8", errors="replace")
                bucket.append(text)
                joined = "".join(bucket)
                if len(joined) > limit:
                    bucket[:] = [joined[:limit]]
                if on_progress:
                    try:
                        await on_progress(text.rstrip("\r\n"), stream=stream_name)
                    except TypeError:
                        await on_progress(text.rstrip("\r\n"))

        stdout_task = asyncio.create_task(_pump(proc.stdout, stdout_chunks, "stdout", _STDOUT_LIMIT))
        stderr_task = asyncio.create_task(_pump(proc.stderr, stderr_chunks, "stderr", _STDERR_LIMIT))
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        await asyncio.gather(stdout_task, stderr_task)
        stdout = "".join(stdout_chunks)[:_STDOUT_LIMIT]
        stderr = "".join(stderr_chunks)[:_STDERR_LIMIT]
        return {
            "success": proc.returncode == 0,
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": proc.returncode,
            "timed_out": False,
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        return {
            "success": False,
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": True,
            "error": f"Command timed out after {timeout}s",
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }


async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    buffer = bytearray()
    while True:
        chunk = await stream.read(_STREAM_READ_SIZE)
        if not chunk:
            if buffer:
                yield bytes(buffer)
            return
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            line = bytes(buffer[: newline_index + 1])
            del buffer[: newline_index + 1]
            yield line


def _context_preview(context: dict[str, Any]) -> dict[str, Any]:
    sandbox = dict(context.get("sandbox", {}) or {})
    return {
        "workspace_root": str(context.get("workspace_root", "") or ""),
        "output_root": str(context.get("output_root", "") or ""),
        "comms_root": str(context.get("comms_root", "") or ""),
        "venv_path": str(context.get("venv_path", "") or ""),
        "python_executable": str(context.get("python_executable", "") or ""),
        "venv_provider": str(context.get("venv_provider", "") or ""),
        "preparation_error": str(context.get("preparation_error", "") or ""),
        "sandbox": sandbox,
    }


async def bash_exec(
    command: str,
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Execute a command using bash/sh semantics."""
    shell_binary = _shell_binary("bash", "sh" if os.name != "nt" else "bash")
    return await _run_shell_command(
        shell_name="bash",
        command=command,
        args=[shell_binary, "-lc", command],
        working_directory=working_directory,
        timeout=timeout,
        task=task,
        on_progress=on_progress,
    )


def _split_top_level_chain(command: str) -> list[tuple[str, str]] | None:
    """Split on `&&` / `||` that are outside quotes, keeping the operators.

    Returns [(segment, operator_before_it), ...] with "" as the first operator,
    or None when there is nothing to split — which keeps the untouched command
    on the fast path and out of the rewriter entirely.
    """
    segments: list[tuple[str, str]] = []
    buffer: list[str] = []
    pending = ""
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            buffer.append(char)
            if char == "`" and quote == '"' and index + 1 < len(command):
                # PowerShell escapes with a backtick inside double quotes.
                buffer.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
            index += 1
            continue
        if command.startswith("&&", index) or command.startswith("||", index):
            segments.append(("".join(buffer).strip(), pending))
            pending = command[index : index + 2]
            buffer = []
            index += 2
            continue
        buffer.append(char)
        index += 1
    if quote or not segments:
        # Unbalanced quotes: leave the command alone and let the shell report it.
        return None
    segments.append(("".join(buffer).strip(), pending))
    return segments if all(text for text, _op in segments) else None


def _rewrite_chain_for_windows_powershell(command: str) -> str:
    """Express `&&` / `||` chains in a dialect Windows PowerShell 5.1 accepts.

    5.1 has no pipeline chain operators; it fails the whole line with
    "&& is not a valid statement separator in this version". Agents write shell
    chains by default — every command in this runtime's approval log is one —
    so the failure lands on ordinary work rather than on anything exotic
    (observed live 2026-09-01: a designer's command died on `&&`, and the model
    spent a round trip rediscovering that before retrying with `;`).

    Telling the model not to use `&&` does not hold: it had been told, in the
    tool description, and wrote `&&` anyway. Translating at the boundary needs
    no cooperation. Short-circuiting is preserved through an explicit success
    flag rather than by chaining `if ($?)`, whose meaning would shift with
    whatever the previous statement left behind.
    """
    segments = _split_top_level_chain(command)
    if segments is None:
        return command
    flag = "$__ampuly_chain_ok"
    lines: list[str] = []
    for text, operator in segments:
        if not operator:
            # The first segment is unconditional. It must not be wrapped in
            # braces: a bare `{ ... }` is a ScriptBlock value in PowerShell, so
            # the whole command would be printed instead of run.
            lines.append(text)
        else:
            guard = "" if operator == "&&" else "-not "
            lines.append(f"if ({guard}{flag}) {{\n{text}\n}}")
        lines.append(f"{flag} = $?")
    return "\n".join(lines)


def _is_windows_powershell(executable: str) -> bool:
    """Windows PowerShell 5.1 ships as powershell.exe; 7+ ships as pwsh."""
    return Path(str(executable or "")).stem.lower() == "powershell"


# Windows PowerShell 5.1 defaults its file cmdlets to the system ANSI codepage,
# not UTF-8. On a Chinese install that means Get-Content reads a UTF-8
# deliverable as GBK and hands the agent mojibake, and Set-Content writes
# Chinese as GBK — bytes the rest of the runtime, which reads UTF-8, cannot
# decode at all. Both were measured: `# 品牌简报` came back as `# 鍝佺墝绠€鎶?`,
# and a written file began d6d0ce rather than a valid UTF-8 sequence.
#
# File cmdlets need *:Encoding; redirected stdout also needs
# OutputEncoding / Console.OutputEncoding or Chinese becomes ASCII
# question marks on the pipe. An explicit -Encoding in the agent's own
# command still wins. In 5.1 "utf8" means with a BOM, which is why the
# workspace readers use utf-8-sig.
_POWERSHELL_STDIO_UTF8_PREAMBLE = (
    "$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
)
_POWERSHELL_UTF8_PREAMBLE = (
    _POWERSHELL_STDIO_UTF8_PREAMBLE
    +
    "$PSDefaultParameterValues['*:Encoding']='utf8'"
)


async def powershell_exec(
    command: str,
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Execute a command using PowerShell semantics."""
    executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    effective = command
    if _is_windows_powershell(executable):
        effective = (
            f"{_POWERSHELL_UTF8_PREAMBLE}\n"
            f"{_rewrite_chain_for_windows_powershell(command)}"
        )
    else:
        # pwsh reads UTF-8 files, but a Windows console can still emit GBK.
        # Normalize this process's pipe without changing its file defaults.
        effective = f"{_POWERSHELL_STDIO_UTF8_PREAMBLE}\n{command}"
    return await _run_shell_command(
        shell_name="powershell",
        command=effective,
        args=[executable, "-NoProfile", "-Command", effective],
        working_directory=working_directory,
        timeout=timeout,
        task=task,
        on_progress=on_progress,
    )


async def shell_exec(
    command: str,
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    shell: str | None = None,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Compatibility wrapper that selects bash or PowerShell."""
    frozen_plan = current_opaque_execution_plan("shell_exec")
    if frozen_plan is not None:
        return await _run_shell_command(
            shell_name=str(
                frozen_plan.envelope.get("shell_kind", "") or "shell"
            ),
            command=command,
            args=list(frozen_plan.argv),
            working_directory=working_directory,
            timeout=timeout,
            task=None,
            on_progress=on_progress,
            frozen_plan=frozen_plan,
        )
    normalized = str(shell or "").strip().lower()
    if normalized == "powershell":
        return await powershell_exec(
            command=command,
            working_directory=working_directory,
            timeout=timeout,
            task=task,
            on_progress=on_progress,
        )
    if os.name == "nt" and normalized not in {"bash", "sh"}:
        return await powershell_exec(
            command=command,
            working_directory=working_directory,
            timeout=timeout,
            task=task,
            on_progress=on_progress,
        )
    return await bash_exec(
        command=command,
        working_directory=working_directory,
        timeout=timeout,
        task=task,
        on_progress=on_progress,
    )


def _shell_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": description},
            "working_directory": {"type": "string", "description": "Working directory for the command (optional)"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": _DEFAULT_SHELL_TIMEOUT},
        },
        "required": ["command"],
    }


def _shell_exec_description() -> str:
    """Name the shell the caller will actually get, and its real limits.

    Saying only "bash or PowerShell depending on platform" is how every command
    the agents recorded came out as `cd ... && ls -la && find . -type d`: a
    Unix line that Windows PowerShell 5.1 rejects at the first `&&`, before any
    sandbox is involved. The description states which interpreter is present so
    the command is written for it.
    """
    if os.name != "nt":
        return (
            "Execute a shell command through bash. The optional `shell` hint "
            "accepts bash | powershell."
        )
    if shutil.which("pwsh"):
        return (
            "Execute a shell command through PowerShell 7 (pwsh). `&&` and `||` "
            "work; Unix tools (ls, find, grep) are NOT available — use "
            "Get-ChildItem, Get-ChildItem -Recurse -Directory, Select-String. "
            "Read and write text files as UTF-8 explicitly (-Encoding UTF8). "
            "The optional `shell` hint accepts bash | powershell."
        )
    return (
        "Execute a shell command through Windows PowerShell 5.1. `&&` and `||` "
        "are accepted and keep their short-circuit meaning (the runtime "
        "translates them, since 5.1 has no chain operators of its own). Unix "
        "tools (ls, find, grep) are NOT available — use Get-ChildItem, "
        "Get-ChildItem -Recurse -Directory, Select-String. Pass -Encoding UTF8 "
        "when reading or writing text files, or non-ASCII content comes back "
        "mangled. Prefer the file tools (file_read, file_write, list_dir) over "
        "shell for anything they cover."
    )


def create_shell_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="shell_exec",
            description=_shell_exec_description(),
            parameters={
                "type": "object",
                "properties": {
                    **_shell_schema("The shell command to execute")["properties"],
                    "shell": {
                        "type": "string",
                        "description": "Optional shell hint: bash | powershell",
                        "default": "",
                    },
                },
                "required": ["command"],
            },
            func=shell_exec,
            category="compute",
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_OPAQUE_EXACT,
            permission_effects=["process_execute", "workspace_write"],
        ),
    ]


def create_shell_tool() -> ToolDefinition:
    """Backward-compatible helper used by older callers."""
    for tool in create_shell_tools():
        if tool.name == "shell_exec":
            return tool
    raise RuntimeError("shell_exec definition is missing")
