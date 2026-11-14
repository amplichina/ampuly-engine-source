"""Ampuly Engine CLI."""

from __future__ import annotations

import asyncio
from collections import deque
from importlib import resources as importlib_resources
import json
import os
import shlex
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Optional
import re

import typer
from rich.console import Console
from rich.markup import escape
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

from ampuly import __version__
# process_probe is force-included into wheels as top-level scripts/ (see pyproject).
# Module __getattr__ lazy-binds ProcessState / process_state for external readers
# so a missing scripts/ package does not kill console import (4.9 Q1 / AUD49-R17).
# channels_status / channels_stop resolve those names themselves: a function's
# global lookup does not call module __getattr__.
from ampuly.cli import ui_bridge
from ampuly.core.config import AmpulyConfig, get_ampuly_home, get_project_config_workspace
from ampuly.core.sqlite_open import opened
from ampuly.core.execution_agents import EXECUTION_AGENTS, execution_agent_label
from ampuly.core.interaction_protocol import owner_interaction_actor_identity
from ampuly.core.native_permissions import (
    NativeApprovalLevel,
    native_sandbox_profile,
    normalize_native_approval_level,
    parse_native_approval_level,
    tasks_in_native_permission_scope,
)
from ampuly.core.windows_ssl import (
    format_windows_sslkeylog_warning,
    pop_windows_sslkeylogfile,
)
from ampuly.database.store import AmpulyStore
from ampuly.layer2_organization.talent_market import TalentMarket
from ampuly.core.models import AmpulyEvent
from ampuly.project_id import is_valid_project_id

# Proxy is configured via llm_config.yaml or shell environment — not hardcoded.
# os.environ['https_proxy'] = 'http://127.0.0.1:7890'
# os.environ['http_proxy'] = 'http://127.0.0.1:7890'

custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "red bold",
    "success": "green bold",
    "agent": "blue",
    "tool": "magenta",
})
console = Console(theme=custom_theme)
app = typer.Typer(
    name="superones",
    help="SuperOnes Engine — autonomous AI agent system with a natural language interface (ampuly is kept as an alias).",
    no_args_is_help=True,
)

def __getattr__(name: str):
    """Lazy-bind process_probe symbols used by channel runtime status/stop."""
    if name in {"ProcessState", "process_state"}:
        from scripts.process_probe import ProcessState as _ProcessState
        from scripts.process_probe import process_state as _process_state

        globals()["ProcessState"] = _ProcessState
        globals()["process_state"] = _process_state
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class _CliRepoPath:
    """Native filesystem path with stable Windows-drive display semantics."""

    def __init__(self, value: str) -> None:
        self._native = Path(value)
        self._display = str(PureWindowsPath(value))

    def __fspath__(self) -> str:
        return os.fspath(self._native)

    def __str__(self) -> str:
        return self._display

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native, name)


def _talent_repo_path(value: str) -> Path | _CliRepoPath:
    if _WINDOWS_DRIVE_PATH_RE.match(str(value or "")):
        return _CliRepoPath(value)
    return Path(value)


def _current_command_label(argv: list[str] | None = None) -> str:
    args = list(argv or sys.argv)
    if len(args) > 1 and not args[1].startswith("-"):
        return f"ampuly {args[1]}"
    return "ampuly"


def _get_config() -> AmpulyConfig:
    config_dir = get_ampuly_home() / "config"
    if config_dir.exists():
        return _load_config_with_workspace_trust(config_dir)
    return AmpulyConfig()


def _parse_native_approval_level(value: str | None) -> NativeApprovalLevel:
    try:
        return parse_native_approval_level(value)
    except ValueError as exc:
        raise typer.BadParameter(
            "approval level must be read-only, auto, or full-access"
        ) from exc


def _render_native_permissions(
    config: AmpulyConfig,
    *,
    current: str | None = None,
) -> None:
    default = normalize_native_approval_level(
        config.autonomy.native_approval_level
    )
    effective = normalize_native_approval_level(current, default=default)
    sandbox = native_sandbox_profile(effective)
    table = Table(title="SuperOnes Native Permissions", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("System default", default)
    table.add_row("Current session", effective)
    table.add_row("Sandbox", str(sandbox["mode"]))
    table.add_row("Network", "allowed" if sandbox["allow_network"] else "ask first")
    console.print(table)
    console.print("[dim]Only SuperOnes Native is controlled; external agent harness permissions remain independent.[/dim]")


def _stdin_is_interactive() -> bool:
    """Whether a person can answer a prompt on stdin.

    ``sys.stdin.isatty()`` is not enough on Windows: the NUL device is a
    character device, so a subprocess started with ``stdin=DEVNULL`` (CI,
    schedulers, the console launching the CLI) reports a TTY, gets the trust
    prompt and aborts with exit 1 instead of failing closed with exit 2. A
    real console answers ``GetConsoleMode``; NUL and pipes do not.
    """
    import types

    probe = getattr(sys.stdin, "isatty", None)
    try:
        if probe is None or not probe():
            return False
    except (AttributeError, ValueError):
        return False
    # Only second-guess the *real* isatty: a test or embedding that replaced
    # it has already decided what the terminal is.
    if sys.platform != "win32" or not isinstance(probe, (types.BuiltinFunctionType, types.BuiltinMethodType)):
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:
        return True


_TRUST_ENTRY_POINTS = {"ampuly", "superones", "ampuly"}
_TRUST_REASON_TEXT = {
    "source_changed": "项目配置在上次信任之后被改过",
    "authority_changed": "项目配置里决定权限的内容（模型、外部程序、通道、审批级别）和上次信任时不一样了",
    "legacy_record": "这个项目的信任记录是旧格式，需要重新确认一次",
    "untrusted": "这个项目还没有被信任过",
}


def _quote_command_arg(value: str) -> str:
    return f'"{value}"' if " " in value else value


def _trust_add_command(workspace: Path) -> str:
    """The command the owner can paste, spelled with the entry point that is actually running."""

    entry = str(sys.argv[0] or "") if sys.argv else ""
    stem = Path(entry).stem.lower().removesuffix("-script")
    if stem not in _TRUST_ENTRY_POINTS:
        program = "ampuly"
    else:
        # pip's launcher hands the script its own path with ``.exe`` stripped; the
        # owner's terminal needs the file that exists, since .venv is not on PATH.
        located = next(
            (
                candidate
                for candidate in (Path(entry), Path(entry + ".exe"))
                if Path(entry).is_absolute() and candidate.is_file()
            ),
            None,
        )
        program = str(located) if located is not None else stem
    return f"{_quote_command_arg(program)} trust add {_quote_command_arg(str(workspace))}"


def _authority_files_changed_since_trust(config_dir: Path) -> tuple[list[tuple[str, str]], bool]:
    """Name the authority files that look edited after the last trust decision, with their times.

    The trust record keeps one fingerprint for all four files, so which one changed is read off
    the clock: a file written after the trust store was last saved is the suspect.  When none is
    newer (the store was touched for another workspace since), every file present is listed with
    its time, so the owner still knows where to look.  The flag says which of the two the list is:
    True for the suspects, False for the whole-list fallback (the caller words it as a guess).
    """

    from ampuly.core.workspace_trust import _AUTHORITY_SOURCE_FILES, WorkspaceTrustStore

    try:
        trusted_at: float | None = WorkspaceTrustStore().path.stat().st_mtime
    except OSError:
        trusted_at = None
    present: list[tuple[str, float]] = []
    for name in _AUTHORITY_SOURCE_FILES:
        try:
            present.append((name, (Path(config_dir) / name).stat().st_mtime))
        except OSError:
            continue
    newer = [row for row in present if trusted_at is not None and row[1] > trusted_at]
    return (
        [
            (name, datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"))
            for name, mtime in (newer or present)
        ],
        bool(newer),
    )


def _migrated_config_remedy(exc: Any) -> str:
    """Name the old and new config directories and the command that re-trusts them.

    Shown only when a migration receipt exists. It does not grant trust.
    """

    try:
        from ampuly.core.home_migration import migration_config_dirs
    except Exception:
        return ""
    found = migration_config_dirs(Path(exc.workspace))
    if found is None:
        return ""
    old_config, new_config = found
    try:
        same_dir = Path(new_config).resolve(strict=False) == Path(exc.config_dir).resolve(strict=False)
    except OSError:
        same_dir = str(Path(new_config)) == str(Path(exc.config_dir))
    if not same_dir:
        return ""
    command = _trust_add_command(Path(exc.workspace))
    return (
        f"迁移已把配置从 {old_config} 复制到 {new_config}，信任记录还对在旧目录上，所以新目录过不了信任门槛。\n"
        "门槛没有关闭，迁移也不会自动信任被改过或新出现的文件。\n"
        f"核对这两个目录。确认新目录里的文件就是你信任过的那些，再运行：{command}\n"
        "文件被改过，或不是你做的迁移，不要直接信任。"
    )


def _explain_workspace_trust_failure(exc: Any) -> tuple[str, str]:
    """Plain-language account of a refused workspace, for the owner rather than an engineer.

    Returns the situation and the way out separately: the second holds paths and a
    command to paste, so it is printed without hard wrapping.
    """

    command = _trust_add_command(exc.workspace)
    reason = exc.reason if exc.reason in _TRUST_REASON_TEXT else "untrusted"
    situation = [f"{_TRUST_REASON_TEXT[reason]}。"]
    if reason == "source_changed":
        files, pinned_down = _authority_files_changed_since_trust(exc.config_dir)
        if files:
            lead = "改动的文件：" if pinned_down else "无法确定是哪一个，可能是："
            situation.append(lead + "；".join(f"{name}（{when}）" for name, when in files))
    if reason == "untrusted":
        situation.append("项目配置可以启动本机程序、连接外部服务、使用密钥，所以第一次要你确认。")
    action = [f"项目：{exc.workspace}", f"配置目录：{exc.config_dir}"]
    migrated = _migrated_config_remedy(exc)
    if migrated:
        action.append(migrated)
    if reason in {"source_changed", "authority_changed"}:
        action.append(f"如果是你改的，运行：{command}")
        action.append("如果不是你改的，先检查这些文件再决定，不要直接信任。")
    elif reason == "legacy_record":
        action.append(f"确认配置没问题后，运行：{command}")
    else:
        action.append(f"确认这是你的项目后，运行：{command}")
    return "\n".join(situation), "\n".join(action)


def _load_config_with_workspace_trust(config_dir: Path) -> AmpulyConfig:
    """Load config, obtaining an explicit CLI trust decision when possible."""

    from ampuly.core.workspace_trust import WorkspaceTrustRequired, WorkspaceTrustStore

    try:
        return AmpulyConfig.load(config_dir)
    except WorkspaceTrustRequired as exc:
        situation, action = _explain_workspace_trust_failure(exc)
        if not _stdin_is_interactive():
            console.print(f"[error]{escape(situation)}[/error]")
            console.print(escape(action), soft_wrap=True)
            console.print("[info]这里没有人能回答确认，所以先停下；在终端里运行一次上面的命令即可。[/info]")
            raise typer.Exit(code=2) from exc

        console.print(Panel(
            escape(situation),
            title="需要确认信任这个项目",
            border_style="yellow",
        ))
        console.print(escape(action), soft_wrap=True)
        if not typer.confirm("信任这个项目并加载它的配置？", default=False):
            console.print("[warning]没有信任这个项目，配置没有加载。[/warning]")
            raise typer.Exit(code=2) from exc
        # Parsing is allowed only after the explicit decision.  Trust is bound
        # to both the post-migration source bytes and normalized authority.
        config = AmpulyConfig.load(config_dir, trusted_source=True)
        WorkspaceTrustStore().trust(exc.workspace, config_dir, config)
        config.bind_workspace_trust(exc.workspace, config_dir)
        config.require_workspace_trust(include_effective=True)
        console.print(f"[success]已信任项目：[/success]{escape(str(exc.workspace))}")
        return config


def _channel_runtime_pid_path() -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home() / "run" / "channels.pid"


def _read_channel_runtime_state() -> dict | None:
    path = _channel_runtime_pid_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_channel_runtime_state(enabled_channels: list[str]) -> None:
    path = _channel_runtime_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "channels": enabled_channels}, ensure_ascii=False))


def _clear_channel_runtime_state() -> None:
    path = _channel_runtime_pid_path()
    if path.exists():
        path.unlink()


def _create_default_skills(ampuly_home: Path) -> None:
    """Copy bundled skills to the SuperOnes home skills directory."""
    import shutil

    repo_skills = Path(__file__).parent.parent.parent / "skills" / "core"
    target_skills = ampuly_home / "skills" / "core"
    target_skills.mkdir(parents=True, exist_ok=True)

    copied = False
    if repo_skills.exists():
        for skill_file in repo_skills.glob("*.md"):
            dest = target_skills / skill_file.name
            if not dest.exists():
                shutil.copy2(skill_file, dest)
            copied = True
    if copied:
        return

    try:
        resource_skills = importlib_resources.files("ampuly").joinpath("skills_assets", "core")
    except Exception:
        return
    if not resource_skills.is_dir():
        return
    for child in resource_skills.iterdir():
        if child.is_file() and child.name.endswith(".md"):
            dest = target_skills / child.name
            if not dest.exists():
                dest.write_bytes(child.read_bytes())


def _progress_callback():
    """Create an async progress callback for streaming agent output."""
    async def callback(text: str, **_: Any) -> None:
        if text.startswith("[Tool:"):
            console.print(f"  [tool]{escape(text)}[/tool]")
        elif text.startswith("[Suggestion]"):
            console.print(Panel(text, title="Suggestion", border_style="yellow"))
        elif text.startswith("[Delegating") or text.startswith("[Autonomy]"):
            console.print(f"  [info]{escape(text)}[/info]")
        elif text.startswith("[External approval]"):
            console.print(f"  [info]{escape(text)}[/info]")
        elif text.startswith("[External status]"):
            if _should_show_external_status(text):
                console.print(f"  [info]{escape(text)}[/info]")
        elif text.startswith("[External agent denied]"):
            console.print(f"  [warning]{escape(text)}[/warning]")
        elif text.startswith("[External agent failed]") or text.startswith("[External agents exhausted]"):
            console.print(f"  [warning]{escape(text)}[/warning]")
        elif text.startswith("[External:"):
            if _cli_verbose_external_progress():
                console.print(f"  [warning]{escape(text)}[/warning]")
        else:
            pass
    return callback


def _cli_verbose_external_progress() -> bool:
    value = os.environ.get("AMPULY_CLI_VERBOSE_EXTERNAL", "")
    return value.strip().lower() in {"1", "true", "yes", "on", "debug"}


def _should_show_external_status(text: str) -> bool:
    if _cli_verbose_external_progress():
        return True
    detail = text.split("]", 1)[1].strip().lower() if "]" in text else text.lower()
    if "working; last activity" in detail:
        return False
    return any(token in detail for token in ("started", "approval", "timeout", "failed", "denied", "exhausted", "awaiting", "requires"))


@dataclass
class _QueuedAssistantLine:
    text: str
    enqueued_at: float


class _CliRuntimeDisplay:
    def __init__(self, rich_console: Console) -> None:
        self.console = rich_console
        self._assistant_buffer = ""
        self._line_queue: deque[_QueuedAssistantLine] = deque()
        self._drain_task: asyncio.Task[None] | None = None
        self._mode = "smooth"
        self._below_exit_since: float | None = None
        self._last_exit_at: float | None = None
        self._status_snapshot: dict[str, object] = {}
        self._checkpoint_hint = ""
        self._last_status_render = ""
        self._last_status_render_at = 0.0
        self._sidecar_quiet_depth = 0
        self._sidecar_quiet_event_count = 0
        self.has_streamed_content = False

    def begin_turn(self) -> None:
        self.has_streamed_content = False
        self._assistant_buffer = ""
        self._line_queue.clear()
        self._mode = "smooth"
        self._below_exit_since = None
        self._last_exit_at = None
        self._checkpoint_hint = ""

    def status_snapshot(self) -> dict[str, object]:
        snapshot = dict(self._status_snapshot)
        snapshot["stream_queue_depth"] = len(self._line_queue)
        snapshot["stream_drain_mode"] = self._mode
        if self._checkpoint_hint:
            snapshot["checkpoint_hint"] = self._checkpoint_hint
        return snapshot

    def set_checkpoint_hint(self, hint: str) -> None:
        self._checkpoint_hint = hint.strip()

    async def render_status(self, *, force: bool = False) -> None:
        await self._render_status(force=force)

    async def handle_event(self, event: AmpulyEvent) -> None:
        if getattr(event, "event_type", "") != "runtime_event":
            return
        payload = dict(getattr(event, "payload", {}) or {})
        runtime_type = str(payload.get("type", "") or "").strip()
        if self._sidecar_quiet_depth > 0:
            self._sidecar_quiet_event_count += 1
            if runtime_type == "status_snapshot":
                self._status_snapshot = payload
            elif runtime_type in {"turn_completed", "turn_failed"}:
                self._assistant_buffer = ""
                self._line_queue.clear()
            return
        if runtime_type == "assistant_delta":
            self._enqueue_assistant_delta(str(payload.get("text", "") or ""))
            self._ensure_drain_task()
            return
        if runtime_type in {"turn_completed", "turn_failed"}:
            self._finalize_assistant_buffer()
            await self.flush()
            await self._render_status(force=True)
            return
        if runtime_type == "status_snapshot":
            self._status_snapshot = payload
            await self._render_status()
            return
        if runtime_type == "tool_started":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            self.console.print(f"  [tool]Running {escape(tool_name)}[/tool]")
            return
        if runtime_type == "tool_completed":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            elapsed_ms = int(payload.get("elapsed_ms", 0) or 0)
            summary = str(payload.get("result_summary", "") or "").strip()
            suffix = f" ({elapsed_ms}ms)" if elapsed_ms > 0 else ""
            if summary:
                self.console.print(f"  [tool]{escape(tool_name)} finished{suffix}: {escape(summary)}[/tool]")
            else:
                self.console.print(f"  [tool]{escape(tool_name)} finished{suffix}[/tool]")
            return
        if runtime_type == "permission_requested":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            rationale = str(payload.get("rationale", "") or "").strip()
            self.console.print(
                f"  [warning]Approval needed for {escape(tool_name)}: {escape(rationale or 'awaiting review')}[/warning]"
            )
            return
        if runtime_type == "sandbox_retry_requested":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            self.console.print(
                f"  [info]Retrying {escape(tool_name)} with sandbox upgrade "
                f"{escape(str(payload.get('from_mode', 'unknown')))} -> "
                f"{escape(str(payload.get('to_mode', 'unknown')))}[/info]"
            )
            return
        if runtime_type == "sandbox_retry_completed":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            state = "succeeded" if bool(payload.get("success", False)) else "failed"
            self.console.print(f"  [info]Sandbox retry {state} for {escape(tool_name)}[/info]")
            return
        if runtime_type == "context_warning":
            pct = int(payload.get("context_remaining_pct", 0) or 0)
            self.console.print(f"  [warning]Context window low: {pct}% remaining[/warning]")

    def _enqueue_assistant_delta(self, text: str) -> None:
        if not text:
            return
        self._assistant_buffer += text
        while "\n" in self._assistant_buffer:
            line, self._assistant_buffer = self._assistant_buffer.split("\n", 1)
            self._line_queue.append(_QueuedAssistantLine(line, time.monotonic()))

    def _finalize_assistant_buffer(self) -> None:
        if self._assistant_buffer:
            self._line_queue.append(_QueuedAssistantLine(self._assistant_buffer, time.monotonic()))
            self._assistant_buffer = ""

    def _ensure_drain_task(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop())

    def _drain_count(self, now: float) -> int:
        queued = len(self._line_queue)
        if queued == 0:
            self._mode = "smooth"
            self._below_exit_since = None
            return 0
        oldest_age_ms = int(max(0.0, now - self._line_queue[0].enqueued_at) * 1000)
        severe = queued >= 64 or oldest_age_ms >= 300
        if self._mode == "smooth":
            if (queued >= 8 or oldest_age_ms >= 120) and (not self._last_exit_at or severe or (now - self._last_exit_at) >= 0.25):
                self._mode = "catch_up"
                self._below_exit_since = None
        else:
            below_exit = queued <= 2 and oldest_age_ms <= 40
            if below_exit:
                if self._below_exit_since is None:
                    self._below_exit_since = now
                elif (now - self._below_exit_since) >= 0.25:
                    self._mode = "smooth"
                    self._below_exit_since = None
                    self._last_exit_at = now
            else:
                self._below_exit_since = None
        return queued if self._mode == "catch_up" else 1

    async def _drain_loop(self) -> None:
        try:
            while self._line_queue:
                now = time.monotonic()
                count = self._drain_count(now)
                for _ in range(min(count, len(self._line_queue))):
                    line = self._line_queue.popleft()
                    self.console.print(line.text, markup=False)
                    self.has_streamed_content = True
                await self._render_status()
                await asyncio.sleep(0.05)
        finally:
            self._drain_task = None

    async def _render_status(self, *, force: bool = False) -> None:
        now = time.monotonic()
        snapshot = dict(self._status_snapshot)
        stream_queue_depth = len(self._line_queue)
        runtime_queue_depth = snapshot.get("queue_depth", 0)
        current_tool = str(snapshot.get("current_tool", "") or "").strip()
        agent_id = str(snapshot.get("agent_id", "") or snapshot.get("role_id", "") or "").strip()
        task_id = str(snapshot.get("task_id", "") or "").strip()
        context_remaining_pct = snapshot.get("context_remaining_pct")
        turn_cost = snapshot.get("turn_cost_usd")
        session_cost = snapshot.get("session_cost_usd")
        pending_permission_count = snapshot.get("pending_permission_count")
        parts: list[str] = []
        parts.append(f"queue={int(runtime_queue_depth or 0)}")
        parts.append(f"stream_queue={stream_queue_depth}")
        parts.append(f"drain={self._mode}")
        if agent_id:
            parts.append(f"agent={agent_id}")
        if task_id:
            parts.append(f"task={task_id}")
        if current_tool:
            elapsed_ms = int(snapshot.get("tool_elapsed_ms", 0) or 0)
            tool_part = f"tool={current_tool}"
            if elapsed_ms > 0:
                tool_part += f" {elapsed_ms}ms"
            parts.append(tool_part)
        if context_remaining_pct not in (None, ""):
            parts.append(f"context={int(context_remaining_pct)}%")
        if turn_cost not in (None, ""):
            parts.append(f"turn=${float(turn_cost):.4f}")
        if session_cost not in (None, ""):
            parts.append(f"session=${float(session_cost):.4f}")
        if pending_permission_count not in (None, "") and int(pending_permission_count or 0) > 0:
            parts.append(f"approvals={int(pending_permission_count or 0)}")
        if self._checkpoint_hint:
            parts.append(f"checkpoint={self._checkpoint_hint}")
        message = " | ".join(parts)
        if not message:
            return
        if not force and (message == self._last_status_render or (now - self._last_status_render_at) < 0.75):
            return
        self.console.print(f"[dim][status][/dim] {message}")
        self._last_status_render = message
        self._last_status_render_at = now

    async def flush(self) -> None:
        self._finalize_assistant_buffer()
        if self._drain_task is not None:
            await self._drain_task
        while self._line_queue:
            line = self._line_queue.popleft()
            self.console.print(line.text)
            self.has_streamed_content = True

    def enter_sidecar_quiet(self) -> None:
        self._sidecar_quiet_depth += 1

    def exit_sidecar_quiet(self) -> int:
        if self._sidecar_quiet_depth > 0:
            self._sidecar_quiet_depth -= 1
        count = self._sidecar_quiet_event_count
        if self._sidecar_quiet_depth == 0:
            self._sidecar_quiet_event_count = 0
        return count


def _create_cli_engine(config, project: str | None):
    from ampuly.core.config import get_ampuly_home
    from ampuly.engine import AmpulyEngine
    from ampuly.plugins.office_ui.order_liveness import BusinessOrderLiveness

    runtime_display = _CliRuntimeDisplay(console)
    engine = AmpulyEngine(
        config=config,
        # A CLI session has no request project; the console's long-standing
        # default is the explicit project named "default", never None (which
        # the employee runtime refuses as an unverified scope).
        project_id=(str(project).strip() if project else "default"),
        on_progress=_progress_callback(),
        on_runtime_event=runtime_display.handle_event,
        on_escalation=_escalation_callback(),
        # runtime contract: the console's orders are the kernel's order port here too, so an order
        # that has ended gets no owner card from a CLI engine either.
        order_liveness=BusinessOrderLiveness(get_ampuly_home()),
    )
    _bind_model_catalogue_after_initialize(engine)
    return engine, runtime_display


def _bind_model_catalogue_after_initialize(engine) -> None:
    """Model-routing phase 0, open point 14 (owner ruling): a CLI engine finds a routed model's key through the model
    catalogue, as the console does, so cross-vendor routes are checked and sent with their own vendor's key instead of
    being refused. Bound here, in the plugin layer, once the engine has its provider; the kernel never imports the
    catalogue."""
    initialize = engine.initialize

    async def initialize_and_bind(*args, **kwargs):
        result = await initialize(*args, **kwargs)
        from ampuly.plugins.office_ui.step_models import bind_step_model_resolver

        bind_step_model_resolver(getattr(engine, "llm", None))
        return result

    engine.initialize = initialize_and_bind


def _attach_cli_runtime_callbacks(state: _InteractiveChatState) -> None:
    def on_company_runtime_children(session_id: str, task_ids: list[str]) -> None:
        parent_session_id = str(session_id or "").strip()
        clean_task_ids = [str(item or "").strip() for item in list(task_ids or []) if str(item or "").strip()]
        if parent_session_id and clean_task_ids:
            state.session_to_task[parent_session_id] = clean_task_ids[0]
            for task_id in clean_task_ids:
                state.active_runtime_children[task_id] = clean_task_ids[0]

    try:
        state.engine.on_company_runtime_children = on_company_runtime_children
    except Exception:
        pass


def _normalize_escalation_key(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip()).strip("_").casefold()


def _format_escalation_option(option: dict, index: int) -> str:
    option_id = str(option.get("id", "")).strip()
    label = str(option.get("label", option_id)).strip() or option_id
    # Use an explicit numeric prefix and escape markup so Rich never hides the id.
    return f"  {index}. {escape(label)} ({escape(option_id)})"


def _normalize_escalation_reply(reply: str, options: list[dict]) -> str | None:
    raw_reply = reply.strip()
    if not raw_reply:
        return None

    normalized_map: dict[str, str] = {}
    for idx, option in enumerate(options, start=1):
        option_id = str(option.get("id", "")).strip()
        label = str(option.get("label", option_id)).strip()
        if not option_id:
            continue
        normalized_map[option_id.casefold()] = option_id
        normalized_map[_normalize_escalation_key(option_id)] = option_id
        if label:
            normalized_map[label.casefold()] = option_id
            normalized_map[_normalize_escalation_key(label)] = option_id
        normalized_map[str(idx)] = option_id

    alias_map = {
        "y": "approve_once",
        "yes": "approve_once",
        "approve": "approve_once",
        "allow": "approve_once",
        "n": "deny",
        "no": "deny",
        "deny": "deny",
        "reject": "deny",
        "project": "always_project",
        "global": "always_global",
    }
    if alias := alias_map.get(_normalize_escalation_key(raw_reply)):
        if alias in normalized_map.values():
            return alias

    return normalized_map.get(raw_reply.casefold()) or normalized_map.get(_normalize_escalation_key(raw_reply))


def _escalation_callback():
    """Create an async escalation callback for human-in-the-loop."""
    async def callback(message: str, options: list[dict]) -> str | None:
        console.print(Panel(message, title="Action Required", border_style="yellow"))
        if options:
            for idx, opt in enumerate(options, start=1):
                console.print(_format_escalation_option(opt, idx))
            console.print("  Enter the number, label, or internal id.")
            while True:
                try:
                    reply = console.input("[bold]Your choice: [/bold]")
                except (EOFError, KeyboardInterrupt):
                    return None
                normalized = _normalize_escalation_reply(reply, options)
                if normalized is not None:
                    return normalized
                console.print("[warning]Invalid choice. Please enter a listed number, label, or id.[/warning]")
        else:
            try:
                reply = console.input("[bold]Your reply: [/bold]")
                return reply.strip()
            except (EOFError, KeyboardInterrupt):
                return None
    return callback


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

permissions_app = typer.Typer(
    help="Inspect or set SuperOnes Native permissions.",
    invoke_without_command=True,
)
app.add_typer(permissions_app, name="permissions")


@permissions_app.callback()
def permissions_status(ctx: typer.Context) -> None:
    """Show the system default SuperOnes Native permission level."""
    if ctx.invoked_subcommand is None:
        _render_native_permissions(_get_config())


@permissions_app.command("set")
def permissions_set(
    level: str = typer.Argument(..., help="read-only, auto, or full-access"),
) -> None:
    """Set the default copied into newly created sessions."""
    from ampuly.core.workspace_trust import save_explicit_workspace_authority_change

    config = _get_config()
    normalized = _parse_native_approval_level(level)
    previous = config.autonomy.native_approval_level
    config.autonomy.native_approval_level = normalized
    try:
        save_explicit_workspace_authority_change(
            config,
            get_ampuly_home() / "config",
        )
    except Exception as exc:
        config.autonomy.native_approval_level = previous
        console.print(
            f"[error]Unable to save Native permissions: {escape(str(exc))}[/error]"
        )
        raise typer.Exit(code=2) from exc
    console.print(f"[success]SuperOnes Native default set to {normalized}.[/success]")
    console.print("[dim]Existing sessions keep their own level.[/dim]")

@app.command()
def chat(
    message: Optional[str] = typer.Argument(None, help="Single message to process"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID to work in"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default LLM model"),
    mode: str = typer.Option("task", "--mode", help="Execution mode: task or company"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred agent ID; jiuwen = JiuwenSwarm-single, jiuwenswarm = JiuwenSwarm-team"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="Company profile for company mode"),
    approval_level: Optional[str] = typer.Option(None, "--approval-level", help="SuperOnes Native permissions: read-only, auto, or full-access"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed logs"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Plain text output"),
):
    """Chat with the SuperOnes system — interactive or single message mode."""
    config = _get_config()
    if model:
        config.llm.default_model = model
    if verbose:
        config.system.log_level = "DEBUG"
    native_approval_level = (
        _parse_native_approval_level(approval_level)
        if approval_level is not None
        else normalize_native_approval_level(config.autonomy.native_approval_level)
    )

    if message:
        asyncio.run(_single_message(
            config,
            message,
            project,
            no_markdown,
            mode=mode,
            preferred_agent=agent,
            company_profile=company_profile,
            native_approval_level=native_approval_level,
        ))
    else:
        asyncio.run(_interactive_mode(
            config,
            project,
            no_markdown,
            mode=mode,
            preferred_agent=agent,
            company_profile=company_profile,
            explicit_mode=_cli_option_present("--mode"),
            explicit_agent=_cli_option_present("--agent"),
            explicit_company_profile=_cli_option_present("--company-profile"),
            native_approval_level=native_approval_level,
            explicit_native_approval_level=_cli_option_present("--approval-level"),
        ))


distribution_app = typer.Typer(help="The distribution layer as a contract: check it, scaffold a customer deployment")
app.add_typer(distribution_app, name="distribution")


# --- inserted by shop-ego owner leaf; place after distribution_app block ---
browser_app = typer.Typer(
    help="Owner-triggerable browser capabilities (Ego sandbox-shop; no external agent)"
)
app.add_typer(browser_app, name="browser")


# --- CRM Owner real-data gate ops (engine-owned; parallel to browser shop-home) ---
crm_app = typer.Typer(
    help="Owner-triggerable CRM gate ops (backup-key custody + restore drill; no key printing)"
)
app.add_typer(crm_app, name="crm")


# --- Shop workspace + 店规 (rules-first; engine-owned) ---
shop_app = typer.Typer(help="Per-shop workspace + 店规 (人定规, Owner CLI)")
app.add_typer(shop_app, name="shop")
shop_workspace_app = typer.Typer(help="Init / inspect isolated shop workspaces")
shop_app.add_typer(shop_workspace_app, name="workspace")
shop_rules_app = typer.Typer(help="Show / set / learn / accept shop rules")
shop_app.add_typer(shop_rules_app, name="rules")


@shop_app.command("describe")
def shop_describe(
    json_output: bool = typer.Option(False, "--json", help="Emit capability card JSON"),
) -> None:
    """Show shop workspace + rules capability card."""
    from ampuly.layer4_tools.shop_workspace import describe_shop_workspace_capability

    card = describe_shop_workspace_capability()
    if json_output:
        console.print_json(data=card)
        return
    console.print(f"[info]{escape(card['id'])}[/info]  {escape(card['title'])}")
    for cli_name in card["cli"]:
        console.print(f"[dim]cli[/dim]  {escape(cli_name)}")


@shop_workspace_app.command("init")
def shop_workspace_init(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    name: str = typer.Option(
        "Example shop",
        "--name",
        help="Shop display name",
    ),
    profile: str = typer.Option("sandbox-shop", "--profile", help="Ego profile id"),
    seed_sandbox: bool = typer.Option(
        True,
        "--seed-sandbox/--no-seed-sandbox",
        help="Write stub shop-rules.json (not accepted)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Create AMPULY_HOME/workspaces/{business}/{shop}/ layout (+ optional stub rules)."""
    from ampuly.layer4_tools.shop_workspace import init_workspace, ampuly_home

    result = init_workspace(
        ampuly_home(),
        business_id=business,
        shop_id=shop,
        shop_display_name=name,
        ego_profile_id=profile,
        seed_stub=seed_sandbox,
    )
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        console.print(
            f"[info]shop workspace init OK[/info]  dir={escape(result.workspace_dir)} "
            f"stub={result.seeded_stub}"
        )
    if not result.ok:
        raise typer.Exit(code=1)


@shop_rules_app.command("show")
def shop_rules_show(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    json_output: bool = typer.Option(False, "--json", help="Emit rules JSON"),
) -> None:
    """Show active shop-rules.json and whether listing is unlocked."""
    from ampuly.layer4_tools.shop_workspace import ampuly_home, show_rules

    card = show_rules(ampuly_home(), business, shop)
    if json_output:
        console.print_json(data=card)
        return
    if not card.get("ok"):
        console.print(f"[error]{escape(str(card.get('error')))}[/error]")
        raise typer.Exit(code=1)
    console.print(
        f"[info]shop rules[/info]  status={escape(str(card.get('status')))} "
        f"listing_unlocked={card.get('listing_unlocked')}"
    )
    console.print(f"[dim]path[/dim]  {escape(str(card.get('rules_path')))}")


@shop_rules_app.command("set")
def shop_rules_set(
    file: Path = typer.Option(..., "--file", help="JSON rules file", path_type=Path),
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    accept: bool = typer.Option(
        False,
        "--accept",
        help="Write as status=accepted (requires categories+brands)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Write shop-rules.json from a file (Owner-authored)."""
    import json as _json

    from ampuly.layer4_tools.shop_workspace import ampuly_home, set_rules

    payload = _json.loads(file.read_text(encoding="utf-8-sig"))
    result = set_rules(ampuly_home(), business, shop, payload, as_accepted=accept)
    if json_output:
        console.print_json(data=result)
    else:
        ok = bool(result.get("ok"))
        style = "info" if ok else "error"
        console.print(f"[{style}]shop rules set {'OK' if ok else 'FAIL'}[/{style}]")
        if result.get("error"):
            console.print(f"[error]{escape(str(result['error']))}[/error]")
    if not result.get("ok"):
        raise typer.Exit(code=1)


@shop_rules_app.command("learn-from-listing")
def shop_rules_learn_from_listing(
    listing_id: str = typer.Option(..., "--listing-id", help="Reference draft/on-shelf id"),
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    from_json: Path | None = typer.Option(
        None,
        "--from-json",
        help="Optional structure JSON (categories/brands/title)",
        path_type=Path,
    ),
    ego: bool = typer.Option(
        False,
        "--ego",
        help="Best-effort extract via Ego sandbox-shop (still writes candidate only)",
    ),
    profile: str = typer.Option("sandbox-shop", "--profile", help="Ego profile id"),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Learn candidate rules from a listing. Never auto-applies — use accept-candidate."""
    import json as _json

    from ampuly.layer4_tools.shop_workspace import learn_from_listing, ampuly_home

    structure = None
    if from_json is not None:
        structure = _json.loads(from_json.read_text(encoding="utf-8-sig"))
    result = learn_from_listing(
        ampuly_home(),
        business_id=business,
        shop_id=shop,
        listing_id=listing_id,
        structure=structure,
        use_ego=ego,
        profile_id=profile,
    )
    if json_output:
        console.print_json(data=result)
    else:
        ok = bool(result.get("ok"))
        style = "info" if ok else "error"
        console.print(
            f"[{style}]learn-from-listing {'OK' if ok else 'FAIL'}[/{style}]  "
            f"auto_applied={result.get('auto_applied', False)}"
        )
        if result.get("candidate_path"):
            console.print(f"[dim]candidate[/dim]  {escape(str(result['candidate_path']))}")
        if result.get("error"):
            console.print(f"[error]{escape(str(result['error']))}[/error]")
        console.print("[dim]next[/dim]  ampuly shop rules accept-candidate")
    if not result.get("ok"):
        raise typer.Exit(code=1)


@shop_rules_app.command("accept-candidate")
def shop_rules_accept_candidate(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    candidate: Path | None = typer.Option(
        None,
        "--candidate",
        help="Candidate JSON path (default: latest under rules/candidates)",
        path_type=Path,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Promote a candidate rules file to accepted shop-rules.json after Owner review."""
    from ampuly.layer4_tools.shop_workspace import accept_candidate, ampuly_home

    result = accept_candidate(
        ampuly_home(),
        business_id=business,
        shop_id=shop,
        candidate_path=candidate,
    )
    if json_output:
        console.print_json(data=result)
    else:
        ok = bool(result.get("ok"))
        style = "info" if ok else "error"
        console.print(f"[{style}]accept-candidate {'OK' if ok else 'FAIL'}[/{style}]")
        if result.get("rules_path"):
            console.print(f"[dim]rules[/dim]  {escape(str(result['rules_path']))}")
        if result.get("error"):
            console.print(f"[error]{escape(str(result['error']))}[/error]")
            if result.get("hint"):
                console.print(f"[dim]hint[/dim]  {escape(str(result['hint']))}")
    if not result.get("ok"):
        raise typer.Exit(code=1)




shop_catalog_app = typer.Typer(help="Shop catalog index + inbox drop ingest")
shop_app.add_typer(shop_catalog_app, name="catalog")
shop_master_app = typer.Typer(help="business_id <-> project_id master link")
shop_app.add_typer(shop_master_app, name="master")


@shop_catalog_app.command("list")
def shop_catalog_list(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    json_output: bool = typer.Option(False, "--json", help="Emit catalog JSON"),
) -> None:
    """List catalog/index.json items for a shop."""
    from ampuly.layer4_tools.shop_catalog import list_catalog
    from ampuly.layer4_tools.shop_workspace import ampuly_home

    card = list_catalog(ampuly_home(), business_id=business, shop_id=shop)
    if json_output:
        console.print_json(data=card)
        return
    console.print(
        f"[info]shop catalog[/info]  count={card.get('count')} "
        f"inbox={escape(str(card.get('inbox_path')))}"
    )
    for item in card.get("items") or []:
        console.print(
            f"  - {escape(str(item.get('listing_id')))}  "
            f"{escape(str(item.get('title') or ''))}"
        )


@shop_catalog_app.command("upsert")
def shop_catalog_upsert(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    listing_id: str | None = typer.Option(None, "--listing-id", help="Listing id"),
    title: str | None = typer.Option(None, "--title", help="Title"),
    brand: str | None = typer.Option(None, "--brand", help="Brand"),
    category: str | None = typer.Option(None, "--category", help="Category keyword"),
    from_json: Path | None = typer.Option(
        None, "--from-json", help="Listing JSON object", path_type=Path
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Upsert one listing into catalog/index.json (+ items/{id}.json)."""
    import json as _json

    from ampuly.layer4_tools.shop_catalog import upsert_listing
    from ampuly.layer4_tools.shop_workspace import ampuly_home

    payload: dict = {}
    if from_json is not None:
        payload = _json.loads(from_json.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            console.print("[error]--from-json must be a JSON object[/error]")
            raise typer.Exit(code=1)
    if listing_id:
        payload["listing_id"] = listing_id
    if title:
        payload["title"] = title
    if brand:
        payload["brand"] = brand
    if category:
        payload["category"] = category
    payload.setdefault("source", "owner_cli_upsert")
    result = upsert_listing(
        ampuly_home(), business_id=business, shop_id=shop, listing=payload
    )
    if json_output:
        console.print_json(data=result)
    else:
        ok = bool(result.get("ok"))
        style = "info" if ok else "error"
        console.print(
            f"[{style}]catalog upsert {'OK' if ok else 'FAIL'}[/{style}]  "
            f"listing_id={escape(str(result.get('listing_id')))}"
        )
        if result.get("error"):
            console.print(f"[error]{escape(str(result['error']))}[/error]")
    if not result.get("ok"):
        raise typer.Exit(code=1)


@shop_catalog_app.command("ingest-drop")
def shop_catalog_ingest_drop(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Specific drop file (default: oldest JSON in catalog/inbox)",
        path_type=Path,
    ),
    then_draft: bool = typer.Option(
        False,
        "--then-draft",
        help="After successful ingest, attempt listing-draft (see --draft-mode)",
    ),
    draft_mode: str = typer.Option(
        "dry-run",
        "--draft-mode",
        help="off | dry-run (default, no Ego) | live (Ego shop-listing-draft)",
    ),
    profile: str = typer.Option(
        "sandbox-shop",
        "--profile",
        help="Ego profile id when --draft-mode live",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Ingest one dropped file from catalog/inbox into the catalog index.

    With --then-draft: after ingest, resolve accepted shop-rules gate and either
    write a dry-run draft plan receipt or kick live shop-listing-draft.
    Never auto-accepts rules, invents categories, or mass_publish.
    """
    from ampuly.layer4_tools.shop_catalog import ingest_drop
    from ampuly.layer4_tools.shop_workspace import ampuly_home

    mode = draft_mode.replace("-", "_") if draft_mode else "dry_run"
    result = ingest_drop(
        ampuly_home(),
        business_id=business,
        shop_id=shop,
        file=file,
        then_draft=then_draft,
        draft_mode=mode,
        profile_id=profile,
    )
    if json_output:
        console.print_json(data=result)
    else:
        ok = bool(result.get("ok"))
        style = "info" if ok else "error"
        console.print(
            f"[{style}]catalog ingest-drop {'OK' if ok else 'FAIL'}[/{style}]"
        )
        if result.get("listing_id"):
            console.print(f"[dim]listing[/dim]  {escape(str(result['listing_id']))}")
        if result.get("inbox_path"):
            console.print(f"[dim]inbox[/dim]  {escape(str(result['inbox_path']))}")
        if result.get("error"):
            console.print(f"[error]{escape(str(result['error']))}[/error]")
        if result.get("hint"):
            console.print(f"[dim]hint[/dim]  {escape(str(result['hint']))}")
        if result.get("next_hint"):
            console.print(f"[dim]next[/dim]  {escape(str(result['next_hint']))}")
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else None
        if draft is not None:
            d_ok = bool(draft.get("ok"))
            d_style = "info" if d_ok else ("warning" if draft.get("skipped") else "error")
            console.print(
                f"[{d_style}]then-draft[/]  mode={escape(str(draft.get('draft_mode')))} "
                f"ok={d_ok} skipped={bool(draft.get('skipped'))} "
                f"brand={escape(str(draft.get('preferred_brand') or ''))} "
                f"category={escape(str(draft.get('category_keyword') or ''))}"
            )
            if draft.get("skip_reason"):
                console.print(f"[dim]skip[/dim]  {escape(str(draft['skip_reason']))}")
            if draft.get("error") and not draft.get("skipped"):
                console.print(f"[error]{escape(str(draft['error']))}[/error]")
    if not result.get("ok"):
        raise typer.Exit(code=1)


@shop_catalog_app.command("watch-inbox")
def shop_catalog_watch_inbox(
    business: str = typer.Option("ampuly", "--business", help="business_id"),
    shop: str = typer.Option("sandbox-shop", "--shop", help="shop_id"),
    once: bool = typer.Option(
        True,
        "--once/--loop",
        help="One-shot poll (default) or loop with --max-cycles",
    ),
    poll_seconds: float = typer.Option(
        2.0,
        "--poll-seconds",
        help="Sleep between cycles when --loop",
    ),
    max_cycles: int = typer.Option(
        1,
        "--max-cycles",
        help="Max poll cycles when --loop (ignored when --once)",
    ),
    then_draft: bool = typer.Option(
        False,
        "--then-draft",
        help="After each successful ingest, attempt listing-draft",
    ),
    draft_mode: str = typer.Option(
        "dry-run",
        "--draft-mode",
        help="off | dry-run (default) | live",
    ),
    profile: str = typer.Option(
        "sandbox-shop",
        "--profile",
        help="Ego profile id when --draft-mode live",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit summary JSON"),
) -> None:
    """Poll catalog/inbox for drops → ingest → optional then-draft.

    Foolproof Owner path (offline plan):
      ampuly shop catalog watch-inbox --once --then-draft --draft-mode dry-run

    Live Ego draft (needs seller login):
      ampuly shop catalog watch-inbox --once --then-draft --draft-mode live
    """
    from ampuly.layer4_tools.shop_catalog import watch_inbox
    from ampuly.layer4_tools.shop_workspace import ampuly_home

    mode = draft_mode.replace("-", "_") if draft_mode else "dry_run"
    result = watch_inbox(
        ampuly_home(),
        business_id=business,
        shop_id=shop,
        once=once,
        poll_seconds=poll_seconds,
        max_cycles=max_cycles,
        then_draft=then_draft,
        draft_mode=mode,
        profile_id=profile,
    )
    if json_output:
        console.print_json(data=result)
    else:
        console.print(
            f"[info]catalog watch-inbox[/info]  processed={result.get('processed_total')} "
            f"cycles={result.get('cycles')} then_draft={bool(result.get('then_draft'))} "
            f"mode={escape(str(result.get('draft_mode')))}"
        )
        if result.get("inbox_path"):
            console.print(f"[dim]inbox[/dim]  {escape(str(result['inbox_path']))}")
        if result.get("error") == "inbox_empty":
            console.print(f"[warning]inbox empty[/warning]")
        if result.get("hint"):
            console.print(f"[dim]hint[/dim]  {escape(str(result['hint']))}")
        fp = result.get("foolproof") if isinstance(result.get("foolproof"), dict) else {}
        if fp.get("one_command"):
            console.print(f"[dim]cmd[/dim]  {escape(str(fp['one_command']))}")


@shop_master_app.command("show")
def shop_master_show(
    json_output: bool = typer.Option(False, "--json", help="Emit map JSON"),
) -> None:
    """Show AMPULY_HOME/config/business-project-map.json."""
    from ampuly.layer4_tools.shop_master_link import show_map

    card = show_map()
    if json_output:
        console.print_json(data=card)
        return
    console.print(f"[info]business<->project map[/info]  count={card.get('count')}")
    console.print(f"[dim]path[/dim]  {escape(str(card.get('path')))}")
    for row in card.get("mappings") or []:
        console.print(
            f"  - {escape(str(row.get('business_id')))} -> "
            f"{escape(str(row.get('project_id')))}"
        )


@shop_master_app.command("link")
def shop_master_link(
    business: str = typer.Option(..., "--business", help="business_id"),
    project: str = typer.Option(..., "--project", help="project_id"),
    notes: str = typer.Option("", "--notes", help="Optional notes"),
    json_output: bool = typer.Option(False, "--json", help="Emit receipt JSON"),
) -> None:
    """Upsert one business_id <-> project_id mapping under AMPULY_HOME."""
    from ampuly.layer4_tools.shop_master_link import link_business_project

    result = link_business_project(business, project, notes=notes)
    if json_output:
        console.print_json(data=result)
    else:
        ok = bool(result.get("ok"))
        style = "info" if ok else "error"
        console.print(
            f"[{style}]master link {'OK' if ok else 'FAIL'}[/{style}]  "
            f"{escape(business)} -> {escape(project)}"
        )
        if result.get("error"):
            console.print(f"[error]{escape(str(result['error']))}[/error]")
    if not result.get("ok"):
        raise typer.Exit(code=1)


@shop_master_app.command("resolve")
def shop_master_resolve(
    business: str = typer.Option(..., "--business", help="business_id"),
    json_output: bool = typer.Option(False, "--json", help="Emit resolve JSON"),
) -> None:
    """Resolve project_id for a business_id (fallback: default)."""
    from ampuly.layer4_tools.shop_master_link import resolve_project_id

    result = resolve_project_id(business)
    if json_output:
        console.print_json(data=result)
        return
    if not result.get("ok"):
        console.print(f"[error]{escape(str(result.get('error')))}[/error]")
        raise typer.Exit(code=1)
    console.print(
        f"[info]resolve[/info]  {escape(business)} -> "
        f"{escape(str(result.get('project_id')))}"
        + (" (fallback)" if result.get("fallback") else "")
    )



@crm_app.command("ensure-backup-keys")
def crm_ensure_backup_keys(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
) -> None:
    """Create AMPULY_HOME backup-keys + custody doc. Never prints key material."""
    from ampuly.plugins.crm_agent.owner_ops import ensure_backup_keys_layout, _ampuly_home

    receipt = ensure_backup_keys_layout(_ampuly_home())
    if json_output:
        console.print_json(data=receipt)
    else:
        console.print(f"[info]crm ensure-backup-keys OK[/info]  dir={escape(receipt['backup_keys_dir'])}")
        console.print(f"[dim]custody[/dim]  {escape(receipt['custody_doc'])}")
        console.print(f"[dim]key_file_count[/dim]  {receipt['key_file_count']}")


@crm_app.command("ensure-backup-key")
def crm_ensure_backup_key(
    project: str = typer.Option(..., "--project", help="Project id for the wrapping key"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
) -> None:
    """Create-or-load one project backup key. Receipt reports presence/length only."""
    from ampuly.plugins.crm_agent.owner_ops import ensure_project_backup_key, _ampuly_home

    result = ensure_project_backup_key(_ampuly_home(), project)
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        status = "OK" if result.ok else "FAIL"
        style = "info" if result.ok else "error"
        console.print(
            f"[{style}]crm ensure-backup-key {status}[/{style}]  "
            f"project={escape(result.project_id)} present={result.key_present} "
            f"bytes={result.key_bytes} created_now={result.created_now}"
        )
        if result.error:
            console.print(f"[error]{escape(result.error)}[/error]")
    if not result.ok:
        raise typer.Exit(code=1)


@crm_app.command("restore-drill")
def crm_restore_drill(
    project: str = typer.Option(
        "crm-drill-synthetic",
        "--project",
        help="Synthetic drill project id (creates a wrapping key under backup-keys)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Optional path to write the JSON receipt copy",
        path_type=Path,
    ),
) -> None:
    """Throwaway seal→open→tombstone reapply drill. Synthetic only; no production CRM tree."""
    from ampuly.plugins.crm_agent.owner_ops import run_restore_drill, _ampuly_home

    result = run_restore_drill(_ampuly_home(), project_id=project)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json() + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        status = "OK" if result.ok else "FAIL"
        style = "info" if result.ok else "error"
        console.print(f"[{style}]crm restore-drill {status}[/{style}]  project={escape(result.project_id)}")
        console.print(
            f"[dim]opened={result.opened_members} tombstones={result.tombstones_applied} "
            f"erasure_reapplied_audit={result.erasure_reapplied_audit} "
            f"kept={result.kept_present} erased_absent={result.erased_absent}[/dim]"
        )
        if result.error:
            console.print(f"[error]{escape(result.error)}[/error]")
    if not result.ok:
        raise typer.Exit(code=1)



@crm_app.command("record-custody-ack")
def crm_record_custody_ack(
    phrase: str = typer.Option(
        "确认密钥保管",
        "--phrase",
        help="Owner confirmation phrase (stored; no key material)",
    ),
    acknowledged_at: str | None = typer.Option(
        None,
        "--acknowledged-at",
        help="Optional Asia/Shanghai ISO timestamp of the Owner ACK",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
) -> None:
    """Record Owner ACK that lost key = backups unreadable. Writes AMPULY_HOME evidence only."""
    from ampuly.plugins.crm_agent.owner_ops import record_custody_ack, _ampuly_home

    receipt = record_custody_ack(
        _ampuly_home(),
        phrase=phrase,
        source="owner_cli",
        acknowledged_at=acknowledged_at,
    )
    if json_output:
        console.print_json(data=receipt)
    else:
        console.print(
            f"[info]crm record-custody-ack OK[/info]  "
            f"path={escape(receipt['ack_path'])} at={escape(receipt['acknowledged_at'])}"
        )



@crm_app.command("record-same-machine-strict-spotcheck")
def crm_record_same_machine_strict_spotcheck(
    install_path: str = typer.Option(..., "--install-path", help="Clean install path e.g. C:\\w\\d3-clean-spotcheck"),
    wheelhouse_path: str = typer.Option(..., "--wheelhouse-path", help="Release wheelhouse path"),
    headless_ok: bool = typer.Option(False, "--headless-ok/--no-headless-ok"),
    headed_ok: bool = typer.Option(False, "--headed-ok/--no-headed-ok"),
    restore_drill_ok: bool = typer.Option(False, "--restore-drill-ok/--no-restore-drill-ok"),
    host_label: str = typer.Option("limeng", "--host-label"),
    note: str = typer.Option("", "--note"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Record same-machine strict evidence. Does NOT open clean_machine / REAL_DATA."""
    from ampuly.plugins.crm_agent.owner_ops import record_same_machine_strict_spotcheck, _ampuly_home

    receipt = record_same_machine_strict_spotcheck(
        _ampuly_home(),
        install_path=install_path,
        wheelhouse_path=wheelhouse_path,
        headless_ok=headless_ok,
        headed_ok=headed_ok,
        restore_drill_ok=restore_drill_ok,
        host_label=host_label,
        note=note,
    )
    if json_output:
        console.print_json(data=receipt)
    else:
        console.print(
            f"[info]crm record-same-machine-strict-spotcheck[/info]  "
            f"ok={receipt['ok']} gate=false path={escape(receipt['path'])}"
        )
    if not receipt.get("ok"):
        raise typer.Exit(code=1)


@crm_app.command("record-clean-machine-spotcheck")
def crm_record_clean_machine_spotcheck(
    second_physical_pc: bool = typer.Option(
        False,
        "--second-physical-pc/--no-second-physical-pc",
        help="Required True to open clean_machine gate (refuses otherwise)",
    ),
    host_label: str = typer.Option(..., "--host-label", help="Other PC hostname/label"),
    install_path: str = typer.Option(..., "--install-path"),
    wheelhouse_path: str = typer.Option(..., "--wheelhouse-path"),
    headless_ok: bool = typer.Option(False, "--headless-ok/--no-headless-ok"),
    headed_ok: bool = typer.Option(False, "--headed-ok/--no-headed-ok"),
    restore_drill_ok: bool = typer.Option(False, "--restore-drill-ok/--no-restore-drill-ok"),
    note: str = typer.Option("", "--note"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Record clean_machine gate. Refuses unless --second-physical-pc."""
    from ampuly.plugins.crm_agent.owner_ops import record_clean_machine_spotcheck, _ampuly_home

    try:
        receipt = record_clean_machine_spotcheck(
            _ampuly_home(),
            second_physical_pc=second_physical_pc,
            host_label=host_label,
            install_path=install_path,
            wheelhouse_path=wheelhouse_path,
            headless_ok=headless_ok,
            headed_ok=headed_ok,
            restore_drill_ok=restore_drill_ok,
            note=note,
        )
    except ValueError as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        raise typer.Exit(code=1) from exc
    if json_output:
        console.print_json(data=receipt)
    else:
        console.print(
            f"[info]crm record-clean-machine-spotcheck OK[/info]  "
            f"gate=true path={escape(receipt['path'])}"
        )


@crm_app.command("real-data-status")
def crm_real_data_status(
    json_output: bool = typer.Option(False, "--json", help="Emit checklist JSON"),
) -> None:
    """Show REAL_DATA door checklist. Does not flip the code flag."""
    from ampuly.plugins.crm_agent.owner_ops import describe_real_data_gates, _ampuly_home

    try:
        home = _ampuly_home()
    except ValueError:
        home = None
    card = describe_real_data_gates(home)
    if json_output:
        console.print_json(data=card)
        return
    console.print(
        f"[info]crm real-data-status[/info]  "
        f"custody_ack={card.get('custody_ack')}  "
        f"restore_drill={card.get('restore_drill')}  "
        f"clean_machine={card.get('clean_machine')}  "
        f"real_data_open={card.get('real_data_open')}  "
        f"REAL_DATA_PREREQUISITES_MET={card['REAL_DATA_PREREQUISITES_MET']}"
    )
    for item in card["checklist"]:
        mark = "x" if item["met"] else " "
        console.print(f"  [{mark}] {escape(item['id'])}: {escape(item['label'])}")


@crm_app.command("describe")
def crm_describe(
    json_output: bool = typer.Option(False, "--json", help="Emit capability card JSON"),
) -> None:
    """Show CRM Owner capability card."""
    from ampuly.plugins.crm_agent.owner_ops import describe_crm_owner_capability

    card = describe_crm_owner_capability()
    if json_output:
        console.print_json(data=card)
        return
    console.print(f"[info]{escape(card['id'])}[/info]  {escape(card['title'])}")
    for cli_name in card["cli"]:
        console.print(f"[dim]cli[/dim]  {escape(cli_name)}")




@browser_app.command("describe")
def browser_describe(
    json_output: bool = typer.Option(False, "--json", help="Emit capability card JSON"),
) -> None:
    """Show the sandbox-shop Ego Owner capability card."""
    from ampuly.layer4_tools.shop_ego_owner import describe_shop_ego_capability

    card = describe_shop_ego_capability()
    if json_output:
        console.print_json(data=card)
        return
    console.print(f"[info]{escape(card['id'])}[/info]  {escape(card['title'])}")
    console.print(f"[dim]cli[/dim]  {escape(card['cli'])}")
    console.print(
        f"[dim]owner_triggerable[/dim]  {card['owner_triggerable']}  "
        f"[dim]external_agent_required[/dim]  {card['external_agent_required']}"
    )


@browser_app.command("shop-home")
def browser_shop_home(
    profile: str = typer.Option(
        "sandbox-shop",
        "--profile",
        help="Ampuly Ego profile id (sandbox-shop = 沙箱店)",
    ),
    url: str = typer.Option(
        "https://myseller.taobao.com/",
        "--url",
        help="Seller home URL to navigate",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
    no_capture: bool = typer.Option(False, "--no-capture", help="Skip screenshot capture"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Optional path to write the JSON receipt copy",
        path_type=Path,
    ),
) -> None:
    """Open Ego Profile sandbox-shop, navigate seller home, write a Task Space receipt.

    Engine-owned Owner leaf — no external agent. Does not scan chat QR or submit listings.
    """
    from ampuly.layer4_tools.shop_ego_owner import run_shop_home_probe

    result = run_shop_home_probe(profile_id=profile, url=url, capture=not no_capture)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json() + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        status = "OK" if result.ok else "FAIL"
        style = "info" if result.ok else "error"
        console.print(f"[{style}]shop-home {status}[/{style}]  profile={escape(result.profile_id)}  task={escape(result.task_id)}")
        if result.receipt_path:
            console.print(f"[dim]receipt[/dim]  {escape(result.receipt_path)}")
        if result.capture_path:
            console.print(f"[dim]capture[/dim]  {escape(result.capture_path)}")
        if result.error:
            console.print(f"[error]{escape(result.error)}[/error]")
        console.print(
            f"[dim]navigate_ok={result.navigate_ok} snapshot_ok={result.snapshot_ok} "
            f"seller_home_hint={result.seller_home_hint}[/dim]"
        )
    if not result.ok:
        raise typer.Exit(code=1)



@browser_app.command("shop-cs-home")
def browser_shop_cs_home(
    profile: str = typer.Option(
        "sandbox-shop",
        "--profile",
        help="Ampuly Ego profile id (sandbox-shop = 沙箱店)",
    ),
    url: str = typer.Option(
        "https://myseller.taobao.com/home.htm/_firstmenuid_/2416",
        "--url",
        help="客服/千牛 entry URL to navigate",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
    no_capture: bool = typer.Option(False, "--no-capture", help="Skip screenshot capture"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Optional path to write the JSON receipt copy",
        path_type=Path,
    ),
) -> None:
    """Open Ego Profile sandbox-shop, navigate 客服入口, write a Task Space receipt.

    Engine-owned Owner leaf — no external agent. Does not send IM messages.
    """
    from ampuly.layer4_tools.shop_ego_owner import run_shop_cs_home_probe

    result = run_shop_cs_home_probe(profile_id=profile, url=url, capture=not no_capture)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json() + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        status = "OK" if result.ok else "FAIL"
        style = "info" if result.ok else "error"
        console.print(f"[{style}]shop-cs-home {status}[/{style}]  profile={escape(result.profile_id)}  task={escape(result.task_id)}")
        if result.receipt_path:
            console.print(f"[dim]receipt[/dim]  {escape(result.receipt_path)}")
        if result.capture_path:
            console.print(f"[dim]capture[/dim]  {escape(result.capture_path)}")
        if result.error:
            console.print(f"[error]{escape(result.error)}[/error]")
        console.print(
            f"[dim]navigate_ok={result.navigate_ok} snapshot_ok={result.snapshot_ok} "
            f"seller_home_hint={result.seller_home_hint} capability={result.capability}[/dim]"
        )
    if not result.ok:
        raise typer.Exit(code=1)



@browser_app.command("shop-listing-draft")
def browser_shop_listing_draft(
    profile: str = typer.Option(
        "sandbox-shop",
        "--profile",
        help="Ampuly Ego profile id (sandbox-shop)",
    ),
    category: str = typer.Option(
        "平面广告设计",
        "--category",
        help="This product's category. The shop allowlist only checks it.",
    ),
    brand: str = typer.Option(
        "",
        "--brand",
        help="This product's explicit brand. Marketplace placeholder values are not authorization.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Draft title (default A21draft-HHMMSS)",
    ),
    wait_category: int = typer.Option(
        300,
        "--wait-category",
        help="Seconds to wait for Owner to pick category leaf in live Ego window",
    ),
    allow_without_rules: bool = typer.Option(
        False,
        "--allow-without-rules",
        help="Emergency bypass of accepted shop-rules gate (receipts will flag)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
    no_capture: bool = typer.Option(False, "--no-capture", help="Skip screenshot capture"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Optional path to write the JSON receipt copy",
        path_type=Path,
    ),
) -> None:
    """Bounded Ego listing draft/warehouse + readback (engine-owned).

    May pause for Owner to click the category leaf in the *live* Ego window
    (never chat QR). Does not elevate A21 YAML or flip IM wire_ready.
    """
    from ampuly.layer4_tools.shop_listing_owner import run_shop_listing_draft

    result = run_shop_listing_draft(
        profile_id=profile,
        category_keyword=category,
        brand=brand or None,
        title=title,
        wait_category_seconds=wait_category,
        capture=not no_capture,
        allow_without_rules=allow_without_rules,
    )
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json() + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        status = "OK" if result.ok else "FAIL"
        style = "info" if result.ok else "error"
        console.print(
            f"[{style}]shop-listing-draft {status}[/{style}]  "
            f"profile={escape(result.profile_id)}  task={escape(result.task_id)}"
        )
        if result.owner_assist_needed:
            console.print(
                f"[warning]Owner assist: {escape(result.owner_window)} — "
                f"{escape(result.owner_step)}[/warning]"
            )
        if result.receipt_path:
            console.print(f"[dim]receipt[/dim]  {escape(result.receipt_path)}")
        if result.error:
            console.print(f"[error]{escape(result.error)}[/error]")
        ready = result.readiness or {}
        console.print(
            f"[dim]available={ready.get('available')} missing={ready.get('missing')}[/dim]"
        )
    if not result.ok:
        raise typer.Exit(code=1)


@browser_app.command("shop-im-wire")
def browser_shop_im_wire(
    profile: str = typer.Option(
        "sandbox-shop",
        "--profile",
        help="Ampuly Ego profile id (sandbox-shop)",
    ),
    url: str = typer.Option(
        "https://myseller.taobao.com/home.htm/_firstmenuid_/2416",
        "--url",
        help="客服/千牛 entry URL",
    ),
    no_dry_run: bool = typer.Option(
        False,
        "--no-dry-run",
        help="Skip composer dry-run fill",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable receipt"),
    no_capture: bool = typer.Option(False, "--no-capture", help="Skip screenshot capture"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Optional path to write the JSON receipt copy",
        path_type=Path,
    ),
) -> None:
    """Ego UI IM wire probe (wire_kind=ego_browser_ui).

    Sets wire_ready only for browser CS control. Does not claim Alibaba push
    protocol or accept_inbound for alibaba_shop. Never sends messages.
    """
    from ampuly.layer4_tools.shop_im_owner import run_shop_im_wire_probe

    result = run_shop_im_wire_probe(
        profile_id=profile,
        url=url,
        do_dry_run_fill=not no_dry_run,
        capture=not no_capture,
    )
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json() + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=result.to_dict())
    else:
        status = "OK" if result.ok else "FAIL"
        style = "info" if result.ok else "error"
        console.print(
            f"[{style}]shop-im-wire {status}[/{style}]  "
            f"wire_ready={result.wire_ready}  kind={escape(result.wire_kind)}"
        )
        if result.receipt_path:
            console.print(f"[dim]receipt[/dim]  {escape(result.receipt_path)}")
        if result.error:
            console.print(f"[error]{escape(result.error)}[/error]")
        console.print(
            f"[dim]alibaba_push={result.alibaba_push_protocol} "
            f"dry_run_fill={result.dry_run_fill_ok}[/dim]"
        )
    if not result.ok:
        raise typer.Exit(code=1)



@distribution_app.command("check")
def distribution_check() -> None:
    """Validate the distribution layer before committing or shipping."""
    from ampuly.cli.distribution import run_check

    repo = Path(__file__).resolve().parents[2]
    report = run_check(repo)
    for note in report.notes:
        console.print(f"[dim]{escape(note)}[/dim]")
    for problem in report.problems:
        console.print(f"[error]{escape(problem)}[/error]")
    console.print("[info]distribution check: OK[/info]" if report.ok else f"[error]distribution check: {len(report.problems)} problem(s)[/error]")
    if not report.ok:
        raise typer.Exit(code=1)


@distribution_app.command("init")
def distribution_init(
    customer: str = typer.Argument(..., help="Customer id (lowercase, digits, - or _)"),
    org: str = typer.Option(..., "--org", help="Organization id to deploy (required)"),
    package: list[str] = typer.Option([], "--package", "-p", help="Workflow package id (repeatable)"),
    model: str = typer.Option("deepseek/deepseek-chat", "--model", help="Default model"),
) -> None:
    """Scaffold deploy/customers/<customer>/ for an FDE delivery (no secrets)."""
    from ampuly.cli.distribution import init_customer

    repo = Path(__file__).resolve().parents[2]
    try:
        root = init_customer(repo, customer, packages=list(package), org_id=org, model=model)
    except (ValueError, FileExistsError) as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(f"[info]created {escape(str(root))}[/info]")


@distribution_app.command("package")
def distribution_package(
    customer: str = typer.Argument(..., help="Customer id scaffolded by `ampuly distribution init`"),
    commit: str = typer.Option("", "--commit", help="Commit to package (defaults to HEAD)"),
) -> None:
    """Build the archive a customer receives, with an SBOM bound to it."""
    from ampuly.cli.release_package import PackagingError, package_customer_release

    repo = Path(__file__).resolve().parents[2]
    try:
        package = package_customer_release(repo, customer, commit=commit or None)
    except PackagingError as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        raise typer.Exit(code=1) from exc
    console.print(f"[info]{escape(package.archive.name)}[/info]  {package.file_count} file(s) from {package.commit[:12]}")
    console.print(f"[dim]sha256 {package.archive_sha256}[/dim]")
    console.print(f"[dim]{escape(str(package.sbom))}[/dim]")
    console.print(f"[dim]{escape(str(package.manifest))}[/dim]")
    if package.working_tree_dirty:
        console.print("[warn]the working tree has uncommitted changes; the archive is HEAD, not what is on disk[/warn]")


@app.command("maintenance")
def maintenance(
    keep_log_days: int = typer.Option(30, "--keep-log-days", help="Delete log day folders older than this (system logs: 30 days)"),
    force: bool = typer.Option(False, "--force", help="VACUUM even if something answers on a console port (never past a held office_ui.lock)"),
    apply: bool = typer.Option(False, "--apply", help="Actually delete what the listing showed (without this, nothing is removed)"),
):
    """List what a cleanup would remove; with --apply, remove it (stop `ampuly ui` first).

    数据与文件 P3: owner 的裁决是「清理先列出将删除的内容，确认后再删」，所以不加 --apply 时一个文件都不动。
    """
    from ampuly.cli.maintenance import run_maintenance
    from ampuly.core.config import get_ampuly_home

    report = run_maintenance(get_ampuly_home(), keep_log_days=keep_log_days, force=force, apply=apply)
    for line in report.lines():
        console.print(line)
    if report.skipped and not report.databases:
        raise typer.Exit(code=2)


@app.command("db-version")
def db_version_command(
    apply: bool = typer.Option(False, "--apply", help="真的打版本号、升级（不加时只列出来，一个字节都不写）"),
    force: bool = typer.Option(False, "--force", help="控制台端口上有人应答也照样执行（正在用这个数据目录的控制台，加了也不行）"),
    home_dir: str = typer.Option("", "--home", help="要检查的数据目录（数据页上给出的命令里已经写好；不写就用当前环境默认的那个）"),
) -> None:
    """运行库的数据格式版本号：列出每个库在第几版；加 --apply 才真的去改（先备份，出错自动退回）。

    已有的库还没有版本号时，控制台开机只看不改，只在数据页上提示。要改的话，先停掉控制台，再运行这条命令。
    """
    from ampuly.cli.maintenance import CONSOLE_PORTS, console_holds_home, console_listening
    from ampuly.core.config import get_ampuly_home
    from ampuly.database.schema_version import migrate_home

    home = Path(home_dir.strip()).expanduser() if home_dir.strip() else get_ampuly_home()
    console.print(escape(f"数据目录：{home}"), soft_wrap=True)
    if apply:
        if console_holds_home(home):
            console.print(escape(f"控制台正在用 {home} 下的库，先停掉控制台再运行。"), soft_wrap=True)
            raise typer.Exit(code=2)
        busy = next((port for port in CONSOLE_PORTS if console_listening(port)), None)
        if busy is not None and not force:
            console.print(escape(f"端口 {busy} 上有控制台在应答，先停掉它再运行；"
                                 "确定它用的不是这里的库时，可以加 --force。"), soft_wrap=True)
            raise typer.Exit(code=2)
    report = migrate_home(home, apply=apply)
    for line in report["lines"]:
        console.print(escape(line), soft_wrap=True)
    if not report["ok"]:
        raise typer.Exit(code=1)


@app.command("migrate-home")
def migrate_home_command(
    apply: bool = typer.Option(False, "--apply", help="真的迁移。不加时只列出计划，不写盘"),
    rollback: bool = typer.Option(False, "--rollback", help="退回上一次成功的迁移：目标改名留档，当初挪走的旧目标放回（不删除来源）"),
    replace_target: bool = typer.Option(False, "--replace-target", help="目标不是空库影子时，仍把整个目标挪到旁边再迁移（不删除）"),
    source: str = typer.Option("", "--from", help="旧数据目录，默认是项目下的 .opc"),
    dest: str = typer.Option("", "--to", help="新数据目录，默认是项目下的 .ampuly"),
) -> None:
    """把旧的 .opc 数据目录复制到 .ampuly。默认只预览；旧目录保留，不删除。

    先停掉控制台。``--apply`` 会做 SQLite 一致性备份，空库影子会被挪到旁边而不是删除。
    ``--rollback`` 退回上一次成功的迁移。
    """
    from ampuly.core.home_migration import migrate_project_home, rollback_project_home

    if apply and rollback:
        console.print("不能同时加上 --apply 和 --rollback。")
        raise typer.Exit(code=1)
    root = _find_repo_root()
    kwargs = {
        "source": Path(source).expanduser() if source.strip() else None,
        "dest": Path(dest).expanduser() if dest.strip() else None,
    }
    if rollback:
        report = rollback_project_home(root, **kwargs)
    else:
        report = migrate_project_home(root, apply=apply, replace_target=replace_target, **kwargs)
    for line in report.lines():
        console.print(escape(line), soft_wrap=True)
    if not report.ok():
        raise typer.Exit(code=1)


def _find_repo_root() -> Path:
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return cwd


@app.command("diagnose")
def diagnose_command(
    port: int | None = typer.Option(
        None, "--port",
        help="Local console port to inspect (default: AMPULY_UI_PORT, else the port this home's console recorded, else 8765)",
    ),
    stale_minutes: int = typer.Option(30, "--stale-minutes", min=1, help="Age before an in-flight record is suspicious"),
    json_output: bool = typer.Option(False, "--json", help="Emit the stable machine-readable report"),
) -> None:
    """Inspect runtime health without changing, retrying or cleaning anything."""
    from ampuly.cli.diagnostics import diagnose
    from ampuly.cli.ui_bridge import console_port
    from ampuly.core.config import get_ampuly_home

    report = diagnose(get_ampuly_home(), port=console_port(port), stale_minutes=stale_minutes)
    if json_output:
        console.print_json(report.to_json())
    else:
        for line in report.lines():
            console.print(escape(line))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("exec")
def exec_command(
    prompt: Optional[str] = typer.Argument(None, help="Prompt to run non-interactively. Reads stdin when omitted."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID to work in"),
    mode: str = typer.Option("task", "--mode", help="Execution mode: task, company, or org"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="Company profile for company mode"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred agent ID; jiuwen = JiuwenSwarm-single, jiuwenswarm = JiuwenSwarm-team"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id", help="Saved org id for org/custom mode"),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Existing task-backed session id to reuse"),
    resume: bool = typer.Option(False, "--resume", help="Resume the latest task-backed session in the project"),
    json_output: bool = typer.Option(False, "--json", help="Print final JSON payload"),
    stream_json: bool = typer.Option(False, "--stream-json", help="Print newline-delimited JSON runtime events"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Plain text output"),
    approval_level: Optional[str] = typer.Option(None, "--approval-level", help="Native permissions: read-only, auto, or full-access"),
    standalone: bool = typer.Option(False, "--standalone", help="Boot a private engine even when the console (ampuly ui) is running"),
):
    """Run one non-interactive AMPULY task for scripts and CI.

    When the console (``ampuly ui``) is listening on its port, the prompt is
    handed to it over the same WebSocket the browser uses, so the run lives
    in the one control plane (its approvals, patrol and pager). Pass
    --standalone or set AMPULY_CLI_STANDALONE=1 to boot a private engine.
    """
    if json_output and stream_json:
        console.print("[error]Use either --json or --stream-json, not both.[/error]")
        raise typer.Exit(code=2)
    if session_id and resume:
        console.print("[error]Use either --session-id or --resume, not both.[/error]")
        raise typer.Exit(code=2)
    message = prompt
    if message is None and not _stdin_is_interactive():
        message = sys.stdin.read()
    config = _get_config()
    native_approval_level = (
        _parse_native_approval_level(approval_level)
        if approval_level is not None
        else None
    )
    if not standalone and not ui_bridge.bridge_disabled():
        relayed = asyncio.run(_exec_via_console(
            prompt=str(message or "").strip(),
            project=project,
            mode=mode,
            company_profile=company_profile,
            preferred_agent=agent,
            org_id=org_id,
            session_id=session_id,
            json_output=json_output,
            stream_json=stream_json,
            no_markdown=no_markdown,
            native_approval_level=(native_approval_level.value if native_approval_level is not None else None),
        ))
        if relayed is not None:
            raise typer.Exit(code=0 if relayed.status in {"done", "delivered"} else 1)
    try:
        asyncio.run(_exec_message(
            config=config,
            prompt=str(message or "").strip(),
            project=project,
            mode=mode,
            company_profile=company_profile,
            preferred_agent=agent,
            org_id=org_id,
            session_id=session_id,
            resume=resume,
            json_output=json_output,
            stream_json=stream_json,
            no_markdown=no_markdown,
            native_approval_level=native_approval_level,
        ))
    except KeyboardInterrupt as exc:
        if stream_json:
            _print_exec_event(
                {"seq": 0},
                "error",
                project_id=project or "default",
                payload={"code": "cancelled", "error": "Interrupted"},
            )
        else:
            console.print("\n[warning]Interrupted.[/warning]")
        raise typer.Exit(code=130) from exc


@app.command()
def secretary(
    message: Optional[str] = typer.Argument(None, help="Single secretary message"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID for secretary context"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default LLM model"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed logs"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Plain text output"),
):
    """Talk directly with the long-term secretary interface."""
    config = _get_config()
    if model:
        config.llm.default_model = model
    if verbose:
        config.system.log_level = "DEBUG"

    if message:
        asyncio.run(_single_secretary_message(config, message, project, no_markdown))
    else:
        asyncio.run(_interactive_secretary_mode(config, project, no_markdown))


@app.command("propose-reorg")
def propose_reorg(
    payload: str = typer.Argument(..., help="JSON payload with summary/rationale/changeset"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Create a runtime company reorg proposal."""
    config = _get_config()
    asyncio.run(_propose_reorg(config, payload, project))


@app.command("approve-reorg")
def approve_reorg(
    proposal_id: str = typer.Argument(..., help="Reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Approve a pending company reorg proposal."""
    config = _get_config()
    asyncio.run(_approve_reorg(config, proposal_id, project, approved=True))


@app.command("deny-reorg")
def deny_reorg(
    proposal_id: str = typer.Argument(..., help="Reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Deny a pending company reorg proposal."""
    config = _get_config()
    asyncio.run(_approve_reorg(config, proposal_id, project, approved=False))


@app.command("apply-reorg")
def apply_reorg(
    proposal_id: str = typer.Argument(..., help="Approved reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Apply an approved company reorg proposal."""
    config = _get_config()
    asyncio.run(_apply_reorg(config, proposal_id, project))


@app.command("show-reorg")
def show_reorg(
    proposal_id: str = typer.Argument(..., help="Reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Show a company reorg proposal."""
    config = _get_config()
    asyncio.run(_show_reorg(config, proposal_id, project))


def _template_has_required_config_files(source: Any) -> bool:
    try:
        return (
            source.is_dir()
            and source.joinpath("llm_config.yaml").is_file()
            and source.joinpath("system_config.yaml").is_file()
        )
    except Exception:
        return False


def _project_config_template_dir() -> Any | None:
    """Return the best config template source.

    Source checkouts use ``project_root/config``. Installed packages fall back
    to ``ampuly/config_templates`` shipped as package data via ``importlib.resources``.
    """
    from ampuly.core.config import get_ampuly_home

    ampuly_home = get_ampuly_home()
    project_root = ampuly_home.parent
    repo_config = project_root / "config"
    if _template_has_required_config_files(repo_config):
        return repo_config

    try:
        packaged_config = importlib_resources.files("ampuly").joinpath("config_templates")
    except Exception:
        packaged_config = None
    if packaged_config is not None and _template_has_required_config_files(packaged_config):
        return packaged_config
    return None


def _copy_template_tree(source: Any, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        dest = target / child.name
        if child.is_dir():
            _copy_template_tree(child, dest)
        elif child.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())


def _load_config_template(source: Any) -> AmpulyConfig:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        _copy_template_tree(source, tmp_path)
        return AmpulyConfig.load(tmp_path)


def _ampuly_config_initialized(ampuly_home: Path) -> bool:
    config_dir = ampuly_home / "config"
    if not config_dir.exists():
        return False
    expected = (
        "llm_config.yaml",
        "system_config.yaml",
        "agent_config.yaml",
        "org_config.yaml",
    )
    if any((config_dir / name).exists() for name in expected):
        return True
    return any(config_dir.iterdir())


def _trust_configured_external_agents(
    config: AmpulyConfig,
    ampuly_home: Path,
    *,
    project_id: str | None,
) -> None:
    from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager

    manager = ApprovalAllowlistManager(ampuly_home)
    manager.ensure_file()
    scope_project = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
    for agent_name, agent_config in config.agents.agents.items():
        if not getattr(agent_config, "enabled", True):
            continue
        manager.add_patterns(
            "external_agent",
            agent_name,
            ["*"],
            project_id=scope_project,
        )


def _run_init_external_agent_preflight(
    config: AmpulyConfig,
    *,
    project_id: str,
    ampuly_home: Path,
    workspace_path: Path,
) -> None:
    from ampuly.layer3_agent.preflight import run_external_agent_preflight

    results = run_external_agent_preflight(
        config,
        project_id=project_id,
        workspace_path=workspace_path,
        ampuly_home=ampuly_home,
        probe_commands=False,
        prepare_surfaces=True,
    )
    available = sum(1 for item in results if item.available)
    configured = sum(1 for item in results if item.enabled)
    failures = [item for item in results if item.enabled and item.issues]
    console.print(
        f"[info]External agent preflight:[/info] {available}/{configured} configured agents found; "
        f"collab surfaces and workspace permissions checked."
    )
    for item in failures:
        console.print(
            f"  [warning]- {execution_agent_label(item.agent)}: "
            f"{'; '.join(item.issues[:2])}[/warning]"
        )


def _render_external_agent_detection(config: AmpulyConfig) -> None:
    import shutil

    from ampuly.layer3_agent.adapters.registry import ADAPTER_CLASSES

    for agent_name, agent_config in config.agents.agents.items():
        adapter_cls = ADAPTER_CLASSES.get(agent_name)
        if adapter_cls is not None:
            adapter = adapter_cls(config=agent_config)
            command = adapter.configured_command()
            found = adapter.resolve_binary()
        else:
            command = agent_config.command
            found = shutil.which(command)
        if found:
            console.print(f"  - {execution_agent_label(agent_name)}: [success]found[/success] ({found})")
        else:
            console.print(f"  - {execution_agent_label(agent_name)}: [warning]not found[/warning] (command={command})")


def _render_external_agent_preflight_table(results: list[Any]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Agent")
    table.add_column("Binary")
    table.add_column("Mode")
    table.add_column("Stdin")
    table.add_column("Workspace")
    table.add_column("Collab")
    table.add_column("Notes")
    for item in results:
        if not item.enabled:
            binary = "[warning]disabled[/warning]"
        elif item.available:
            binary = f"[success]found[/success]\n{item.binary}"
        else:
            binary = f"[error]missing[/error]\n{item.command}"
        mode = ""
        if item.launch_command:
            mode = _truncate_status_cell(item.launch_command, limit=180)
        elif item.version:
            mode = item.version
        workspace_ok = all(check.ok for check in item.write_checks)
        workspace = "[success]writable[/success]" if workspace_ok else "[error]blocked[/error]"
        collab = "[success]ready[/success]" if item.collab_cli and not item.issues else "[warning]check[/warning]"
        rpc_transport = str(getattr(item, "collaboration_rpc_transport", "") or "").strip()
        if rpc_transport:
            collab = f"{collab}\nRPC: {rpc_transport}"
        notes = []
        if item.issues:
            notes.extend(item.issues[:2])
        if item.warnings:
            notes.extend(item.warnings[:2])
        table.add_row(
            execution_agent_label(item.agent),
            binary,
            mode,
            str(getattr(item, "stdin_policy", "") or "-"),
            workspace,
            collab,
            _truncate_status_cell("; ".join(notes) if notes else "ok", limit=160),
        )
    console.print(table)


def _truncate_status_cell(value: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


@app.command()
def init(
    project: Optional[str] = typer.Argument(None, help="Project ID to initialize"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Continue when SuperOnes is already initialized; existing config is preserved."),
    external_agent_preflight: bool = typer.Option(
        True,
        "--external-agent-preflight/--no-external-agent-preflight",
        help="Provision and validate external-agent homes, collab CLI, and workspace permissions.",
    ),
    trust_external_agents: bool = typer.Option(
        True,
        "--trust-external-agents/--no-trust-external-agents",
        help="Persist SuperOnes approval rules for configured external-agent launches.",
    ),
):
    """Initialize AMPULY configuration and workspace."""
    from ampuly.core.config import get_ampuly_home, get_project_workplace

    ampuly_home = get_ampuly_home()
    template_dir = _project_config_template_dir()
    config_dir_preexisting = (ampuly_home / "config").exists()
    already_initialized = _ampuly_config_initialized(ampuly_home)
    created_project_config = not config_dir_preexisting
    config: AmpulyConfig

    if already_initialized:
        if not yes and not typer.confirm(
            f"SuperOnes is already initialized at {ampuly_home}. Continue without overwriting existing config?",
            default=False,
        ):
            console.print("[warning]Init cancelled. Existing config was left unchanged.[/warning]")
            raise typer.Exit(1)
        console.print(f"[info]Existing config preserved: {ampuly_home / 'config'}[/info]")
        config = _load_config_with_workspace_trust(ampuly_home / "config")
    elif template_dir is not None:
        # Use repo config template (same setup as maintainers, keys left for user to set)
        config = _load_config_template(template_dir)
        config.llm.api_key = ""
        config.save(ampuly_home / "config")
    else:
        config = AmpulyConfig()
        if not config.org.roles:
            from ampuly.core.config import RoleConfig, EscalationRule
            browser_tools = [
                "browser_navigate",
                "browser_navigate_back",
                "browser_click",
                "browser_snapshot",
                "browser_observe",
                "browser_type",
                "browser_submit_enter",
                "browser_wait_for",
                "browser_scroll",
                "browser_select_option",
                "browser_take_screenshot",
                "browser_close",
            ]
            planning_tools = [
                "file_read",
                "file_search",
                "list_dir",
                "todo_write",
                "todo_read",
            ]
            engineering_tools = [
                "shell_exec",
                "file_read",
                "file_write",
                "file_edit",
                "file_search",
                "list_dir",
                "web_search",
                "web_fetch",
                "todo_write",
                "todo_read",
                *browser_tools,
            ]
            review_tools = [
                "file_read",
                "file_search",
                "list_dir",
                "shell_exec",
                "browser_navigate",
                "browser_navigate_back",
                "browser_snapshot",
                "browser_observe",
                "browser_wait_for",
                "browser_scroll",
                "browser_select_option",
                "browser_take_screenshot",
            ]
            config.org.roles = [
                RoleConfig(
                    id="ceo",
                    name="CEO",
                    icon="leader",
                    responsibility="Strategic intake, high-level routing, final aggregation and delivery to the owner",
                    reports_to="owner",
                    can_spawn=["cto", "cmo", "coo"],
                    capabilities=[
                        "strategic_planning", "work_delegation",
                        "cross_team_coordination", "final_synthesis",
                        "delivery_review",
                    ],
                    tools=planning_tools,
                ),
                RoleConfig(
                    id="cto",
                    name="CTO",
                    icon="code",
                    responsibility="Technical planning, architecture decisions, code review, and engineering oversight",
                    reports_to="ceo",
                    can_spawn=["senior_engineer", "devops_engineer"],
                    capabilities=[
                        "technical_planning", "architecture_design",
                        "technical_research", "engineering_oversight",
                        "code_review", "research_synthesis",
                    ],
                    tools=[*planning_tools, "shell_exec", "web_search", "web_fetch"],
                ),
                RoleConfig(
                    id="cmo",
                    name="CMO",
                    icon="marketing",
                    responsibility="Marketing strategy, content planning, UX review, and brand oversight",
                    reports_to="ceo",
                    can_spawn=["content_specialist", "designer"],
                    capabilities=[
                        "market_research", "industry_analysis",
                        "marketing_strategy", "content_strategy",
                        "user_research", "brand_strategy",
                        "research_synthesis",
                    ],
                    tools=[
                        *planning_tools,
                        "web_search",
                        "web_fetch",
                        "browser_navigate",
                        "browser_navigate_back",
                        "browser_snapshot",
                        "browser_observe",
                        "browser_wait_for",
                        "browser_scroll",
                        "browser_take_screenshot",
                    ],
                ),
                RoleConfig(
                    id="coo",
                    name="COO",
                    icon="strategy",
                    responsibility="Operations coordination, process management, cross-team alignment, and quality assurance",
                    reports_to="ceo",
                    can_spawn=["qa_analyst"],
                    capabilities=[
                        "operational_planning", "cross_team_coordination",
                        "quality_assurance", "source_acquisition",
                        "compliance_review", "research_synthesis",
                    ],
                    tools=[
                        *planning_tools,
                        "shell_exec",
                        "browser_navigate",
                        "browser_navigate_back",
                        "browser_snapshot",
                        "browser_observe",
                        "browser_wait_for",
                        "browser_scroll",
                        "browser_take_screenshot",
                    ],
                ),
                RoleConfig(
                    id="senior_engineer",
                    name="Senior Engineer",
                    icon="terminal",
                    responsibility="Code implementation, system development, and technical execution",
                    reports_to="cto",
                    preferred_external_agent="codex",
                    capabilities=[
                        "software_implementation", "system_development",
                        "technical_execution", "testing",
                    ],
                    tools=engineering_tools,
                ),
                RoleConfig(
                    id="devops_engineer",
                    name="DevOps Engineer",
                    icon="settings",
                    responsibility="Infrastructure, deployment, CI/CD, monitoring, and operational hardening",
                    reports_to="cto",
                    preferred_external_agent="cursor",
                    capabilities=[
                        "infrastructure_engineering", "ci_cd", "deployment",
                        "monitoring", "operational_hardening",
                    ],
                    tools=engineering_tools,
                ),
                RoleConfig(
                    id="content_specialist",
                    name="Content Specialist",
                    icon="writing",
                    responsibility="Documentation, copywriting, presentations, and user-facing writing",
                    reports_to="cmo",
                    capabilities=[
                        "documentation", "copywriting", "presentation_design",
                        "user_facing_writing",
                    ],
                    tools=[
                        "file_read",
                        "file_write",
                        "file_edit",
                        "file_search",
                        "list_dir",
                        "web_search",
                        "web_fetch",
                        "todo_write",
                        "todo_read",
                        *browser_tools,
                    ],
                ),
                RoleConfig(
                    id="designer",
                    name="Designer",
                    icon="design",
                    responsibility="Visual design, UX artifacts, wireframes, and design system work",
                    reports_to="cmo",
                    capabilities=[
                        "visual_design", "ux_design", "wireframing",
                        "design_systems",
                    ],
                    tools=[
                        "file_read",
                        "file_write",
                        "file_edit",
                        "file_search",
                        "list_dir",
                        "web_search",
                        "web_fetch",
                        "todo_write",
                        "todo_read",
                        *browser_tools,
                    ],
                ),
                RoleConfig(
                    id="qa_analyst",
                    name="QA Analyst",
                    icon="bug",
                    responsibility="Testing, security review, compliance checks, and acceptance validation",
                    reports_to="coo",
                    capabilities=[
                        "testing", "security_review", "compliance_review",
                        "acceptance_validation", "quality_assurance",
                    ],
                    tools=review_tools,
                ),
            ]
            config.org.escalation_rules = [
                EscalationRule(condition="3 consecutive failures with no progress", action="Escalate to owner with failure reason"),
                EscalationRule(condition="External account or credentials required", action="Escalate to owner"),
                EscalationRule(condition="Security vulnerability severity >= HIGH", action="Immediately halt and escalate"),
                EscalationRule(condition="Budget exceeds 80% of limit", action="Alert owner and await instructions"),
            ]
        config.save(ampuly_home / "config")

    if created_project_config:
        workspace = get_project_config_workspace(ampuly_home / "config")
        if workspace is not None:
            from ampuly.core.workspace_trust import WorkspaceTrustStore

            # Bind the normalized on-disk form, not the pre-serialization
            # template object, so the next load reproduces the same authority.
            config = AmpulyConfig.load(ampuly_home / "config", trusted_source=True)
            WorkspaceTrustStore().trust(workspace, ampuly_home / "config", config)

    # Create default directories. ``agent_homes/`` and ``bin/`` get
    # provisioned lazily by the skill installer the first time an
    # external agent launches.
    for subdir in ["memory", "skills/core", "skills/cache", "skills/learned", "logs", "prompts/talent"]:
        (ampuly_home / subdir).mkdir(parents=True, exist_ok=True)

    # Create default skills
    _create_default_skills(ampuly_home)

    # Create default global memory.
    from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from ampuly.layer5_memory.markdown_memory import MarkdownMemoryStore
    memory_store = MarkdownMemoryStore(ampuly_home)
    if not memory_store.load_raw_text():
        memory_store.save_visible_text("# Global Memory", None)
    ApprovalAllowlistManager(ampuly_home).ensure_file()

    if project:
        if not is_valid_project_id(project):
            console.print("[error]Invalid project ID (use alphanumeric, hyphens, underscores).[/error]")
            raise typer.Exit(1)
        proj_dir = ampuly_home / "projects" / project
        project_memory = memory_store.memory_path(project)
        workplace = get_project_workplace(project)
        if proj_dir.exists() or project_memory.exists() or workplace.exists():
            console.print(f"[error]Project '{project}' already exists.[/error]")
            raise typer.Exit(1)
        proj_dir.mkdir(parents=True, exist_ok=False)
        workplace.mkdir(parents=True, exist_ok=False)
        memory_store.ensure_memory_file(project, f"# Project Memory ({project})")
        console.print(f"[success]Project '{project}' initialized at {proj_dir}[/success]")
        console.print(f"  Workplace: {workplace}")

    if trust_external_agents:
        _trust_configured_external_agents(config, ampuly_home, project_id=project)

    if external_agent_preflight:
        _run_init_external_agent_preflight(
            config,
            project_id=project or "default",
            ampuly_home=ampuly_home,
            workspace_path=get_project_workplace(project or "default"),
        )

    console.print(f"[success]SuperOnes initialized at {ampuly_home}[/success]")
    console.print(f"  Config: {ampuly_home / 'config'}")
    console.print(f"  Memory: {ampuly_home / 'memory'}")
    console.print(f"  Skills: {ampuly_home / 'skills'}")
    console.print(f"\nEdit [bold]{ampuly_home / 'config' / 'llm_config.yaml'}[/bold] to set your API key.")


@app.command()
def status(
    project: str = typer.Option("default", "--project", "-p", help="Project ID used for workspace preflight."),
    external_agent_preflight: bool = typer.Option(
        True,
        "--external-agent-preflight/--no-external-agent-preflight",
        help="Run external-agent command, flag, collab, and workspace checks.",
    ),
    probe_agent_commands: bool = typer.Option(
        True,
        "--probe-agent-commands/--no-probe-agent-commands",
        help="Run short --version/--help probes to detect unsupported CLI flags.",
    ),
):
    """Show AMPULY system status."""
    from ampuly.core.config import get_ampuly_home

    config = _get_config()
    ampuly_home = get_ampuly_home()

    console.print(Panel(f"[bold]SuperOnes Engine v{__version__}[/bold]", border_style="blue"))
    console.print(f"  Home: {ampuly_home}")
    console.print(f"  Model: {config.llm.default_model}")
    console.print(f"  Log level: {config.system.log_level}")
    console.print(f"  Max iterations: {config.system.max_agent_iterations}")
    console.print(f"  Autonomy mode: {config.autonomy.mode}")
    console.print(f"  Max auto-approve risk: {config.autonomy.max_auto_approve_risk}")
    native_level = normalize_native_approval_level(
        config.autonomy.native_approval_level
    )
    console.print(f"  Native permissions default: {native_level}")
    console.print(f"  Native sandbox: {native_sandbox_profile(native_level)['mode']}")

    # Roles
    console.print("\n[bold]Organization:[/bold]")
    console.print(f"  Company: {config.org.company_name}")
    for role in config.org.roles:
        console.print(f"  - {role.name} ({role.id}): {role.responsibility[:60]}...")

    # External agents
    console.print("\n[bold]External Agents:[/bold]")
    for name, agent_config in config.agents.agents.items():
        status_str = "[success]enabled[/success]" if agent_config.enabled else "[warning]disabled[/warning]"
        console.print(f"  - {execution_agent_label(name)}: {status_str}")

    if not config.agents.agents:
        console.print("  (none configured)")

    console.print("\n[bold]External Agent Preflight:[/bold]")
    if external_agent_preflight:
        from ampuly.layer3_agent.preflight import run_external_agent_preflight

        results = run_external_agent_preflight(
            config,
            project_id=project,
            probe_commands=probe_agent_commands,
            prepare_surfaces=True,
        )
        _render_external_agent_preflight_table(results)
    else:
        _render_external_agent_detection(config)

    # Projects
    projects_dir = ampuly_home / "projects"
    if projects_dir.exists():
        projects = [p.name for p in projects_dir.iterdir() if p.is_dir()]
        console.print(f"\n[bold]Projects:[/bold] {', '.join(projects) if projects else '(none)'}")

    # Cost
    asyncio.run(_show_cost_summary(config))
    asyncio.run(_show_autonomy_summary(config))


@app.command()
def autonomy_status(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Show autonomy policy, learned preferences, and approval stats."""
    config = _get_config()
    console.print(Panel("[bold]Autonomy Status[/bold]", border_style="blue"))
    console.print(f"  Mode: {config.autonomy.mode}")
    console.print(f"  Enabled: {config.autonomy.enabled}")
    console.print(f"  Max auto-approve risk: {config.autonomy.max_auto_approve_risk}")
    console.print(f"  Confidence threshold: {config.autonomy.approval_confidence_threshold}")
    asyncio.run(_show_autonomy_summary(config, project=project))


@app.command()
def autonomy_reset(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Reset learned autonomy preferences globally or for a project."""
    from ampuly.core.config import get_ampuly_home
    from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from ampuly.layer5_memory.preference import PreferenceManager

    ampuly_home = get_ampuly_home()
    prefs = PreferenceManager(ampuly_home)
    prefs.reset_autonomy_preferences(project_id=project)
    ApprovalAllowlistManager(ampuly_home).reset(project_id=project)
    scope = f"project '{project}'" if project else "global"
    console.print(f"[success]Reset learned autonomy preferences and allowlist rules for {scope}.[/success]")


@app.command()
def autonomy_configure(
    mode: Optional[str] = typer.Option(None, "--mode", help="Autonomy mode, e.g. bounded"),
    max_auto_approve_risk: Optional[str] = typer.Option(None, "--max-risk", help="Max auto-approve risk"),
    approval_confidence_threshold: Optional[float] = typer.Option(None, "--confidence-threshold", help="Approval confidence threshold"),
):
    """Update autonomy configuration settings."""
    from ampuly.core.config import get_ampuly_home

    config = _get_config()
    if mode is not None:
        config.autonomy.mode = mode
    if max_auto_approve_risk is not None:
        config.autonomy.max_auto_approve_risk = max_auto_approve_risk
    if approval_confidence_threshold is not None:
        config.autonomy.approval_confidence_threshold = approval_confidence_threshold
    config.save(get_ampuly_home() / "config")
    console.print("[success]Updated autonomy configuration.[/success]")


@app.command()
def projects():
    """List all projects."""
    from ampuly.core.config import get_ampuly_home, get_project_workplace
    from ampuly.layer5_memory.markdown_memory import MarkdownMemoryStore
    ampuly_home = get_ampuly_home()
    memory_store = MarkdownMemoryStore(ampuly_home)
    projects_dir = ampuly_home / "projects"

    if not projects_dir.exists() or not any(projects_dir.iterdir()):
        console.print("[warning]No projects found.[/warning]")
        console.print("Create one with: ampuly init <project-name>")
        return

    for p in sorted(projects_dir.iterdir()):
        if p.is_dir():
            has_tasks = (p / "tasks.db").exists()
            has_memory = memory_store.memory_path(p.name).exists()
            has_workplace = get_project_workplace(p.name).exists()
            markers = []
            if has_tasks:
                markers.append("tasks")
            if has_memory:
                markers.append("memory")
            if has_workplace:
                markers.append("workplace")
            info = f" ({', '.join(markers)})" if markers else ""
            console.print(f"  - [bold]{p.name}[/bold]{info}")


@app.command()
def skills():
    """List available skills."""
    from ampuly.core.config import get_ampuly_home
    from ampuly.layer5_memory.skill_library import SkillLibrary

    ampuly_home = get_ampuly_home()
    lib = SkillLibrary(ampuly_home)
    lib.load_all()

    skill_list = lib.list_skills()
    if not skill_list:
        console.print("[warning]No skills found.[/warning]")
        console.print(f"Add skills to {ampuly_home / 'skills'}")
        return

    console.print(f"[bold]Skills ({len(skill_list)}):[/bold]")
    for s in skill_list:
        always = " [always]" if s.always else ""
        level = f" [{s.level}]" if s.level else ""
        console.print(f"  - [bold]{s.name}[/bold]{always}{level}")
        if s.description:
            console.print(f"    {s.description}")


@app.command()
def config_show():
    """Show current configuration."""
    import yaml
    config = _get_config()
    console.print(yaml.dump(config.model_dump(), default_flow_style=False))


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _print_json(payload: Any) -> None:
    """Write machine-readable JSON to stdout, unwrapped.

    ``console.print`` wraps at the terminal width, which inserts newlines
    inside long string values -- a `--json` payload with a workspace path in
    it stopped parsing. Rich markup in the data would also be
    interpreted, so JSON never goes through the themed console.
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe) + chr(10)
    try:
        # Always UTF-8: a Windows console defaults to the system codepage and
        # mangles Chinese titles into undecodable bytes.
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        # Captured stdout (pytest, subprocess pipes with a text wrapper).
        sys.stdout.write(text)
        sys.stdout.flush()


def _emit_payload(payload: dict[str, Any], *, json_output: bool = False) -> None:
    if json_output:
        _print_json(payload)
        return
    if not payload:
        console.print("[success]OK[/success]")
        return
    table = Table(show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for key, value in payload.items():
        if isinstance(value, (list, dict)):
            rendered = json.dumps(value, ensure_ascii=False, default=_json_safe)
        else:
            rendered = str(_json_safe(value))
        table.add_row(str(key), _clip_text(rendered, 400))
    console.print(table)


async def _run_service_command(
    project: str | None,
    operation: Any,
    *,
    json_output: bool = False,
    render: Any | None = None,
) -> None:
    from ampuly.core.service_models import ServiceError
    from ampuly.plugins.office_ui.services.factory import OfficeServiceFactory

    async with OfficeServiceFactory(config=_get_config(), project_id=project) as services:
        try:
            result = await operation(services)
        except ServiceError as exc:
            if json_output:
                _print_json({"ok": False, **exc.to_payload()})
            else:
                console.print(f"[error]{escape(exc.message)}[/error]")
            raise typer.Exit(code=1) from exc
        payload = {"ok": True, **dict(result.payload)}
        if render is not None and not json_output:
            render(payload)
        else:
            _emit_payload(payload, json_output=json_output)


async def _run_read_command(
    project: Optional[str],
    *,
    action: str,
    request: dict[str, Any],
    operation: Any,
    json_output: bool,
) -> None:
    """Serve a read-only view from the live console, or from a local engine.

    Building an engine per read command is both slow and a second writer
    against the console's store; the console answers the same service call.
    """
    if not ui_bridge.bridge_disabled():
        bridged = await ui_bridge.read_via_console(action=action, payload=request)
        if bridged is not None and bridged.get("ok"):
            payload = {k: v for k, v in bridged.items() if k not in {"ok", "action"}}
            _emit_payload({"ok": True, **payload}, json_output=json_output)
            return
    await _run_service_command(project, operation, json_output=json_output)


async def _run_session_control_command(
    *,
    action: str,
    target: str,
    project: str | None,
    json_output: bool,
    operation: Any,
) -> None:
    """Use the live console for stop/resume, falling back only when absent.

    A resolution-only negative acknowledgement such as ``task_not_found`` is
    safe to fall back from because the console did not mutate anything.
    Transport loss or any other rejection is surfaced directly and never
    starts a second engine.
    """
    project_id = str(project or "default").strip() or "default"
    bridged = await ui_bridge.session_control_via_console(
        action=action,
        project_id=project_id,
        task_id=str(target or "").strip(),
    )
    fallback_errors = {
        "task_not_found",
        "session_not_found",
        "missing_runtime_session_id",
        "missing_checkpoint_id",
        "company_runtime_identity_mismatch",
    }
    if bridged is None or (bridged.status == "failed" and bridged.error in fallback_errors):
        await _run_service_command(project, operation, json_output=json_output)
        return
    payload = {
        "ok": bridged.status not in {"failed", "connection_lost"},
        "project_id": project_id,
        "task_id": bridged.task_id,
        "status": bridged.status,
        "console_url": bridged.console_url,
    }
    if bridged.error:
        payload["error"] = bridged.error
    if json_output:
        _print_json(payload)
    else:
        if bridged.error:
            console.print(f"[error]{escape(bridged.error)}[/error]")
        else:
            console.print(f"[success]{action}: {bridged.status}[/success]")
    if not payload["ok"]:
        raise typer.Exit(code=1)

def _load_structured_payload(*, payload: str | None = None, file_path: str | None = None) -> Any:
    raw = ""
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8")
    elif payload:
        raw = payload
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import yaml

        return yaml.safe_load(raw) or {}


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _exec_event_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return _json_safe(value)


def _print_exec_event(
    event_state: dict[str, Any],
    event_type: str,
    *,
    project_id: str,
    task_id: str = "",
    session_id: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    event_state["seq"] = int(event_state.get("seq", 0) or 0) + 1
    row = {
        "type": event_type,
        "seq": event_state["seq"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "task_id": task_id,
        "session_id": session_id,
        "payload": payload or {},
    }
    sys.stdout.write(json.dumps(row, ensure_ascii=False, default=_json_safe) + "\n")
    sys.stdout.flush()


def _exec_title(prompt: str) -> str:
    for line in str(prompt or "").splitlines():
        title = line.strip()
        if title:
            return title[:120]
    return "Exec Session"


async def _task_id_for_session(services: Any, *, project_id: str, session_id: str) -> str:
    engine = await services.context.engine_for_project(project_id)
    store = getattr(engine, "store", None)
    if not store or not session_id:
        return ""
    tasks = await store.get_tasks(project_id=project_id) if hasattr(store, "get_tasks") else []
    for task in tasks:
        if str(getattr(task, "session_id", "") or "") == session_id:
            return str(getattr(task, "id", "") or "")
    return ""


async def _resolve_exec_session(
    services: Any,
    *,
    project_id: str,
    prompt: str,
    mode: str,
    company_profile: str,
    preferred_agent: str | None,
    org_id: str | None,
    session_id: str | None,
    resume: bool,
    native_approval_level: NativeApprovalLevel | None,
) -> dict[str, Any]:
    from ampuly.core.service_models import ServiceError

    normalized_mode = str(mode or "task").strip().lower()
    if normalized_mode == "custom":
        normalized_mode = "org"
    if normalized_mode not in {"task", "company", "org"}:
        raise ServiceError("invalid_mode", "mode must be task, company, or org", {"mode": mode})

    if session_id:
        task_id = await _task_id_for_session(services, project_id=project_id, session_id=session_id)
        if not task_id:
            raise ServiceError("session_not_task_backed", "Session is not linked to a task-backed CLI/UI session", {"session_id": session_id})
        detail = await services.session.detail(project_id=project_id, task_id=task_id, session_id=session_id, limit=1)
        payload = {**detail.payload, "task_id": task_id, "session_id": session_id, "restored": True}
        if native_approval_level is not None:
            updated = await services.session.update_config(
                project_id=project_id,
                task_id=task_id,
                native_approval_level=native_approval_level,
            )
            payload.update(dict(updated.payload))
        return payload

    if resume:
        starting = await services.session.resolve_starting_session(project_id=project_id)
        payload = dict(starting.payload)
        restored_session_id = str(payload.get("session_id", "") or "")
        restored_task_id = str(payload.get("task_id", "") or "")
        if restored_session_id and not restored_task_id:
            restored_task_id = await _task_id_for_session(services, project_id=project_id, session_id=restored_session_id)
        if not restored_task_id:
            raise ServiceError("session_not_task_backed", "Latest session is not task-backed; create a new exec session without --resume", {"session_id": restored_session_id})
        payload["task_id"] = restored_task_id
        payload["session_id"] = restored_session_id
        payload["restored"] = True
        if native_approval_level is not None:
            updated = await services.session.update_config(
                project_id=project_id,
                task_id=restored_task_id,
                native_approval_level=native_approval_level,
            )
            payload.update(dict(updated.payload))
        return payload

    created = await services.session.create(
        project_id=project_id,
        title=_exec_title(prompt),
        exec_mode=normalized_mode,
        company_profile="custom" if normalized_mode == "org" else company_profile,
        preferred_agent=preferred_agent,
        org_id=org_id,
        interface="cli_exec",
        native_approval_level=native_approval_level,
    )
    return dict(created.payload)


def _cli_is_zh() -> bool:
    try:
        from ampuly.i18n_runtime import is_zh

        return bool(is_zh())
    except Exception:
        return False


async def _exec_via_console(
    *,
    prompt: str,
    project: str | None,
    mode: str,
    company_profile: str,
    preferred_agent: str | None,
    org_id: str | None,
    session_id: str | None,
    json_output: bool,
    stream_json: bool,
    no_markdown: bool,
    native_approval_level: str | None,
) -> "ui_bridge.BridgeResult | None":
    """Relay one prompt through the running console; None when none is up."""
    project_id = str(project or "default").strip() or "default"
    zh = _cli_is_zh()
    console_url = ui_bridge.console_http_url()

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        if stream_json:
            _print_exec_event(
                {"seq": 0},
                f"console_{kind}",
                project_id=project_id,
                task_id=str(payload.get("task_id", "") or ""),
                payload=payload,
            )
            return
        if json_output:
            return
        if kind == "waiting":
            console.print(
                f"[warning]{'等你在控制台拍板' if zh else 'Waiting for your decision in the console'}: {console_url}[/warning]"
            )
        elif kind == "message":
            content = str(payload.get("content", "") or "").strip()
            if content:
                console.print(content if no_markdown else Markdown(content))
        elif kind == "status":
            status = str(payload.get("status", "") or "")
            if status == "sent":
                console.print(
                    f"[dim]{'已交给运行中的控制台' if zh else 'Handed to the running console'} {console_url} "
                    f"({'任务' if zh else 'task'} {payload.get('task_id', '')})[/dim]"
                )

    result = await ui_bridge.exec_via_console(
        prompt=prompt,
        project_id=project_id,
        mode=mode,
        company_profile=company_profile,
        preferred_agent=preferred_agent,
        org_id=org_id,
        session_id=session_id,
        native_approval_level=native_approval_level,
        on_event=on_event,
    )
    if result is None:
        return None
    if json_output:
        _print_json({
            "task_id": result.task_id,
            "status": result.status,
            "waited_on_human": result.waited_on_human,
            "console_url": result.console_url,
            "error": result.error,
            "result": result.final_text,
        })
    elif not stream_json:
        if result.error:
            console.print(f"[warning]{'控制台连接中断，请回到控制台继续处理' if zh else 'Console connection lost; return to the console to continue'}: {result.console_url}[/warning]")
        console.print(f"[dim]{'状态' if zh else 'status'}: {result.status}[/dim]")
    return result


async def _exec_message(
    *,
    config: AmpulyConfig,
    prompt: str,
    project: str | None,
    mode: str,
    company_profile: str,
    preferred_agent: str | None,
    org_id: str | None,
    session_id: str | None,
    resume: bool,
    json_output: bool,
    stream_json: bool,
    no_markdown: bool,
    native_approval_level: NativeApprovalLevel | None,
) -> None:
    from ampuly.core.service_models import ServiceError
    from ampuly.plugins.office_ui.services.factory import OfficeServiceFactory

    project_id = str(project or "default").strip() or "default"
    event_state: dict[str, Any] = {"seq": 0, "task_id": "", "session_id": ""}

    async def on_progress(*args: Any, **kwargs: Any) -> None:
        if not stream_json:
            return
        _print_exec_event(
            event_state,
            "runtime_update",
            project_id=project_id,
            task_id=str(event_state.get("task_id", "") or ""),
            session_id=str(event_state.get("session_id", "") or ""),
            payload={"args": [_exec_event_payload(arg) for arg in args], "kwargs": kwargs},
        )

    async def on_runtime_event(event: Any) -> None:
        if not stream_json:
            return
        _print_exec_event(
            event_state,
            "runtime_update",
            project_id=project_id,
            task_id=str(event_state.get("task_id", "") or ""),
            session_id=str(event_state.get("session_id", "") or ""),
            payload=_exec_event_payload(event),
        )

    try:
        async with OfficeServiceFactory(
            config=config,
            project_id=project_id,
            on_progress=on_progress,
            on_runtime_event=on_runtime_event,
            on_escalation=_escalation_callback(),
        ) as services:
            target = await _resolve_exec_session(
                services,
                project_id=project_id,
                prompt=prompt,
                mode=mode,
                company_profile=company_profile,
                preferred_agent=preferred_agent,
                org_id=org_id,
                session_id=session_id,
                resume=resume,
                native_approval_level=native_approval_level,
            )
            task_id = str(target.get("task_id", "") or "")
            resolved_session_id = str(target.get("session_id", "") or "")
            event_state["task_id"] = task_id
            event_state["session_id"] = resolved_session_id
            if stream_json:
                _print_exec_event(
                    event_state,
                    "session_created" if not target.get("restored") else "session_resumed",
                    project_id=project_id,
                    task_id=task_id,
                    session_id=resolved_session_id,
                    payload=target,
                )

            response = ""
            if prompt:
                normalized_mode = str(mode or "task").strip().lower()
                send_mode = "company" if normalized_mode in {"company", "org", "custom"} else "task"
                send_profile = "custom" if normalized_mode in {"org", "custom"} else company_profile
                sent = await services.session.send(
                    project_id=project_id,
                    task_id=task_id,
                    content=prompt,
                    mode=send_mode,
                    company_profile=send_profile,
                    preferred_agent=preferred_agent,
                )
                response = str(sent.payload.get("response", "") or "")
                if stream_json:
                    _print_exec_event(
                        event_state,
                        "message",
                        project_id=project_id,
                        task_id=task_id,
                        session_id=resolved_session_id,
                        payload={"role": "assistant", "content": response},
                    )

            final_payload = {
                "ok": True,
                "project_id": project_id,
                "task_id": task_id,
                "session_id": resolved_session_id,
                "mode": mode,
                "company_profile": company_profile,
                "response": response,
            }
            if stream_json:
                _print_exec_event(
                    event_state,
                    "final",
                    project_id=project_id,
                    task_id=task_id,
                    session_id=resolved_session_id,
                    payload=final_payload,
                )
                return
            if json_output:
                _print_json(final_payload)
                return
            if response:
                _print_response(response, no_markdown=no_markdown)
            else:
                _emit_payload(final_payload)
    except ServiceError as exc:
        payload = {"ok": False, **exc.to_payload()}
        if stream_json:
            _print_exec_event(event_state, "error", project_id=project_id, payload=payload)
        elif json_output:
            _print_json(payload)
        else:
            console.print(f"[error]{escape(exc.message)}[/error]")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        payload = {"ok": False, "code": "invalid_argument", "error": str(exc)}
        if stream_json:
            _print_exec_event(event_state, "error", project_id=project_id, payload=payload)
        elif json_output:
            _print_json(payload)
        else:
            console.print(f"[error]{escape(str(exc))}[/error]")
        raise typer.Exit(code=2) from exc


trust_app = typer.Typer(help="Manage trust for project-local SuperOnes configuration")
app.add_typer(trust_app, name="trust")


@trust_app.command("add")
def trust_add(
    path: Path = typer.Argument(Path("."), help="Workspace, .ampuly, or .ampuly/config path"),
) -> None:
    """Trust a workspace after reviewing its project-local Ampuly config."""

    from ampuly.core.workspace_trust import WorkspaceTrustStore, workspace_from_user_path

    try:
        workspace = workspace_from_user_path(path)
    except ValueError as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        raise typer.Exit(code=2) from exc
    config_dir = workspace / ".ampuly" / "config"
    try:
        config = AmpulyConfig.load(config_dir, trusted_source=True)
    except Exception as exc:
        console.print(f"[error]Unable to load reviewed configuration: {escape(str(exc))}[/error]")
        raise typer.Exit(code=2) from exc
    WorkspaceTrustStore().trust(workspace, config_dir, config)
    console.print(f"[success]已信任项目：[/success]{workspace}")


@trust_app.command("remove")
def trust_remove(
    path: Path = typer.Argument(Path("."), help="Trusted workspace path"),
) -> None:
    """Remove a workspace trust decision."""

    from ampuly.core.workspace_trust import (
        WorkspaceTrustStore,
        canonical_workspace,
        workspace_from_user_path,
    )

    try:
        workspace = workspace_from_user_path(path)
    except ValueError:
        workspace = canonical_workspace(path)
    if WorkspaceTrustStore().untrust(workspace):
        console.print(f"[success]Removed workspace trust:[/success] {workspace}")
    else:
        console.print(f"[warning]Workspace was not trusted:[/warning] {workspace}")


@trust_app.command("list")
def trust_list() -> None:
    """List trusted workspaces and the user-owned trust store location."""

    from ampuly.core.workspace_trust import WorkspaceTrustStore

    store = WorkspaceTrustStore()
    trusted = store.list_trusted()
    console.print(f"[info]Trust store:[/info] {store.path}")
    if not trusted:
        console.print("[info]No trusted workspaces.[/info]")
        return
    for workspace in trusted:
        console.print(f"  - {workspace}")


project_app = typer.Typer(help="Manage SuperOnes projects")
app.add_typer(project_app, name="project")


@project_app.command("list")
def project_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List projects."""
    asyncio.run(_run_read_command(
        project,
        action="project_list_read",
        request={"project_id": project or ""},
        operation=lambda svc: svc.project.list(active_project_id=project),
        json_output=json_output,
    ))


@project_app.command("show")
def project_show(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Show project index payload."""
    target = project or "default"
    asyncio.run(_run_read_command(
        project,
        action="project_show_read",
        request={"project_id": target, "target": target},
        operation=lambda svc: svc.project.project_index(target, include_snapshot=False),
        json_output=json_output,
    ))


@project_app.command("create")
def project_create(
    project_id: str = typer.Argument(..., help="Project ID to create"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Create a project."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.create(project_id, active_project_id=project), json_output=json_output))


@project_app.command("switch")
def project_switch(
    project_id: str = typer.Argument(..., help="Project ID to prepare/switch"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Current project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Prepare and validate switching to a project."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.switch(project_id, include_snapshot=False), json_output=json_output))


@project_app.command("rename")
def project_rename(
    old_project_id: str = typer.Argument(..., help="Existing project ID"),
    new_project_id: str = typer.Argument(..., help="New project ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Rename a project id and move its persisted project data."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.rename(old_project_id, new_project_id), json_output=json_output))


@project_app.command("delete")
def project_delete(
    project_id: str = typer.Argument(..., help="Project ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deletion"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Delete a project and its persisted UI/runtime data."""
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.project.delete(project_id), json_output=json_output))


session_app = typer.Typer(help="Manage SuperOnes sessions")
app.add_typer(session_app, name="session")


@session_app.command("list")
def session_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_read_command(
        project,
        action="session_list",
        request={"project_id": project or "default", "limit": limit},
        operation=lambda svc: svc.session.list(project_id=project or "default", limit=limit),
        json_output=json_output,
    ))


@session_app.command("create")
def session_create(
    title: str = typer.Argument("New Chat", help="Session title"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    mode: str = typer.Option("task", "--mode", help="task, company, or org"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="corporate or custom"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred task-mode agent"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id", help="Saved org id"),
    approval_level: Optional[str] = typer.Option(None, "--approval-level", help="SuperOnes Native permissions"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    native_level = _parse_native_approval_level(approval_level) if approval_level is not None else None
    asyncio.run(_run_service_command(project, lambda svc: svc.session.create(project_id=project or "default", title=title, exec_mode=mode, company_profile=company_profile, preferred_agent=agent, org_id=org_id, native_approval_level=native_level, interface="cli"), json_output=json_output))


@session_app.command("show")
def session_show(
    target: str = typer.Argument(..., help="Task ID or session ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    limit: int = typer.Option(200, "--limit", "-n", help="Transcript limit"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_read_command(
        project,
        action="session_detail_read",
        request={"project_id": project or "default", "target": target, "limit": limit},
        operation=lambda svc: svc.session.detail(project_id=project or "default", task_id=target, session_id=target, limit=limit),
        json_output=json_output,
    ))


@session_app.command("config")
def session_config(
    task_id: str = typer.Argument(..., help="Task ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    mode: Optional[str] = typer.Option(None, "--mode", help="task, company, or org"),
    company_profile: Optional[str] = typer.Option(None, "--company-profile", help="corporate or custom"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred task-mode agent"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id", help="Saved org id"),
    approval_level: Optional[str] = typer.Option(None, "--approval-level", help="SuperOnes Native permissions"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    native_level = _parse_native_approval_level(approval_level) if approval_level is not None else None
    asyncio.run(_run_service_command(project, lambda svc: svc.session.update_config(project_id=project or "default", task_id=task_id, exec_mode=mode, company_profile=company_profile, preferred_agent=agent, org_id=org_id, native_approval_level=native_level), json_output=json_output))


@session_app.command("send")
def session_send(
    task_id: str = typer.Argument(..., help="Task ID"),
    message: str = typer.Argument(..., help="Message to send"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    mode: str = typer.Option("task", "--mode", help="task or company"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="Company profile"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred agent"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.send(project_id=project or "default", task_id=task_id, content=message, mode=mode, company_profile=company_profile, preferred_agent=agent), json_output=json_output))


@session_app.command("rename")
def session_rename(
    target: str = typer.Argument(..., help="Task ID or session ID"),
    title: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.rename(project_id=project or "default", task_id=target, session_id=target, title=title), json_output=json_output))


@session_app.command("delete")
def session_delete(
    task_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.session.delete(project_id=project or "default", task_id=task_id), json_output=json_output))


@session_app.command("stop")
def session_stop(target: str = typer.Argument(..., help="Task ID or session ID"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_session_control_command(
        action="session_stop",
        target=target,
        project=project,
        json_output=json_output,
        operation=lambda svc: svc.session.stop(project_id=project or "default", target=target),
    ))


@session_app.command("continue")
def session_continue(
    target: str = typer.Argument(..., help="Task ID or session ID"),
    message: Optional[str] = typer.Argument(None, help="Optional resume instruction"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.continue_run(project_id=project or "default", target=target, content=message or ""), json_output=json_output))


@session_app.command("resume")
def session_resume(target: str = typer.Argument(..., help="Task ID or session ID"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_session_control_command(
        action="session_resume",
        target=target,
        project=project,
        json_output=json_output,
        operation=lambda svc: svc.session.resume(project_id=project or "default", target=target),
    ))


@session_app.command("complete")
def session_complete(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.complete(project_id=project or "default", task_id=task_id), json_output=json_output))


mode_app = typer.Typer(help="Manage default execution mode")
app.add_typer(mode_app, name="mode")


@mode_app.command("show")
def mode_show(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.mode_show(), json_output=json_output))


@mode_app.command("set")
def mode_set(
    mode: str = typer.Argument(..., help="task, company, or org"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    profile: str = typer.Option("corporate", "--profile", "--company-profile"),
    agent: Optional[str] = typer.Option(None, "--agent"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.mode_set(mode=mode, profile=profile, preferred_agent=agent, org_id=org_id), json_output=json_output))


kanban_app = typer.Typer(help="Manage kanban views and tasks")
kanban_task_app = typer.Typer(help="Manage kanban tasks")
kanban_app.add_typer(kanban_task_app, name="task")
app.add_typer(kanban_app, name="kanban")


@kanban_task_app.command("create")
def kanban_task_create(title: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), description: str = typer.Option("", "--description", "-d"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.create_task(project_id=project or "default", title=title, description=description), json_output=json_output))


@kanban_task_app.command("update")
def kanban_task_update(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), title: Optional[str] = typer.Option(None, "--title"), description: Optional[str] = typer.Option(None, "--description"), json_output: bool = typer.Option(False, "--json")):
    updates = {k: v for k, v in {"title": title, "description": description}.items() if v is not None}
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.update_task(project_id=project or "default", task_id=task_id, updates=updates), json_output=json_output))


@kanban_task_app.command("move")
def kanban_task_move(task_id: str = typer.Argument(...), column: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.move_task(project_id=project or "default", task_id=task_id, column_id=column), json_output=json_output))


@kanban_task_app.command("delete")
def kanban_task_delete(task_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.delete_task(project_id=project or "default", task_id=task_id), json_output=json_output))


@kanban_task_app.command("assign")
def kanban_task_assign(task_id: str = typer.Argument(...), agent_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.assign(project_id=project or "default", task_id=task_id, agent_id=agent_id), json_output=json_output))


@kanban_task_app.command("status")
def kanban_task_status(task_id: str = typer.Argument(...), status: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.status(project_id=project or "default", task_id=task_id, status=status), json_output=json_output))


@kanban_app.command("view")
def kanban_view(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.project.project_index(project or "default", include_snapshot=False), json_output=json_output))


agent_app = typer.Typer(help="Manage UI agents")
app.add_typer(agent_app, name="agent")


@agent_app.command("list")
def agent_list(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_read_command(
        project,
        action="agent_list_read",
        request={"project_id": project or ""},
        operation=lambda svc: svc.agent.list(),
        json_output=json_output,
    ))


@agent_app.command("create")
def agent_create(name: str = typer.Argument(...), role_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), description: str = typer.Option("", "--description"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.create(name=name, role_id=role_id, office_id=office_id, description=description), json_output=json_output))


@agent_app.command("create-from-template")
def agent_create_from_template(template_id: str = typer.Argument(...), role_id: Optional[str] = typer.Option(None, "--role", "--role-id"), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.create_from_template(template_id=template_id, role_id=role_id or template_id, office_id=office_id), json_output=json_output))


@agent_app.command("import-employee")
def agent_import_employee(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.import_employee(employee_id=employee_id, office_id=office_id), json_output=json_output))


@agent_app.command("detail")
def agent_detail(agent_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_read_command(
        project,
        action="agent_detail_read",
        request={"project_id": project or "default", "target": agent_id},
        operation=lambda svc: svc.agent.detail(project_id=project or "default", agent_id=agent_id),
        json_output=json_output,
    ))


@agent_app.command("delete")
def agent_delete(agent_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.delete(agent_id), json_output=json_output))


@agent_app.command("move")
def agent_move(agent_id: str = typer.Argument(...), office_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), seat_zone: Optional[str] = typer.Option(None, "--seat-zone"), desk_id: Optional[str] = typer.Option(None, "--desk"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.move(agent_id=agent_id, office_id=office_id, seat_zone=seat_zone, desk_id=desk_id), json_output=json_output))


agents_app = typer.Typer(help="Inspect external execution agents")
app.add_typer(agents_app, name="agents")


@agents_app.command("list")
def external_agents_list(json_output: bool = typer.Option(False, "--json")):
    from ampuly.layer3_agent.adapters.registry import ADAPTER_CLASSES

    config = _get_config()

    async def collect_profiles() -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for name, adapter_cls in ADAPTER_CLASSES.items():
            adapter = adapter_cls(config=config.agents.agents.get(name))
            profile = adapter.describe()
            profile["available"] = await adapter.is_available()
            profiles.append(profile)
        return profiles

    profiles = asyncio.run(collect_profiles())
    if json_output:
        console.print_json(data={"agents": profiles})
        return
    table = Table(title="External Agents")
    table.add_column("Agent")
    table.add_column("Available")
    table.add_column("Execution unit")
    table.add_column("Task")
    table.add_column("Company")
    table.add_column("Transport / mode")
    for profile in profiles:
        capabilities = dict(profile.get("capabilities", {}) or {})
        table.add_row(
            execution_agent_label(profile.get("agent")),
            "yes" if profile.get("available") else "no",
            str(profile.get("execution_unit_kind") or "external_agent"),
            "yes" if capabilities.get("task_mode") else "no",
            "yes" if capabilities.get("company_mode") else "no",
            f"{profile.get('transport', 'cli')} / {profile.get('provider_mode') or profile.get('run_mode', '')}",
        )
    console.print(table)


@agents_app.command("preflight")
def external_agents_preflight(
    agent: Optional[str] = typer.Argument(None),
    project: str = typer.Option("default", "--project", "-p"),
    probe_commands: bool = typer.Option(True, "--probe-commands/--no-probe-commands"),
    json_output: bool = typer.Option(False, "--json"),
):
    from ampuly.layer3_agent.preflight import run_external_agent_preflight

    results = run_external_agent_preflight(
        _get_config(),
        project_id=project,
        probe_commands=probe_commands,
        prepare_surfaces=True,
    )
    if agent:
        results = [item for item in results if item.agent == agent]
        if not results:
            console.print(f"[error]Unknown external agent: {agent}[/error]")
            raise typer.Exit(code=2)
    if json_output:
        console.print_json(data={"agents": [item.as_dict() for item in results]})
        return
    _render_external_agent_preflight_table(results)


org_app = typer.Typer(help="Manage organization configuration")
app.add_typer(org_app, name="org")


@org_app.command("info")
def org_info(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.info(), json_output=json_output))


@org_app.command("export")
def org_export(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(True, "--json/--no-json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.export_config(), json_output=json_output))


@org_app.command("import")
def org_import(
    path: str = typer.Argument(..., help="YAML or JSON org config payload"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    raw = Path(path).read_text(encoding="utf-8")
    asyncio.run(_run_service_command(project, lambda svc: svc.org.import_config(raw, dry_run=dry_run), json_output=json_output))


@org_app.command("team-bind")
def org_team_bind(
    role_id: str = typer.Option(..., "--role", help="Boundary role id, for example cto or cmo"),
    organization_id: Optional[str] = typer.Option(None, "--org", help="Expected active organization id"),
    binding_id: str = typer.Option("", "--binding-id", help="Stable binding id (auto-generated when omitted)"),
    agent: str = typer.Option("jiuwenswarm", "--agent"),
    scope: str = typer.Option("subtree", "--scope", help="role or subtree"),
    collapse_subtree: bool = typer.Option(True, "--collapse-subtree/--no-collapse-subtree"),
    provider_mode: str = typer.Option("team", "--provider-mode"),
    session_scope: str = typer.Option("company_run", "--session-scope"),
    max_inflight: int = typer.Option(1, "--max-inflight", min=1),
    failure_policy: str = typer.Option("fail_closed", "--failure-policy", help="fail_closed or fallback_native"),
    review_owner_role_id: str = typer.Option("", "--review-owner", help="Reviewer role; defaults to the boundary manager"),
    artifact_isolation: str = typer.Option("validated_workspace", "--artifact-isolation"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    payload = {
        "organization_id": organization_id,
        "binding_id": binding_id,
        "boundary_role_id": role_id,
        "external_agent": agent,
        "scope": scope,
        "collapse_subtree": collapse_subtree,
        "provider_mode": provider_mode,
        "session_scope": session_scope,
        "max_inflight": max_inflight,
        "failure_policy": failure_policy,
        "review_owner_role_id": review_owner_role_id,
        "artifact_isolation": artifact_isolation,
    }
    asyncio.run(
        _run_service_command(
            project,
            lambda svc: svc.org.bind_external_team(payload),
            json_output=json_output,
        )
    )


@org_app.command("team-bindings")
def org_team_bindings(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(
        _run_service_command(
            project,
            lambda svc: svc.org.list_external_team_bindings(),
            json_output=json_output,
        )
    )


@org_app.command("team-unbind")
def org_team_unbind(
    role_or_binding: str = typer.Option(..., "--role", "--binding"),
    organization_id: Optional[str] = typer.Option(None, "--org", help="Expected active organization id"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(
        _run_service_command(
            project,
            lambda svc: svc.org.unbind_external_team(
                role_or_binding,
                organization_id=organization_id,
            ),
            json_output=json_output,
        )
    )


org_saved_app = typer.Typer(help="Manage saved org architectures")
org_app.add_typer(org_saved_app, name="saved")


@org_saved_app.command("list")
def org_saved_list(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_list(), json_output=json_output))


@org_saved_app.command("save")
def org_saved_save(name: str = typer.Argument(...), overwrite: bool = typer.Option(False, "--overwrite"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_save_as(name, overwrite=overwrite), json_output=json_output))


@org_saved_app.command("load")
def org_saved_load(name: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_load(name), json_output=json_output))


@org_saved_app.command("delete")
def org_saved_delete(name: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_delete(name), json_output=json_output))


org_role_app = typer.Typer(help="Manage organization roles")
org_app.add_typer(org_role_app, name="role")


@org_role_app.command("add")
def org_role_add(role_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), name: Optional[str] = typer.Option(None, "--name"), responsibility: str = typer.Option("", "--responsibility"), reports_to: str = typer.Option("owner", "--reports-to"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.add_role({"role_id": role_id, "name": name or role_id, "responsibility": responsibility, "reports_to": reports_to}), json_output=json_output))


@org_role_app.command("update")
def org_role_update(
    role_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    name: Optional[str] = typer.Option(None, "--name"),
    responsibility: Optional[str] = typer.Option(None, "--responsibility"),
    reports_to: Optional[str] = typer.Option(None, "--reports-to"),
    can_spawn: Optional[str] = typer.Option(None, "--can-spawn", help="Comma-separated role ids"),
    tools: Optional[str] = typer.Option(None, "--tools", help="Comma-separated tool names"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred external agent"),
    json_output: bool = typer.Option(False, "--json"),
):
    updates = {
        key: value
        for key, value in {
            "name": name,
            "responsibility": responsibility,
            "reports_to": reports_to,
            "preferred_external_agent": agent,
        }.items()
        if value is not None
    }
    if can_spawn is not None:
        updates["can_spawn"] = _split_csv(can_spawn)
    if tools is not None:
        updates["tools"] = _split_csv(tools)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.update_role(role_id, updates), json_output=json_output))


@org_role_app.command("bulk-add")
def org_role_bulk_add(path: str = typer.Argument(..., help="JSON/YAML list of role objects or object with roles"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    loaded = _load_structured_payload(file_path=path)
    roles = loaded.get("roles", []) if isinstance(loaded, dict) else loaded if isinstance(loaded, list) else []
    asyncio.run(_run_service_command(project, lambda svc: svc.org.bulk_add_roles(list(roles or [])), json_output=json_output))


@org_role_app.command("delete")
def org_role_delete(role_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.delete_role(role_id), json_output=json_output))


org_policy_app = typer.Typer(help="Manage organization runtime policy")
org_app.add_typer(org_policy_app, name="policy")


@org_policy_app.command("update")
def org_policy_update(payload: Optional[str] = typer.Option(None, "--payload", help="JSON/YAML object"), file_path: Optional[str] = typer.Option(None, "--file", "-f"), profile: str = typer.Option("custom", "--profile"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    data = _load_structured_payload(payload=payload, file_path=file_path)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.update_runtime_policy(data, profile=profile), json_output=json_output))


org_strategy_app = typer.Typer(help="Manage organization strategy")
org_app.add_typer(org_strategy_app, name="strategy")


@org_strategy_app.command("update")
def org_strategy_update(final_decider_role_id: Optional[str] = typer.Option(None, "--final-decider", "--final-decider-role-id"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.update_org_strategy(final_decider_role_id=final_decider_role_id), json_output=json_output))


@org_app.command("reset")
def org_reset(yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.reset_architecture(), json_output=json_output))


runtime_app = typer.Typer(help="Inspect and control runtime state")
app.add_typer(runtime_app, name="runtime")


@runtime_app.command("status")
def runtime_status(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.status(project_id=project or "default"), json_output=json_output))


@runtime_app.command("checkpoints")
def runtime_checkpoints(project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(50, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.checkpoints(project_id=project or "default", limit=limit), json_output=json_output))


@runtime_app.command("logs")
def runtime_logs(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.logs(project_id=project or "default", task_id=task_id, limit=limit), json_output=json_output))


@runtime_app.command("run")
def runtime_run(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.run_task(project_id=project or "default", task_id=task_id), json_output=json_output))


comms_app = typer.Typer(help="Inspect company-mode comms")
app.add_typer(comms_app, name="comms")


@comms_app.command("state")
def comms_state(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.comms.state(project_id=project or "default", task_id=task_id), json_output=json_output))


@comms_app.command("read")
def comms_read(task_id: str = typer.Argument(...), path: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.comms.read(project_id=project or "default", task_id=task_id, path=path), json_output=json_output))


work_item_app = typer.Typer(help="Inspect company-mode work items")
app.add_typer(work_item_app, name="work-item")


@work_item_app.command("list")
def work_item_list(project: Optional[str] = typer.Option(None, "--project", "-p"), role_id: Optional[str] = typer.Option(None, "--role", "--role-id"), status: Optional[str] = typer.Option(None, "--status"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.list(project_id=project or "default", role_id=role_id, status=status, limit=limit), json_output=json_output))


@work_item_app.command("show")
def work_item_show(work_item_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.show(project_id=project or "default", work_item_id=work_item_id, limit=limit), json_output=json_output))


@work_item_app.command("logs")
def work_item_logs(work_item_id: str = typer.Argument(""), role_id: Optional[str] = typer.Option(None, "--role", "--role-id"), project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.logs(project_id=project or "default", work_item_id=work_item_id, role_id=role_id or "", limit=limit), json_output=json_output))


@work_item_app.command("role-status")
def work_item_role_status(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.status_by_role(project_id=project or "default"), json_output=json_output))


channels_app = typer.Typer(help="Manage external messaging channels")
app.add_typer(channels_app, name="channels")

talent_app = typer.Typer(help="Manage recruitable talent templates and employees")
app.add_typer(talent_app, name="talent")


@talent_app.command("list")
def talent_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List imported talent templates."""
    config = _get_config()
    market = TalentMarket(get_ampuly_home(), config)
    templates = market.list_templates()
    if json_output:
        _emit_payload({"templates": templates}, json_output=True)
        return
    if not templates:
        console.print("[warning]No imported talent templates found.[/warning]")
        return
    console.print(f"[bold]Talent Templates ({len(templates)}):[/bold]")
    for template in templates:
        domains = ", ".join(template.domains[:4]) if template.domains else "general"
        console.print(
            f"  - [bold]{template.id}[/bold] :: {template.name} "
            f"[{template.category}] domains={domains}"
        )


@talent_app.command("employees")
def talent_employees(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List hired employees."""
    config = _get_config()
    market = TalentMarket(get_ampuly_home(), config)
    employees = market.list_employees()
    if json_output:
        _emit_payload({"employees": employees}, json_output=True)
        return
    if not employees:
        console.print("[warning]No employees hired yet.[/warning]")
        return
    console.print(f"[bold]Employees ({len(employees)}):[/bold]")
    for employee in employees:
        domains = ", ".join(employee.domains[:4]) if employee.domains else "general"
        console.print(
            f"  - [bold]{employee.employee_id}[/bold] :: {employee.name} "
            f"role={employee.role_id} domains={domains}"
        )


def _run_headless_talent_command(
    project: str | None,
    instance_id: str,
    instruction: str,
    *,
    json_output: bool,
    source_handoff_id: str = "",
    rework_of_handoff_id: str = "",
) -> None:
    """Run an employee task on a core engine. Does not open the console adapter."""
    from ampuly.core.service_models import ServiceError
    from ampuly.headless.employee_run import run_headless_employee_task
    from ampuly.plugins.office_ui.employee_direct_task import (
        EmployeeDirectRefused,
        run_employee_direct_task,
    )
    from ampuly.plugins.office_ui.employee_runtime_host import build_employee_runtime_host

    async def _go() -> dict[str, Any]:
        try:
            return await run_headless_employee_task(
                config=_get_config(),
                project_id=project,
                instance_id=instance_id,
                instruction=instruction,
                actor="owner",
                host_factory=build_employee_runtime_host,
                direct_runner=run_employee_direct_task,
                source_handoff_id=source_handoff_id,
                rework_of_handoff_id=rework_of_handoff_id,
            )
        except EmployeeDirectRefused as exc:
            raise ServiceError(
                "employee_direct_refused", str(exc.code),
                {"code": exc.code, "gaps": list(exc.gaps)},
            ) from exc

    try:
        result = asyncio.run(_go())
    except ServiceError as exc:
        if json_output:
            _print_json({"ok": False, **exc.to_payload()})
        else:
            console.print(f"[error]{escape(exc.message)}[/error]")
        raise typer.Exit(code=1) from exc
    _emit_payload({"ok": True, **result}, json_output=json_output)


@talent_app.command("run")
def talent_run(
    instance_id: str = typer.Argument(..., help="Adopted employee instance id (ei-...)"),
    instruction: str = typer.Argument(..., help="Task instruction for this employee"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Run one adopted employee on an independent task (no order, flow or node)."""
    _run_headless_talent_command(
        project, instance_id, instruction, json_output=json_output,
    )


@talent_app.command("handoff")
def talent_handoff(
    handoff_id: str = typer.Argument(..., help="Handoff id from a completed employee run"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Read a handoff and the fixed artifacts it names (read-only)."""
    from ampuly.core.service_models import ServiceError, ServiceResult

    async def operation(services):
        engine = services.context.active_engine
        store = getattr(engine, "store", None)
        record = await store.get_handoff_record(handoff_id) if store is not None else None
        if record is None:
            raise ServiceError("handoff_not_found", "handoff_not_found", {"handoff_id": handoff_id})
        artifacts = []
        getter = getattr(store, "get_artifacts", None)
        if callable(getter):
            rows = await getter(record.project_id, limit=50)
            artifacts = [
                {"artifact_id": row.artifact_id, "name": row.name, "task_id": row.task_id,
                 "sha256": (row.details or {}).get("content_sha256"),
                 "bytes": (row.details or {}).get("bytes")}
                for row in rows if row.task_id == record.task_id
            ]
        return ServiceResult({
            "handoff": {"handoff_id": record.handoff_id, "project_id": record.project_id,
                        "task_id": record.task_id, "from_role": record.from_role,
                        "to_role": record.to_role, "status": record.status,
                        "requires_ack": record.requires_ack, "summary": record.summary,
                        "payload": dict(record.payload or {})},
            "artifacts": artifacts,
        })

    asyncio.run(_run_service_command(project, operation, json_output=json_output))


@talent_app.command("accept-handoff")
def talent_accept_handoff(
    handoff_id: str = typer.Argument(..., help="Handoff id to accept"),
    note: str = typer.Option("", "--note", help="Acceptance note"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Accept a sent handoff after re-checking its fixed artifacts."""
    from ampuly.core.service_models import ServiceError, ServiceResult
    from ampuly.plugins.office_ui.employee_handoff import HandoffRefused, accept_handoff

    async def operation(services):
        store = services.context.active_engine.store
        try:
            record = await accept_handoff(store, handoff_id, actor="owner", note=note)
        except HandoffRefused as exc:
            raise ServiceError("handoff_refused", str(exc.code), {"code": exc.code}) from exc
        return ServiceResult({"handoff_id": record.handoff_id, "status": record.status,
                              "accepted_at": record.accepted_at.isoformat() if record.accepted_at else ""})

    asyncio.run(_run_service_command(project, operation, json_output=json_output))


@talent_app.command("reject-handoff")
def talent_reject_handoff(
    handoff_id: str = typer.Argument(..., help="Handoff id to reject"),
    reason: str = typer.Option(..., "--reason", "-r", help="Why the handoff is rejected"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Reject a sent handoff with a reason; no downstream work starts."""
    from ampuly.core.service_models import ServiceError, ServiceResult
    from ampuly.plugins.office_ui.employee_handoff import HandoffRefused, reject_handoff

    async def operation(services):
        store = services.context.active_engine.store
        try:
            record = await reject_handoff(store, handoff_id, actor="owner", reason=reason)
        except HandoffRefused as exc:
            raise ServiceError("handoff_refused", str(exc.code), {"code": exc.code}) from exc
        return ServiceResult({"handoff_id": record.handoff_id, "status": record.status,
                              "rejected_at": record.rejected_at.isoformat() if record.rejected_at else ""})

    asyncio.run(_run_service_command(project, operation, json_output=json_output))


@talent_app.command("continue-handoff")
def talent_continue_handoff(
    handoff_id: str = typer.Argument(..., help="Accepted handoff id"),
    instance_id: str = typer.Argument(..., help="Adopted employee instance id"),
    instruction: str = typer.Argument(..., help="Follow-up instruction"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Start a downstream employee task from an accepted handoff."""
    _run_headless_talent_command(
        project, instance_id, instruction, json_output=json_output,
        source_handoff_id=handoff_id,
    )


@talent_app.command("rework-handoff")
def talent_rework_handoff(
    handoff_id: str = typer.Argument(..., help="Rejected handoff id"),
    instance_id: str = typer.Argument(..., help="Adopted employee instance id"),
    instruction: str = typer.Argument(..., help="Rework instruction"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Start a new attempt from a rejected handoff; old evidence is kept."""
    _run_headless_talent_command(
        project, instance_id, instruction, json_output=json_output,
        rework_of_handoff_id=handoff_id,
    )


@talent_app.command("import")
def talent_import(
    repo_path: str = typer.Argument(..., help="Local path to the agency-agents repository"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Import local agency-agent markdown files into recruitable talent templates."""
    config = _get_config()
    market = TalentMarket(get_ampuly_home(), config)
    imported = market.import_from_repo(_talent_repo_path(repo_path))
    config.save(get_ampuly_home() / "config")
    if json_output:
        _emit_payload({"count": len(imported), "imported": imported}, json_output=True)
        return
    console.print(f"[success]Imported {len(imported)} talent templates.[/success]")


@talent_app.command("hire")
def talent_hire(
    template_id: str = typer.Argument(..., help="Imported talent template id"),
    role_id: str = typer.Argument(..., help="Company role id to staff"),
    employee_name: Optional[str] = typer.Option(None, "--name", help="Optional hired employee display name"),
    employee_id: Optional[str] = typer.Option(None, "--employee-id", help="Optional employee id override"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Hire an imported template into a company role."""
    config = _get_config()
    market = TalentMarket(get_ampuly_home(), config)
    employee = market.hire_template(
        template_id,
        role_id,
        employee_name=employee_name,
        employee_id=employee_id,
    )
    config.save(get_ampuly_home() / "config")
    if json_output:
        _emit_payload({"employee": employee}, json_output=True)
        return
    console.print(
        f"[success]Hired {employee.name} into role `{employee.role_id}` "
        f"as `{employee.employee_id}`.[/success]"
    )


@talent_app.command("scan")
def talent_scan(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Scan local talent templates."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.scan(), json_output=json_output))


@talent_app.command("import-selected")
def talent_import_selected(template_ids: list[str] = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Import selected local talent templates."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.import_selected(template_ids), json_output=json_output))


@talent_app.command("employee-detail")
def talent_employee(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Show employee detail."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.employee_detail(employee_id), json_output=json_output))


@talent_app.command("import-agent")
def talent_import_agent(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Import an employee as a visual office agent."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.import_employee_as_agent(employee_id=employee_id), json_output=json_output))


talent_employee_app = typer.Typer(help="Manage hired employees")
talent_app.add_typer(talent_employee_app, name="employee")


@talent_employee_app.command("detail")
def talent_employee_detail(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.employee_detail(employee_id), json_output=json_output))


@talent_employee_app.command("import-agent")
def talent_employee_import_agent(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.import_employee_as_agent(employee_id=employee_id, office_id=office_id), json_output=json_output))


# ---------------------------------------------------------------------------
# Market subcommands
# ---------------------------------------------------------------------------
market_app = typer.Typer(help="SuperOnes Market — export, import, and manage architecture packages")
app.add_typer(market_app, name="market")


def _render_architecture_presets(presets: list[Any]) -> None:
    if not presets:
        console.print("[warning]No architecture presets available.[/warning]")
        return
    table = Table(title=f"Architecture Presets ({len(presets)})")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Roles", justify="right")
    table.add_column("Templates", justify="right")
    table.add_column("Pattern")
    for preset in presets:
        table.add_row(
            str(getattr(preset, "id", "") or ""),
            str(getattr(preset, "name", "") or ""),
            str(getattr(preset, "category", "") or ""),
            str(len(getattr(preset, "roles", []) or [])),
            str(len(getattr(preset, "work_item_templates", []) or [])),
            str(getattr(preset, "collaboration_pattern", "") or ""),
        )
    console.print(table)


@market_app.command("export")
def market_export(
    package_id: str = typer.Option(..., "--id", help="Package identifier (slug)"),
    name: str = typer.Option(..., "--name", help="Display name"),
    description: str = typer.Option("", "--desc", help="Package description"),
    version: str = typer.Option("1.0.0", "--version", help="Semantic version"),
    output_dir: str = typer.Option(".", "--output-dir", "-o", help="Output directory"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Export the current org as an .ampulypkg package."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.export(package_id=package_id, name=name, description=description, version=version, output_dir=output_dir), json_output=json_output))


@market_app.command("browse")
def market_browse(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Browse market architecture presets."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.browse(), json_output=json_output))


@market_app.command("preview")
def market_preview(preset_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Preview a built-in architecture preset."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.preview(preset_id), json_output=json_output))


@market_app.command("presets")
def market_presets(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List built-in architecture presets."""
    from ampuly.market.architecture_registry import get_all_presets

    if json_output:
        _emit_payload({"presets": get_all_presets()}, json_output=True)
        return
    _render_architecture_presets(get_all_presets())


@market_app.command("apply-preset")
def market_apply_preset(
    preset_id: str = typer.Argument(..., help="Built-in architecture preset id"),
    strategy: str = typer.Option("overwrite", "--strategy", "-s", help="Role id strategy: namespace or overwrite"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Apply a built-in architecture preset as the active custom organization."""
    if strategy not in {"namespace", "overwrite"}:
        console.print("[error]Strategy must be namespace or overwrite.[/error]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.market.apply_preset(preset_id=preset_id, strategy=strategy), json_output=json_output))


@market_app.command("install")
def market_install(
    path: str = typer.Argument(..., help="Path to .ampulypkg directory"),
    strategy: str = typer.Option("namespace", "--strategy", "-s", help="Conflict strategy: namespace or overwrite"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Install an .ampulypkg package from a local path."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.install(path=path, strategy=strategy), json_output=json_output))


@market_app.command("list")
def market_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List installed SuperOnes Market packages."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.list_installed(), json_output=json_output))


@market_app.command("uninstall")
def market_uninstall(
    package_id: str = typer.Argument(..., help="Package id to uninstall"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm uninstall"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Uninstall an installed AMPULY Market package."""
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.market.uninstall(package_id), json_output=json_output))


@channels_app.command("status")
def channels_status():
    """Show configured channel status."""
    # Inline on purpose: function globals do not call module __getattr__, and
    # the caller tests compile this body alone. Injected names still win.
    _probe_ns = globals()
    ProcessState = _probe_ns.get("ProcessState")
    process_state = _probe_ns.get("process_state")
    if ProcessState is None or not callable(process_state):
        from scripts.process_probe import ProcessState as _ProcessState
        from scripts.process_probe import process_state as _process_state

        ProcessState = _probe_ns["ProcessState"] = _ProcessState
        process_state = _probe_ns["process_state"] = _process_state
    config = _get_config()
    from ampuly.channels.manager import ChannelManager
    from ampuly.channels.provider_registry import ordered_provider_specs
    from ampuly.layer0_interaction.message_bus import MessageBus

    runtime_state = _read_channel_runtime_state()
    runtime_status = ProcessState.DEAD
    runtime_pid = None
    runtime_channels: list[str] = []
    if runtime_state:
        # Preserve the JSON type: bool/float/string are not valid PID facts.
        runtime_pid = runtime_state.get("pid")
        runtime_channels = list(runtime_state.get("channels", []) or [])
        runtime_status = process_state(runtime_pid)
        if runtime_status is ProcessState.DEAD:
            _clear_channel_runtime_state()
    runtime_running = runtime_status is ProcessState.ALIVE

    manager = ChannelManager(config, MessageBus())
    runtime_label = "unknown" if runtime_status is ProcessState.UNKNOWN else ("running" if runtime_running else "stopped")
    console.print(f"[bold]Runtime:[/bold] {runtime_label}")
    if runtime_pid:
        console.print(f"  PID: {runtime_pid}")
    console.print("[bold]Channels:[/bold]")
    for spec in ordered_provider_specs():
        if hasattr(manager, "get_status"):
            status = manager.get_status(spec.name)
        else:
            channel = manager.get_channel(spec.name)
            capability = channel.describe_capability() if channel is not None else {}
            status = {
                "name": spec.name,
                "enabled": bool(getattr(getattr(config.channels, spec.name), "enabled", False)),
                "available": capability.get("available", channel is not None),
                "configured": capability.get("configured", channel is not None),
                "delivery_mode": capability.get("delivery_mode", spec.delivery_mode),
                "bridge_required": spec.bridge_required,
                "last_error": capability.get("last_error", ""),
            }
        details: list[str] = ["enabled" if status["enabled"] else "disabled"]
        if status["enabled"]:
            details.append("available" if status["available"] else "dependency-missing")
            details.append("configured" if status["configured"] else "config-missing")
            if status["bridge_required"]:
                details.append("bridge-required")
            if status["delivery_mode"]:
                details.append(str(status["delivery_mode"]))
        if runtime_running and spec.name in runtime_channels:
            details.append("runtime-active")
        if status["last_error"]:
            details.append(f"error={status['last_error']}")
        console.print(f"  - {spec.name}: {', '.join(details)}")


@channels_app.command("start")
def channels_start(project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID")):
    """Start enabled channels in foreground."""
    config = _get_config()
    asyncio.run(_run_channel_runtime(config, project))


@channels_app.command("stop")
def channels_stop():
    """Stop foreground channel runtime."""
    _probe_ns = globals()
    ProcessState = _probe_ns.get("ProcessState")
    process_state = _probe_ns.get("process_state")
    if ProcessState is None or not callable(process_state):
        from scripts.process_probe import ProcessState as _ProcessState
        from scripts.process_probe import process_state as _process_state

        ProcessState = _probe_ns["ProcessState"] = _ProcessState
        process_state = _probe_ns["process_state"] = _process_state
    runtime_state = _read_channel_runtime_state()
    if not runtime_state:
        console.print("[warning]No running channel runtime found.[/warning]")
        return

    # Never coerce damaged state into the PID of an unrelated process.
    pid = runtime_state.get("pid")
    runtime_status = process_state(pid)
    if runtime_status is ProcessState.DEAD:
        _clear_channel_runtime_state()
        console.print("[warning]Channel runtime is not running.[/warning]")
        return
    if runtime_status is not ProcessState.ALIVE:
        console.print("[warning]Cannot verify channel runtime; state retained and no stop signal sent.[/warning]")
        raise typer.Exit(code=1)

    os.kill(pid, signal.SIGTERM)
    console.print(f"[success]Sent stop signal to channel runtime PID {pid}.[/success]")


@channels_app.command("login")
def channels_login(channel: Optional[str] = typer.Argument(None, help="Optional channel name")):
    """Show login/setup guidance for channels."""
    from ampuly.channels.provider_registry import PROVIDER_SPECS, ordered_provider_specs

    if channel:
        spec = PROVIDER_SPECS.get(channel)
        if spec is None:
            console.print(f"[warning]Unknown channel `{channel}`.[/warning]")
            return
        requirements = ", ".join(spec.required_config_fields) if spec.required_config_fields else "no required config fields"
        bridge = " Requires a companion bridge/runtime." if spec.bridge_required else ""
        console.print(
            f"[info]Configure `{channel}` in `.ampuly/config/channel_config.yaml`. "
            f"{escape(spec.login_summary)} Install extras with `{escape(f'pip install -e .[{spec.extra_name}]')}`. "
            f"Required config: {escape(requirements)}.{bridge} Then run `ampuly channels start`.[/info]"
        )
        return
    console.print("[info]Set channel credentials in .ampuly/config/channel_config.yaml and start the runtime with `ampuly channels start` or `ampuly run`.[/info]")
    for spec in ordered_provider_specs():
        bridge = " bridge-required;" if spec.bridge_required else ""
        console.print(
            f"  - {spec.name}: install `.[{spec.extra_name}]`; required fields: "
            f"{', '.join(spec.required_config_fields) or 'none'}; mode: {spec.delivery_mode};{bridge} {spec.login_summary}"
        )


@app.command()
def run(project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID")):
    """Run the long-lived engine + channel runtime in foreground."""
    config = _get_config()
    asyncio.run(_run_channel_runtime(config, project))


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _single_message(
    config,
    message: str,
    project: str | None,
    no_markdown: bool,
    *,
    mode: str = "task",
    preferred_agent: str | None = None,
    company_profile: str = "corporate",
    native_approval_level: NativeApprovalLevel = "auto",
) -> None:
    engine, runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        console.print(f"\n[info]Processing:[/info] {message}\n")
        runtime_display.begin_turn()
        native_scope_id = str(uuid.uuid4())
        response = await engine.process_message(
            message,
            project_id=project,
            session_id=native_scope_id,
            mode=mode,
            company_profile=company_profile if mode == "company" else None,
            preferred_agent=preferred_agent,
            message_metadata={
                "native_approval_level": native_approval_level,
                "native_permission_scope_id": native_scope_id,
            },
        )
        await runtime_display.flush()
        if not runtime_display.has_streamed_content:
            _print_response(response, no_markdown)
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/warning]")
    except Exception as e:
        console.print(f"\n[error]Error: {escape(str(e))}[/error]")
        if config.system.log_level == "DEBUG":
            import traceback
            console.print(escape(traceback.format_exc()))
    finally:
        await engine.shutdown()


async def _single_secretary_message(config, message: str, project: str | None, no_markdown: bool) -> None:
    engine, runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        console.print(f"\n[info]Secretary processing:[/info] {message}\n")
        runtime_display.begin_turn()
        payload = await engine.process_secretary_message(
            message,
            project_id=project,
            session_id=str(uuid.uuid4()),
        )
        await runtime_display.flush()
        if not runtime_display.has_streamed_content:
            _print_response(payload.get("response", ""), no_markdown)
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/warning]")
    except Exception as e:
        console.print(f"\n[error]Error: {escape(str(e))}[/error]")
        if config.system.log_level == "DEBUG":
            import traceback
            console.print(escape(traceback.format_exc()))
    finally:
        await engine.shutdown()


async def _propose_reorg(config, payload: str, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        data = json.loads(payload)
        proposal = await engine.propose_company_reorg(
            summary=str(data.get("summary", "Runtime company reorg")),
            rationale=str(data.get("rationale", data.get("summary", ""))),
            title=str(data.get("title", "")),
            changeset=data.get("changeset", {}),
            session_id=str(uuid.uuid4()),
            task_id=data.get("task_id"),
            initiated_by=str(data.get("initiated_by", "owner")),
            source_role_id=str(data.get("source_role_id", "")),
            metadata={"source": "cli"},
        )
        console.print(f"[success]Created reorg proposal:[/success] {proposal.proposal_id}")
        console.print(f"Status: {proposal.status.value}")
        console.print(f"Scope: {proposal.scope.value}")
        console.print(f"Risk: {proposal.risk_level.value}")
        console.print(f"Summary: {proposal.summary}")
        if proposal.user_confirmation_required:
            console.print("[warning]User confirmation is required before applying this reorg.[/warning]")
    finally:
        await engine.shutdown()


async def _approve_reorg(config, proposal_id: str, project: str | None, *, approved: bool) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        checkpoint = await _reorg_interaction(engine, proposal_id)
        if checkpoint is not None:
            receipt = await _submit_reorg_interaction(
                engine,
                checkpoint,
                approved=approved,
                notes=f"{'Approved' if approved else 'Denied'} via CLI.",
            )
            console.print(
                f"[success]Submitted reorg decision:[/success] {proposal_id}"
            )
            console.print(f"Checkpoint status: {receipt.get('status', 'answered')}")
            return
        proposal = await engine.approve_company_reorg(
            proposal_id,
            approved=approved,
            notes=f"{'Approved' if approved else 'Denied'} via CLI.",
        )
        console.print(f"[success]Updated reorg:[/success] {proposal.proposal_id}")
        console.print(f"Status: {proposal.status.value}")
    finally:
        await engine.shutdown()


async def _apply_reorg(config, proposal_id: str, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        result = await engine.apply_company_reorg(proposal_id)
        console.print(f"[success]Applied reorg:[/success] {proposal_id}")
        _print_json(result)
    finally:
        await engine.shutdown()


async def _reorg_interaction(engine: Any, proposal_id: str) -> Any | None:
    """Return the proposal's durable owner card, including terminal history."""

    store = getattr(engine, "store", None)
    if store is None:
        return None
    project_id = _current_project_id(engine)
    checkpoints = await store.get_execution_checkpoints(
        project_id=project_id,
        checkpoint_types=["company_reorg_pending"],
    )
    matches = [
        checkpoint
        for checkpoint in checkpoints
        if str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
        == "company_reorg_pending"
        and str(dict(getattr(checkpoint, "payload", {}) or {}).get("proposal_id", "") or "").strip()
        == str(proposal_id or "").strip()
        and str(getattr(checkpoint, "project_id", project_id) or project_id).strip()
        == project_id
    ]
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple durable reorg checkpoints reference proposal {proposal_id}."
        )
    return matches[0] if matches else None


async def _submit_reorg_interaction(
    engine: Any,
    checkpoint: Any,
    *,
    approved: bool,
    notes: str,
) -> dict[str, Any]:
    """Submit one reorg decision through the canonical durable ingress."""

    submit = getattr(engine, "submit_checkpoint_decision", None)
    if not callable(submit):
        raise RuntimeError("The engine does not support durable checkpoint replies.")
    action = "approve" if approved else "deny"
    checkpoint_status = str(
        getattr(checkpoint, "status", "") or ""
    ).strip().lower()
    if checkpoint_status != "pending":
        raise RuntimeError(
            "The reorg decision is already durable "
            f"({checkpoint_status or 'unknown'}); it cannot be revised."
        )
    requester_task_id, requester_session_id = owner_interaction_actor_identity(
        checkpoint
    )
    requester_actor: dict[str, Any] | None = None
    if not requester_task_id and not requester_session_id:
        issue_actor = getattr(engine, "issue_project_owner_actor", None)
        if not callable(issue_actor):
            raise RuntimeError("The engine cannot authorize a taskless owner reply.")
        requester_actor = issue_actor(interface="cli")
    receipt = await submit(
        checkpoint_id=str(getattr(checkpoint, "checkpoint_id", "") or ""),
        checkpoint_type="company_reorg_pending",
        decision={
            "option_id": action,
            "checkpoint_reply_kind": action,
            "text": str(notes or action).strip() or action,
        },
        client_request_id=(
            f"cli-reorg:{getattr(checkpoint, 'checkpoint_id', '')}:{uuid.uuid4().hex}"
        ),
        requester_task_id=requester_task_id,
        requester_session_id=requester_session_id or None,
        requester_actor=requester_actor,
    )
    if not bool(receipt.get("accepted")):
        reason = str(receipt.get("reason", "") or "invalid_decision").strip()
        raise RuntimeError(f"Reorg decision was not accepted ({reason}).")
    return dict(receipt)


async def _show_reorg(config, proposal_id: str, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        proposal = await engine.show_company_reorg(proposal_id)
        if not proposal:
            console.print(f"[warning]Unknown reorg proposal:[/warning] {proposal_id}")
            return
        _print_json({
            "proposal_id": proposal.proposal_id,
            "status": proposal.status.value,
            "scope": proposal.scope.value,
            "risk_level": proposal.risk_level.value,
            "summary": proposal.summary,
            "rationale": proposal.rationale,
            "impact_summary": proposal.impact_summary,
            "approval_notes": proposal.approval_notes,
            "metadata": proposal.metadata,
        })
    finally:
        await engine.shutdown()


_SLASH_DEFAULT_LIMIT = 20
_SLASH_MAX_LIMIT = 100
_VALID_CHAT_MODES = {"task", "company"}
_VALID_COMPANY_PROFILES = {"corporate", "custom"}
_VALID_PREFERRED_AGENTS = EXECUTION_AGENTS


@dataclass(frozen=True)
class _SlashCommandSpec:
    group: str
    command: str
    description: str
    subcommands: tuple[str, ...] = ()


@dataclass
class _InteractiveChatState:
    config: AmpulyConfig
    engine: Any
    runtime_display: Any
    session_id: str
    no_markdown: bool
    mode: str = "task"
    company_profile: str = "corporate"
    org_id: str = ""
    preferred_agent: str | None = "native"
    native_approval_level: NativeApprovalLevel = "auto"
    native_permission_scope_id: str = ""
    domains: list[str] = field(default_factory=list)
    company_staffing_drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_to_task: dict[str, str] = field(default_factory=dict)
    active_runtime_children: dict[str, str] = field(default_factory=dict)
    runtime_control_state: str = ""
    runtime_control_task_id: str = ""
    runtime_control_session_id: str = ""
    runtime_control_checkpoint_id: str = ""


class BusyCommandPolicy(str, Enum):
    IMMEDIATE_READONLY = "immediate_readonly"
    BLOCKED_WHEN_BUSY = "blocked_when_busy"
    NORMAL_WHEN_IDLE = "normal_when_idle"


@dataclass
class QueuedChatInput:
    text: str
    project_id: str
    session_id: str
    mode: str
    company_profile: str
    preferred_agent: str | None
    domains: list[str]
    org_id: str = ""
    message_metadata: dict[str, Any] | None = None
    allow_interactive_followups: bool = False
    enqueue_time: float = field(default_factory=time.time)


class ChatTurnController:
    """Keep interactive chat responsive while a turn is running."""

    def __init__(self, state: _InteractiveChatState) -> None:
        self.state = state
        self.queue: deque[QueuedChatInput] = deque()
        self.active_task: asyncio.Task[None] | None = None
        self.kanban_watch_task: asyncio.Task[None] | None = None
        self._kanban_watch_fingerprint = ""
        self._lock: asyncio.Lock | None = None
        self._closing = False

    @property
    def is_busy(self) -> bool:
        return self.active_task is not None and not self.active_task.done()

    def queue_depth(self) -> int:
        return len(self.queue)

    def kanban_watch_active(self) -> bool:
        return self.kanban_watch_task is not None and not self.kanban_watch_task.done()

    def queued_items(self) -> list[QueuedChatInput]:
        return list(self.queue)

    async def submit_user_message(self, text: str) -> None:
        item = QueuedChatInput(
            text=text,
            project_id=_current_project_id(self.state.engine),
            session_id=self.state.session_id,
            mode=self.state.mode,
            company_profile=self.state.company_profile,
            org_id=self.state.org_id,
            preferred_agent=self.state.preferred_agent,
            domains=list(self.state.domains),
        )
        await self.submit_item(item)

    async def submit_control_message(
        self,
        text: str,
        *,
        message_metadata: dict[str, Any],
        mode: str | None = None,
        company_profile: str | None = None,
        org_id: str | None = None,
        preferred_agent: str | None = None,
        domains: list[str] | None = None,
    ) -> None:
        item = QueuedChatInput(
            text=text,
            project_id=_current_project_id(self.state.engine),
            session_id=self.state.session_id,
            mode=mode or self.state.mode,
            company_profile=company_profile or self.state.company_profile,
            org_id=self.state.org_id if org_id is None else org_id,
            preferred_agent=self.state.preferred_agent if preferred_agent is None else preferred_agent,
            domains=list(self.state.domains if domains is None else domains),
            message_metadata=dict(message_metadata),
        )
        await self.submit_item(item)

    async def submit_item(self, item: QueuedChatInput) -> None:
        async with self._ensure_lock():
            if self.is_busy:
                self.queue.append(item)
                console.print(
                    f"[dim]Queued #{len(self.queue)}; it will run after the current turn. "
                    "Use /queue list or /queue drop <n>.[/dim]"
                )
                return
            self._start_item_locked(item)

    async def wait_for_active(self) -> None:
        task = self.active_task
        if task is not None:
            await task

    async def shutdown(self) -> None:
        self._closing = True
        self.queue.clear()
        await self.stop_kanban_watch(silent=True)
        prepare = getattr(
            self.state.engine,
            "prepare_active_company_runtimes_for_shutdown",
            None,
        )
        if callable(prepare):
            # Persist company checkpoints and put the engine into
            # infrastructure-shutdown mode before cancelling the active turn.
            # Engine.shutdown() repeats this idempotently when it closes the
            # remaining stores and subsystems.
            await prepare()
        task = self.active_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    def drop(self, index: int) -> QueuedChatInput | None:
        if index < 1 or index > len(self.queue):
            return None
        items = list(self.queue)
        item = items.pop(index - 1)
        self.queue = deque(items)
        return item

    def clear(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    async def start_kanban_watch(
        self,
        *,
        interval: float = 2.0,
        limit: int = 100,
        initial_fingerprint: str = "",
        session_id: str | None = None,
        project_scope: bool = False,
    ) -> None:
        if self.kanban_watch_active():
            console.print("[dim]Kanban live watch is already running. Use /kanban stop to stop it.[/dim]")
            return
        self._kanban_watch_fingerprint = initial_fingerprint
        self.kanban_watch_task = asyncio.create_task(
            self._kanban_watch_loop(
                interval=interval,
                limit=limit,
                session_id=session_id,
                project_scope=project_scope,
            )
        )
        scope_label = "project" if project_scope else f"session {(session_id or self.state.session_id)[:8]}"
        console.print(
            f"[dim]Kanban live watch started for {escape(scope_label)}. "
            "New or changed work items will appear here. Exit with /kanban stop.[/dim]"
        )

    async def stop_kanban_watch(self, *, silent: bool = False) -> None:
        task = self.kanban_watch_task
        self.kanban_watch_task = None
        if task is None or task.done():
            if not silent:
                console.print("[dim]Kanban live watch is not running.[/dim]")
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if not silent:
            console.print("[dim]Kanban live watch stopped.[/dim]")

    def _start_item_locked(self, item: QueuedChatInput) -> None:
        self.active_task = asyncio.create_task(self._run_item(item))

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _run_item(self, item: QueuedChatInput) -> None:
        self.state.session_id = item.session_id
        self.state.mode = item.mode
        self.state.company_profile = item.company_profile
        self.state.org_id = item.org_id
        self.state.preferred_agent = item.preferred_agent
        self.state.domains = list(item.domains)
        try:
            await _process_interactive_chat_message(
                self.state,
                item.text,
                message_metadata=item.message_metadata,
                interactive_followups=item.allow_interactive_followups,
            )
        except asyncio.CancelledError:
            raise
        finally:
            async with self._ensure_lock():
                self.active_task = None
                if not self._closing and self.queue:
                    next_item = self.queue.popleft()
                    console.print(f"[dim]Running queued input; {len(self.queue)} remaining.[/dim]")
                    self._start_item_locked(next_item)

    async def _kanban_watch_loop(
        self,
        *,
        interval: float,
        limit: int,
        session_id: str | None,
        project_scope: bool,
    ) -> None:
        try:
            while True:
                items = await _fetch_kanban_items(
                    self.state,
                    limit=limit,
                    session_id=session_id,
                    project_scope=project_scope,
                )
                fingerprint = _kanban_items_fingerprint(items)
                if fingerprint != self._kanban_watch_fingerprint:
                    self._kanban_watch_fingerprint = fingerprint
                    title = "Kanban Live Update (project)" if project_scope else f"Kanban Live Update (session {(session_id or self.state.session_id)[:8]})"
                    _render_kanban_items(items, title=title)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            console.print(f"[warning]Kanban live watch stopped: {escape(str(exc))}[/warning]")


_SLASH_COMMANDS: tuple[_SlashCommandSpec, ...] = (
    _SlashCommandSpec("Chat", "/help", "Show this command list."),
    _SlashCommandSpec("Chat", "/quit", "Exit interactive chat."),
    _SlashCommandSpec("Chat", "/queue list|drop|clear", "Inspect or edit queued prompts while a turn is running.", ("list", "drop", "clear")),
    _SlashCommandSpec("Context", "/status", "Show project, session, mode, agent, domains, model, and cost."),
    _SlashCommandSpec("Context", "/permissions [read-only|auto|full-access]", "Show or change SuperOnes Native permissions for this session.", ("read-only", "auto", "full-access")),
    _SlashCommandSpec("Context", "/mode [task|company] [corporate|custom]", "Set execution mode for future messages.", ("task", "company", "corporate", "custom")),
    _SlashCommandSpec("Context", "/agent [native|codex|jiuwen|jiuwenswarm|claude_code|cursor|opencode|none]", "Set preferred agent; jiuwen = JiuwenSwarm-single, jiuwenswarm = JiuwenSwarm-team.", ("native", "codex", "jiuwen", "jiuwenswarm", "claude_code", "cursor", "opencode", "none")),
    _SlashCommandSpec("Context", "/domains [domain ...|clear]", "Set domain hints for future messages.", ("clear",)),
    _SlashCommandSpec("Project", "/project", "Show current project, known projects, and switch/create/delete usage.", ("list", "switch", "create", "rename", "delete")),
    _SlashCommandSpec("Project", "/project list", "List known projects."),
    _SlashCommandSpec("Project", "/project create <id>", "Create a project through the shared Office service."),
    _SlashCommandSpec("Project", "/project switch <id>", "Switch project and restore its latest session."),
    _SlashCommandSpec("Project", "/project rename <old_id> <new_id>", "Rename a project id and move persisted project data."),
    _SlashCommandSpec("Project", "/project delete <id> --yes", "Delete a project through the shared Office service."),
    _SlashCommandSpec("Session", "/stop [task_id|session_id]", "Stop the current or selected runtime and preserve history for follow-up."),
    _SlashCommandSpec("Session", "/continue [task_id|session_id] [message]", "Continue a stopped runtime without re-planning."),
    _SlashCommandSpec("Session", "/session", "Show current session, recent sessions, and switch/create/delete usage.", ("list", "new", "create", "resume", "show", "config", "send", "rename", "delete", "stop", "continue", "complete")),
    _SlashCommandSpec("Session", "/session list [limit]", "List recent sessions."),
    _SlashCommandSpec("Session", "/session new", "Start a fresh conversation session."),
    _SlashCommandSpec("Session", "/session create [title] [--mode task|company|org] [--agent ...]", "Create a task-backed session through the shared Office service."),
    _SlashCommandSpec("Session", "/session resume <session_id>", "Resume a session in this project."),
    _SlashCommandSpec("Session", "/session show <session_id|task_id> [--limit N] [--full]", "Show session metadata and transcript."),
    _SlashCommandSpec("Session", "/session config <task_id> [--mode ...] [--agent ...] [--org ...] [--approval-level ...]", "Update session execution config through the shared Office service."),
    _SlashCommandSpec("Session", "/session send <task_id> <message>", "Send a message to a task-backed session through the shared Office service."),
    _SlashCommandSpec("Session", "/session rename <task_id|session_id> <title>", "Rename a task-backed or plain session."),
    _SlashCommandSpec("Session", "/session delete <task_id> --yes", "Hard-delete a task-backed session."),
    _SlashCommandSpec("Session", "/session stop [task_id|session_id]", "Stop the current or selected runtime through the shared Office service."),
    _SlashCommandSpec("Session", "/session continue [task_id|session_id] [message]", "Continue a stopped runtime through the shared Office service."),
    _SlashCommandSpec("Session", "/session complete <task_id>", "Complete a task-backed session through the shared Office service."),
    _SlashCommandSpec("Tasks", "/tasks [status] [--limit N] [--full]", "List current project tasks."),
    _SlashCommandSpec("Tasks", "/task show <task_id> [--limit N] [--full]", "Show task detail and transcript.", ("show", "move", "done", "rename", "delete")),
    _SlashCommandSpec("Tasks", "/task move <task_id> todo|in-progress|done|blocked|failed|cancelled", "Move a task through the shared transition API."),
    _SlashCommandSpec("Tasks", "/task done <task_id>", "Mark a task done through the shared transition API."),
    _SlashCommandSpec("Tasks", "/task rename <task_id> <title>", "Rename a task and its session title."),
    _SlashCommandSpec("Tasks", "/task delete <task_id> --yes", "Hard-delete a task and its persisted lifecycle data."),
    _SlashCommandSpec("Runtime", "/runtime [--limit N] [--full]", "Show live runtime, active tasks, external sessions, and checkpoints."),
    _SlashCommandSpec("Runtime", "/logs <task_id|session_id> [--limit N] [--full]", "Show execution logs, runtime events, tools, and transcript."),
    _SlashCommandSpec("Runtime", "/comms <task_id> [--limit N] [--full]", "Show company-mode messages, handoffs, and review notes."),
    _SlashCommandSpec("Runtime", "/attachments [--limit N] [--full]", "List current session attachment references."),
    _SlashCommandSpec("Runtime", "/staffing [context]", "Open the pending company staffing/agent editor or role context preview.", ("context",)),
    _SlashCommandSpec("Board", "/kanban [once|stop|all]", "Show current-session work-item status inline and optionally watch live updates.", ("once", "stop", "all", "project", "--once", "--all")),
    _SlashCommandSpec("Board", "/board kanban|pipeline|work-item|role|logs", "Open ampuly board in read-only inspector mode.", ("kanban", "pipeline", "work-item", "role", "logs")),
    _SlashCommandSpec("Work Items", "/work-items list|show|logs|role-status", "Inspect company work items and role progress.", ("list", "show", "logs", "role-status")),
    _SlashCommandSpec("Org", "/org", "Show or edit organization config.", ("info", "role", "policy", "strategy", "reset", "saved", "export", "import")),
    _SlashCommandSpec("Org", "/org role add|update|delete|bulk-add", "Manage company roles through the shared Office service."),
    _SlashCommandSpec("Org", "/org policy update --payload ...", "Update runtime policy through the shared Office service."),
    _SlashCommandSpec("Org", "/org strategy update --final-decider <role>", "Update organization strategy."),
    _SlashCommandSpec("Org", "/org saved list|save|load|delete", "Manage saved organization architectures."),
    _SlashCommandSpec("Agent", "/agent list|detail|create|delete|move|import-employee", "Manage visual office agents.", ("list", "detail", "create", "delete", "move", "create-from-template", "import-employee")),
    _SlashCommandSpec("Talent", "/talent list|employees|scan", "Browse talent templates and employees.", ("list", "employees", "scan", "import", "import-repo", "hire", "employee", "import-agent")),
    _SlashCommandSpec("Talent", "/talent import <template_id...>", "Import local talent templates."),
    _SlashCommandSpec("Talent", "/talent import-repo <path>", "Import talent templates from a local repo."),
    _SlashCommandSpec("Talent", "/talent hire <template_id> <role_id> [--name ...]", "Hire a template into a role."),
    _SlashCommandSpec("Talent", "/talent employee <employee_id>", "Show employee detail."),
    _SlashCommandSpec("Talent", "/talent import-agent <employee_id>", "Import an employee as a visual office agent."),
    _SlashCommandSpec("Market", "/market list", "List installed SuperOnes Market packages.", ("browse", "preview", "list", "presets", "apply-preset", "install", "uninstall", "export")),
    _SlashCommandSpec("Market", "/market browse|preview <preset_id>", "Browse and preview architecture presets."),
    _SlashCommandSpec("Market", "/market presets", "List built-in architecture presets."),
    _SlashCommandSpec("Market", "/market apply-preset <preset_id> [--strategy namespace|overwrite]", "Apply a built-in architecture preset as the active custom organization."),
    _SlashCommandSpec("Market", "/market install <path> [--strategy namespace|overwrite]", "Install a local .ampulypkg package."),
    _SlashCommandSpec("Market", "/market uninstall <package_id> --yes", "Uninstall a SuperOnes Market package."),
    _SlashCommandSpec("Market", "/market export --id ... --name ...", "Export the current org as a package."),
    _SlashCommandSpec("Reorg", "/reorg list|show|approve|deny|apply", "Manage reorg proposals.", ("list", "show", "approve", "deny", "apply")),
    _SlashCommandSpec("Diagnostics", "/cost", "Show token and cost counters."),
    _SlashCommandSpec("Diagnostics", "/checkpoints [--limit N] [--full]", "List pending execution checkpoints."),
)
CommandSpec = _SlashCommandSpec
_SLASH_ALIASES = {"p": "project", "s": "session", "t": "task", "checkpoint": "checkpoints", "work-item": "work-items", "workitems": "work-items"}


def _initial_company_profile(config: AmpulyConfig) -> str:
    value = str(getattr(getattr(config, "org", None), "company_profile", "") or "corporate").strip().lower()
    return value if value in _VALID_COMPANY_PROFILES else "corporate"


def _normalize_interactive_preferred_agent(value: str | None) -> str | None:
    normalized = str(value or "native").strip().lower().replace("-", "_")
    if normalized in {"none", "auto", "default", "system"}:
        return None
    return normalized if normalized in _VALID_PREFERRED_AGENTS else "native"


def _cli_option_present(*names: str) -> bool:
    argv = list(sys.argv[1:])
    for idx, arg in enumerate(argv):
        if arg in names:
            return True
        if any(arg.startswith(f"{name}=") for name in names):
            return True
    return False


async def _read_persisted_chat_context(state: _InteractiveChatState) -> dict[str, str]:
    try:
        import sqlite3

        from ampuly.plugins.office_ui.ui_state_schema import SERVER_STATE_DDL

        db_path = state.engine.ampuly_home / "ui_state.db"
        if not db_path.exists():
            return {}
        with opened(db_path, timeout=1) as db:
            db.execute(SERVER_STATE_DDL)
            rows = db.execute(
                "SELECT key, value FROM server_state WHERE key IN (?, ?, ?)",
                ("exec_mode", "company_profile", "task_preferred_agent"),
            ).fetchall()
        values = {str(key): str(value) for key, value in rows}
        return {
            "mode": values.get("exec_mode", ""),
            "company_profile": values.get("company_profile", ""),
            "preferred_agent": values.get("task_preferred_agent", ""),
        }
    except Exception:
        return {}


async def _persist_chat_context(state: _InteractiveChatState) -> None:
    try:
        import sqlite3

        from ampuly.plugins.office_ui.ui_state_schema import SERVER_STATE_DDL

        db_path = state.engine.ampuly_home / "ui_state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with opened(db_path, timeout=1) as db:
            db.execute(SERVER_STATE_DDL)
            db.executemany(
                "INSERT OR REPLACE INTO server_state (key, value) VALUES (?, ?)",
                (
                    ("exec_mode", state.mode),
                    ("company_profile", state.company_profile),
                    ("task_preferred_agent", state.preferred_agent or "native"),
                ),
            )
            db.commit()
    except Exception as exc:
        console.print(f"[warning]Could not persist CLI mode state: {escape(str(exc))}[/warning]")


async def _restore_chat_context(
    state: _InteractiveChatState,
    *,
    restore_mode: bool,
    restore_company_profile: bool,
    restore_agent: bool,
) -> None:
    persisted = await _read_persisted_chat_context(state)
    raw_mode = str(persisted.get("mode", "") or "").strip().lower()
    raw_profile = str(persisted.get("company_profile", "") or "").strip().lower()
    raw_agent = str(persisted.get("preferred_agent", "") or "").strip().lower().replace("-", "_")
    if restore_mode:
        if raw_mode in {"company", "company_mode"}:
            state.mode = "company"
        elif raw_mode in {"org", "custom"}:
            state.mode = "company"
            if restore_company_profile:
                state.company_profile = "custom"
        elif raw_mode in {"task", "project", "task_mode", "project_mode"}:
            state.mode = "task"
    if restore_company_profile and raw_profile in _VALID_COMPANY_PROFILES:
        state.company_profile = raw_profile
    if restore_agent and raw_agent:
        state.preferred_agent = _normalize_interactive_preferred_agent(raw_agent)


def _current_project_id(engine: Any) -> str:
    return str(getattr(engine, "project_id", None) or "default").strip() or "default"


def _value_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _format_datetime(value: Any) -> str:
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _clip_text(value: Any, limit: int = 240, *, full: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if full:
        return text
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _json_summary(value: Any, limit: int = 240, *, full: bool = False) -> str:
    if isinstance(value, dict):
        for key in ("summary", "title", "message", "prompt", "original_message"):
            if value.get(key):
                return _clip_text(value.get(key), limit, full=full)
    try:
        return _clip_text(json.dumps(value, ensure_ascii=False, default=str), limit, full=full)
    except TypeError:
        return _clip_text(value, limit, full=full)


def _coerce_limit(value: str, *, default: int = _SLASH_DEFAULT_LIMIT) -> int:
    if not str(value or "").strip():
        return default
    limit = int(str(value).strip())
    return max(1, min(_SLASH_MAX_LIMIT, limit))


def _parse_limit_args(args: list[str], *, default: int = _SLASH_DEFAULT_LIMIT) -> tuple[list[str], int]:
    remaining: list[str] = []
    limit = default
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--limit":
            if idx + 1 >= len(args):
                raise ValueError("Missing value for --limit.")
            limit = _coerce_limit(args[idx + 1], default=default)
            idx += 2
            continue
        if token.startswith("--limit="):
            limit = _coerce_limit(token.split("=", 1)[1], default=default)
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    return remaining, limit


def _parse_view_args(args: list[str], *, default: int = _SLASH_DEFAULT_LIMIT) -> tuple[list[str], int, bool]:
    remaining: list[str] = []
    full = False
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--full":
            full = True
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    remaining, limit = _parse_limit_args(remaining, default=default)
    return remaining, limit, full


def _slash_parts(raw_input: str) -> list[str]:
    command = raw_input[1:] if raw_input.startswith("/") else raw_input
    return shlex.split(command)


def _canonical_slash_command(command: str) -> str:
    return _SLASH_ALIASES.get(command.strip().lower(), command.strip().lower())


def _slash_command_names() -> list[str]:
    names = {
        spec.command.split()[0].lstrip("/")
        for spec in _SLASH_COMMANDS
        if spec.command.startswith("/")
    }
    names.update(_SLASH_ALIASES.keys())
    return sorted(names)


def _slash_subcommands(command: str) -> list[str]:
    canonical = _canonical_slash_command(command)
    values: set[str] = set()
    for spec in _SLASH_COMMANDS:
        root = spec.command.split()[0].lstrip("/")
        if root == canonical:
            values.update(spec.subcommands)
            parts = spec.command.split()
            if len(parts) > 1 and not parts[1].startswith("[") and not parts[1].startswith("<"):
                values.add(parts[1].split("|", 1)[0])
    return sorted(item for item in values if item)


class _AmpulySlashCompleter:
    def get_completions(self, document: Any, complete_event: Any):  # noqa: ANN201 - prompt-toolkit protocol
        try:
            from prompt_toolkit.completion import Completion
        except ImportError:
            return
        text = str(getattr(document, "text_before_cursor", "") or "")
        if not text.startswith("/"):
            return
        body = text[1:]
        if " " not in body:
            prefix = body.lower()
            if prefix in _slash_command_names():
                for suggestion in _slash_subcommands(prefix):
                    yield Completion(f"/{prefix} {suggestion}", start_position=-len(text))
                return
            for command in _slash_command_names():
                if command.startswith(prefix):
                    yield Completion(f"/{command}", start_position=-len(text))
            return
        tokens = body.split()
        command = _canonical_slash_command(tokens[0] if tokens else "")
        prefix = "" if body.endswith(" ") else (tokens[-1].lower() if len(tokens) > 1 else "")
        suggestions = _slash_subcommands(command)
        if command == "agent":
            suggestions.extend(sorted([*_VALID_PREFERRED_AGENTS, "none"]))
        if command == "mode":
            suggestions.extend(["task", "company", "corporate", "custom"])
        for suggestion in sorted(set(suggestions)):
            if suggestion.startswith(prefix):
                yield Completion(suggestion, start_position=-len(prefix))

    async def get_completions_async(self, document: Any, complete_event: Any):  # noqa: ANN201 - prompt-toolkit protocol
        for completion in self.get_completions(document, complete_event):
            yield completion


def _print_slash_help() -> None:
    table = Table(title="SuperOnes Slash Commands", show_lines=False)
    table.add_column("Group", style="cyan", no_wrap=True)
    table.add_column("Command", style="bold")
    table.add_column("What it does")
    for spec in _SLASH_COMMANDS:
        table.add_row(spec.group, spec.command, spec.description)
    table.add_row("Aliases", "/p /s /t", "Short aliases for /project, /session, and /task.")
    console.print(table)


def _print_interactive_cost(engine: Any) -> None:
    if not getattr(engine, "llm", None):
        console.print("[warning]LLM runtime is not available.[/warning]")
        return
    stats = getattr(engine.llm, "stats", {}) or {}
    console.print(
        f"Session: tokens_in={stats.get('tokens_in', 0)}, "
        f"tokens_out={stats.get('tokens_out', 0)}, "
        f"cost=${float(stats.get('estimated_cost', 0.0) or 0.0):.4f}"
    )


def _print_context_status(state: _InteractiveChatState) -> None:
    table = Table(title="Current Context", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Project", _current_project_id(state.engine))
    table.add_row("Session", state.session_id or "(none)")
    table.add_row("Mode", state.mode)
    table.add_row("Company profile", state.company_profile if state.mode == "company" else "(inactive)")
    if state.org_id:
        table.add_row("Org", state.org_id)
    table.add_row(
        "Preferred agent",
        execution_agent_label(state.preferred_agent)
        if state.preferred_agent
        else "(system default)",
    )
    default_level = normalize_native_approval_level(
        state.config.autonomy.native_approval_level
    )
    table.add_row("Native permissions default", default_level)
    table.add_row("Native permissions session", state.native_approval_level)
    table.add_row(
        "Native sandbox",
        str(native_sandbox_profile(state.native_approval_level)["mode"]),
    )
    table.add_row("Domains", ", ".join(state.domains) if state.domains else "(none)")
    table.add_row("Runtime control", state.runtime_control_state or "(running/idle)")
    if state.runtime_control_task_id:
        table.add_row("Runtime task", state.runtime_control_task_id)
    if state.runtime_control_checkpoint_id:
        table.add_row("Runtime checkpoint", state.runtime_control_checkpoint_id)
    console.print(table)
    console.print("[dim]Use /mode, /agent, /permissions, /project, /session, /stop, or /continue to inspect and control context.[/dim]")


def _list_cli_project_ids(engine: Any) -> list[str]:
    project_ids = {"default", _current_project_id(engine)}
    projects_dir = Path(getattr(engine, "ampuly_home", get_ampuly_home())) / "projects"
    if projects_dir.is_dir():
        for entry in sorted(projects_dir.iterdir()):
            if entry.is_dir():
                project_ids.add(entry.name)
    return sorted(project_ids, key=lambda item: (item != "default", item.lower()))


def _cli_project_paths(engine: Any, project_id: str) -> tuple[Path, Path, Path]:
    from ampuly.core.config import get_project_workplace

    ampuly_home = Path(getattr(engine, "ampuly_home", get_ampuly_home()))
    return (
        ampuly_home / "projects" / project_id,
        ampuly_home / "memory" / "projects" / f"{project_id}.md",
        get_project_workplace(project_id),
    )


def _cli_project_exists(engine: Any, project_id: str) -> bool:
    if project_id == "default":
        return True
    return any(path.exists() for path in _cli_project_paths(engine, project_id))


def _render_project_list(state: _InteractiveChatState) -> None:
    current = _current_project_id(state.engine)
    table = Table(title="Projects")
    table.add_column("Project")
    table.add_column("Current", justify="center")
    table.add_column("Markers")
    for project_id in _list_cli_project_ids(state.engine):
        project_dir, project_memory, workplace = _cli_project_paths(state.engine, project_id)
        markers: list[str] = []
        if (project_dir / "tasks.db").exists():
            markers.append("tasks")
        if project_memory.exists():
            markers.append("memory")
        if workplace.exists():
            markers.append("workplace")
        table.add_row(project_id, "*" if project_id == current else "", ", ".join(markers) or "")
    console.print(table)
    console.print("[dim]Switch with /project switch <id> or /project <id>. Create with /project create <id>. Rename with /project rename <old_id> <new_id>.[/dim]")


def _render_project_picker(state: _InteractiveChatState, project_ids: list[str]) -> None:
    current = _current_project_id(state.engine)
    table = Table(title="Choose Project")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Project")
    table.add_column("Current", justify="center")
    for idx, project_id in enumerate(project_ids, start=1):
        table.add_row(str(idx), project_id, "*" if project_id == current else "")
    console.print(table)
    console.print("[dim]Enter a number or project id. Use `new <id>` to create a project. Blank selects the current project.[/dim]")


def _render_session_picker(sessions: list[Any]) -> None:
    if not sessions:
        console.print("[info]No sessions found for this project.[/info]")
        console.print("[dim]Press Enter to create a new session.[/dim]")
        return
    table = Table(title="Choose Session")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Session ID")
    table.add_column("Title")
    table.add_column("Mode")
    table.add_column("Updated")
    for idx, item in enumerate(sessions, start=1):
        table.add_row(
            str(idx),
            str(getattr(item, "session_id", "") or ""),
            _clip_text(getattr(item, "title", "") or "(untitled)", 72),
            str(getattr(item, "mode", "") or ""),
            _format_datetime(getattr(item, "updated_at", None)),
        )
    console.print(table)
    console.print("[dim]Enter a number or session id. Press Enter, or type `new`, to create a new session.[/dim]")


async def _load_recent_primary_sessions(store: AmpulyStore | None, project_id: str | None, *, limit: int = 20) -> list[Any]:
    if not store:
        return []
    return await store.list_sessions(project_id=project_id or "default", parent_session_id=None, limit=limit)


def _session_short_id(session_id: str) -> str:
    text = str(session_id or "").strip()
    if len(text) <= 12:
        return text or "(none)"
    return f"{text[:8]}..."


def _chat_bottom_toolbar_text(state: _InteractiveChatState, controller: ChatTurnController | None = None) -> list[tuple[str, str]]:
    profile = f" {state.company_profile}" if state.mode == "company" else ""
    org = f" org:{state.org_id}" if state.org_id else ""
    busy_hint = ""
    if controller is not None:
        if controller.is_busy:
            kanban_hint = " kanban:live" if controller.kanban_watch_active() else ""
            busy_hint = f"  running queue:{controller.queue_depth()}{kanban_hint}  Enter=queue /kanban /logs /runtime"
        elif controller.kanban_watch_active():
            busy_hint = "  kanban:live exit:/kanban stop"
        elif controller.queue_depth():
            busy_hint = f"  queue:{controller.queue_depth()}"
    runtime_hint = f"  runtime:{state.runtime_control_state}" if state.runtime_control_state else ""
    text = (
        f" project:{_current_project_id(state.engine)}"
        f"  session:{_session_short_id(state.session_id)}"
        f"  mode:{state.mode}{profile}"
        f"{org}"
        f"  agent:{execution_agent_label(state.preferred_agent) if state.preferred_agent else 'system'}"
        f"  permissions:{state.native_approval_level}"
        f"{runtime_hint}"
        f"{busy_hint}"
        "    /mode /agent /permissions /project /session /help"
    )
    return [("class:bottom-toolbar", text)]


def _print_chat_hint() -> None:
    console.print("[dim]Hint: /mode and /agent select execution; /permissions controls SuperOnes Native for this session.[/dim]")


async def _run_chat_office_service(state: _InteractiveChatState, operation: Any) -> dict[str, Any] | None:
    from ampuly.core.service_models import ServiceError
    from ampuly.plugins.office_ui.services.factory import OfficeServiceFactory

    try:
        async with OfficeServiceFactory(config=state.config, project_id=_current_project_id(state.engine)) as services:
            result = await operation(services)
            return {"ok": True, **dict(result.payload)}
    except ServiceError as exc:
        console.print(f"[warning]{escape(exc.message)}[/warning]")
        return None
    except Exception as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        return None


async def _run_chat_current_office_service(state: _InteractiveChatState, operation: Any) -> dict[str, Any] | None:
    from ampuly.plugins.office_ui.services import ModeState, OfficeServiceContext, OfficeServices, ServiceError

    try:
        context = OfficeServiceContext(
            engine=state.engine,
            agent_store=None,
            chat_store=None,
            event_adapter=None,
            mode_state=ModeState(
                exec_mode=state.mode,
                company_profile=state.company_profile,
                task_preferred_agent=state.preferred_agent or "native",
            ),
        )
        context.session_to_task = state.session_to_task
        context.active_runtime_children = state.active_runtime_children
        result = await operation(OfficeServices(context))
        return {"ok": True, **dict(result.payload)}
    except ServiceError as exc:
        console.print(f"[warning]{escape(exc.message)}[/warning]")
        return None
    except Exception as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        return None


async def _switch_chat_project(state: _InteractiveChatState, project_id: str, *, restore_session: bool = True) -> None:
    if not project_id:
        console.print("[warning]Usage: /project switch <id>[/warning]")
        return
    if not is_valid_project_id(project_id):
        console.print("[warning]Invalid project id. Use letters, numbers, hyphens, and underscores.[/warning]")
        return
    if not _cli_project_exists(state.engine, project_id):
        console.print(f"[warning]Project does not exist: {project_id}[/warning]")
        return
    await state.engine.shutdown()
    engine, runtime_display = _create_cli_engine(state.config, project_id)
    await engine.initialize()
    state.engine = engine
    state.runtime_display = runtime_display
    state.session_to_task.clear()
    state.active_runtime_children.clear()
    state.runtime_control_state = ""
    state.runtime_control_task_id = ""
    state.runtime_control_session_id = ""
    state.runtime_control_checkpoint_id = ""
    state.org_id = ""
    state.native_permission_scope_id = ""
    _attach_cli_runtime_callbacks(state)
    console.print(f"[info]Switched to project: {project_id}[/info]")
    if not restore_session:
        state.session_id = ""
        state.native_permission_scope_id = ""
        return
    state.session_id, restored_latest = await _resolve_starting_session_id(engine)
    if restored_latest:
        await _restore_interactive_native_permission(state)
        console.print(f"[info]Restored recent session: {state.session_id}[/info]")
    else:
        state.native_approval_level = normalize_native_approval_level(
            state.config.autonomy.native_approval_level
        )
        state.native_permission_scope_id = state.session_id
        console.print(f"[info]Started a new session: {state.session_id}[/info]")


async def _create_and_switch_chat_project(state: _InteractiveChatState, project_id: str) -> bool:
    if not is_valid_project_id(project_id):
        console.print("[warning]Invalid project id. Use letters, numbers, hyphens, and underscores.[/warning]")
        return False
    payload = await _run_chat_office_service(
        state,
        lambda svc: svc.project.create(project_id, active_project_id=_current_project_id(state.engine)),
    )
    if not payload:
        return False
    console.print(f"[success]Created project: {payload.get('project_id') or project_id}[/success]")
    await _switch_chat_project(state, project_id, restore_session=False)
    return True


async def _choose_initial_project(state: _InteractiveChatState, *, explicit_project: bool = False) -> None:
    if explicit_project:
        console.print(f"[info]Project: {_current_project_id(state.engine)}[/info]")
        return
    while True:
        project_ids = _list_cli_project_ids(state.engine)
        _render_project_picker(state, project_ids)
        try:
            choice = console.input("[bold]Project[/bold] [current]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if not choice:
            console.print(f"[info]Project: {_current_project_id(state.engine)}[/info]")
            return
        lowered = choice.lower()
        if lowered in {"quit", "exit", "/quit", "/exit"}:
            raise KeyboardInterrupt
        if lowered.startswith("/project"):
            try:
                parts = _slash_parts(choice)
                project_args = parts[1:]
                before_project = _current_project_id(state.engine)
                await _handle_project_slash(state, project_args)
            except ValueError as exc:
                console.print(f"[warning]Could not parse command: {exc}[/warning]")
                continue
            subcommand = project_args[0].lower() if project_args else ""
            if subcommand in {"switch", "use"} and _current_project_id(state.engine) != before_project:
                return
            if len(project_args) == 1 and subcommand not in {"list", "ls", "create", "new", "delete", "rm"} and _current_project_id(state.engine) != before_project:
                return
            continue
        new_prefixes = ("new ", "create ", "+ ")
        if any(lowered.startswith(prefix) for prefix in new_prefixes):
            project_id = choice.split(maxsplit=1)[1].strip() if " " in choice else ""
            if not project_id:
                console.print("[warning]Usage: new <project_id>[/warning]")
                continue
            if await _create_and_switch_chat_project(state, project_id):
                return
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(project_ids):
                await _switch_chat_project(state, project_ids[idx - 1], restore_session=False)
                return
            console.print("[warning]Invalid project number.[/warning]")
            continue
        if choice in project_ids or _cli_project_exists(state.engine, choice):
            await _switch_chat_project(state, choice, restore_session=False)
            return
        console.print(f"[warning]Project not found: {choice}. Type `new {choice}` to create it.[/warning]")


async def _choose_initial_session(state: _InteractiveChatState) -> None:
    sessions = await _load_recent_primary_sessions(
        getattr(state.engine, "store", None),
        _current_project_id(state.engine),
        limit=20,
    )
    while True:
        _render_session_picker(sessions)
        try:
            choice = console.input("[bold]Session[/bold] [new]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        lowered = choice.lower()
        if lowered in {"quit", "exit", "/quit", "/exit"}:
            raise KeyboardInterrupt
        if not choice or lowered in {"new", "create", "+"}:
            _start_new_chat_session(state)
            return
        if lowered.startswith("/session"):
            try:
                await _handle_session_slash(state, _slash_parts(choice)[1:])
            except ValueError as exc:
                console.print(f"[warning]Could not parse command: {exc}[/warning]")
            if state.session_id:
                return
            sessions = await _load_recent_primary_sessions(
                getattr(state.engine, "store", None),
                _current_project_id(state.engine),
                limit=20,
            )
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(sessions):
                await _resume_chat_session(state, str(getattr(sessions[idx - 1], "session_id", "") or ""))
                return
            console.print("[warning]Invalid session number.[/warning]")
            continue
        matching = next((item for item in sessions if str(getattr(item, "session_id", "") or "") == choice), None)
        if matching:
            await _resume_chat_session(state, choice)
            return
        console.print(f"[warning]Session not found: {choice}. Type `new` to create a new session.[/warning]")


async def _run_interactive_startup_selector(state: _InteractiveChatState, *, explicit_project: bool = False) -> None:
    await _choose_initial_project(state, explicit_project=explicit_project)
    state.session_id = ""
    await _choose_initial_session(state)
    profile = f" company_profile={state.company_profile}" if state.mode == "company" else ""
    console.print(
        f"[success]Ready:[/success] project={_current_project_id(state.engine)} "
        f"session={state.session_id} mode={state.mode}{profile} "
        f"agent={execution_agent_label(state.preferred_agent) if state.preferred_agent else 'system'}"
    )
    _print_chat_hint()


async def _handle_project_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print(f"[info]Current project: {_current_project_id(state.engine)}[/info]")
        _render_project_list(state)
        console.print("[dim]Use /project list, /project switch <id>, /project <id>, /project create <id>, /project rename <old_id> <new_id>, or /project delete <id> --yes.[/dim]")
        return
    subcommand = args[0].lower()
    if subcommand in {"list", "ls"}:
        _render_project_list(state)
        return
    if subcommand in {"create", "new"}:
        if len(args) < 2:
            console.print("[warning]Usage: /project create <id>[/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.project.create(args[1], active_project_id=_current_project_id(state.engine)))
        if payload:
            console.print(f"[success]Created project: {payload.get('project_id')}[/success]")
        return
    if subcommand in {"rename", "mv"}:
        if len(args) < 3:
            console.print("[warning]Usage: /project rename <old_id> <new_id>[/warning]")
            return
        old_id, new_id = args[1], args[2]
        renaming_current = old_id == _current_project_id(state.engine)
        payload = await _run_chat_office_service(state, lambda svc: svc.project.rename(old_id, new_id))
        if payload:
            console.print(f"[success]Renamed project:[/success] {payload.get('old_project_id')} -> {payload.get('new_project_id') or payload.get('project_id')}")
            if renaming_current:
                await _switch_chat_project(state, new_id)
        return
    if subcommand in {"delete", "rm"}:
        if len(args) < 2:
            console.print("[warning]Usage: /project delete <id> --yes[/warning]")
            return
        remaining, ok = _require_yes_arg(args[2:], usage="/project delete <id> --yes")
        if remaining or not ok:
            return
        deleting_current = args[1] == _current_project_id(state.engine)
        payload = await _run_chat_office_service(state, lambda svc: svc.project.delete(args[1]))
        if payload:
            console.print(f"[success]Deleted project: {payload.get('project_id')}[/success]")
            if deleting_current:
                await _switch_chat_project(state, "default")
        return
    if subcommand in {"switch", "use"}:
        if len(args) < 2:
            console.print("[warning]Usage: /project switch <id>[/warning]")
            return
        await _switch_chat_project(state, args[1])
        return
    if len(args) == 1:
        await _switch_chat_project(state, args[0])
        return
    console.print("[warning]Usage: /project [list|create <id>|rename <old_id> <new_id>|switch <id>|delete <id> --yes][/warning]")


async def _render_sessions(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Session store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
        if args and args[0].isdigit():
            limit = _coerce_limit(args[0])
            args = args[1:]
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if args:
        console.print("[warning]Usage: /session list [limit][/warning]")
        return
    sessions = await state.engine.store.list_sessions(
        project_id=_current_project_id(state.engine),
        parent_session_id=None,
        limit=limit,
    )
    if not sessions:
        console.print("[info]No sessions found for this project.[/info]")
        return
    table = Table(title=f"Recent Sessions ({len(sessions)})")
    table.add_column("Session ID")
    table.add_column("Current", justify="center")
    table.add_column("Title")
    table.add_column("Mode")
    table.add_column("Status")
    table.add_column("Updated")
    current_session = str(state.session_id or "")
    for item in sessions:
        session_id = str(getattr(item, "session_id", "") or "")
        table.add_row(
            session_id,
            "*" if session_id == current_session else "",
            _clip_text(getattr(item, "title", "") or "(untitled)", 80, full=full),
            str(getattr(item, "mode", "") or ""),
            str(getattr(item, "status", "") or ""),
            _format_datetime(getattr(item, "updated_at", None)),
        )
    console.print(table)
    console.print("[dim]Switch with /session resume <session_id> or /session <session_id>. Create with /session new.[/dim]")


async def _resume_chat_session(state: _InteractiveChatState, session_id: str) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Session store is not available.[/warning]")
        return
    if not session_id:
        console.print("[warning]Usage: /session resume <session_id>[/warning]")
        return
    target = await state.engine.store.get_session(session_id)
    if not target:
        console.print(f"[warning]Session not found: {session_id}[/warning]")
        return
    current_project = _current_project_id(state.engine)
    target_project = str(getattr(target, "project_id", "") or "default")
    if target_project != current_project:
        console.print(
            "[warning]"
            f"Session {session_id} belongs to project '{target_project}'. "
            f"Switch project first with /project {target_project}."
            "[/warning]"
        )
        return
    state.session_id = session_id
    await _restore_interactive_native_permission(state)
    title = getattr(target, "title", "") or "(untitled)"
    console.print(f"[info]Resumed session: {state.session_id} :: {title}[/info]")


def _start_new_chat_session(state: _InteractiveChatState) -> None:
    state.session_id = str(uuid.uuid4())
    state.native_permission_scope_id = state.session_id
    state.native_approval_level = normalize_native_approval_level(
        state.config.autonomy.native_approval_level
    )
    approval_engine = getattr(state.engine, "approval_engine", None)
    native_policy = getattr(approval_engine, "native_policy", None)
    if native_policy is not None:
        native_policy.set_session_level(
            state.native_permission_scope_id,
            state.native_approval_level,
        )
    console.print(f"[info]Started a new session: {state.session_id}[/info]")


async def _restore_interactive_native_permission(
    state: _InteractiveChatState,
) -> NativeApprovalLevel:
    """Restore one session's durable level without consulting other sessions."""

    default = normalize_native_approval_level(
        state.config.autonomy.native_approval_level
    )
    level: NativeApprovalLevel = default
    permission_scope_id = state.session_id
    store = getattr(state.engine, "store", None)
    if store is not None and state.session_id:
        session = await store.get_session(state.session_id)
        session_metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
        raw = session_metadata.get("native_approval_level")
        permission_scope_id = str(
            session_metadata.get("native_permission_scope_id", "")
            or state.session_id
        ).strip()
        getter = getattr(store, "get_tasks", None)
        all_tasks = await getter(
            project_id=_current_project_id(state.engine),
        ) if callable(getter) else []
        selected_task = next(
            (
                task
                for task in all_tasks
                if str(getattr(task, "session_id", "") or "").strip()
                == state.session_id
            ),
            None,
        )
        if selected_task is not None:
            selected_metadata = dict(getattr(selected_task, "metadata", {}) or {})
            permission_scope_id = str(
                selected_metadata.get("native_permission_scope_id", "")
                or selected_metadata.get("root_session_id", "")
                or selected_metadata.get("company_runtime_root_session_id", "")
                or getattr(selected_task, "parent_session_id", "")
                or permission_scope_id
            ).strip()
        tasks = tasks_in_native_permission_scope(
            all_tasks,
            root_session_id=permission_scope_id or state.session_id,
            scope_id=permission_scope_id,
        )
        if raw in (None, ""):
            for task in tasks:
                task_metadata = dict(getattr(task, "metadata", {}) or {})
                raw = task_metadata.get("native_approval_level")
                permission_scope_id = str(
                    task_metadata.get("native_permission_scope_id", "")
                    or permission_scope_id
                ).strip()
                if raw not in (None, ""):
                    break
        level = normalize_native_approval_level(raw, default=default)
        # First restoration migrates old records and freezes the result so a
        # later default change cannot alter this session.
        if session is not None and (
            session_metadata.get("native_approval_level") != level
            or session_metadata.get("native_permission_scope_id") != permission_scope_id
        ):
            session.metadata = {
                **session_metadata,
                "native_approval_level": level,
                "native_permission_scope_id": permission_scope_id,
            }
            save_session = getattr(store, "save_session", None)
            if callable(save_session):
                await save_session(session)
        for task in tasks:
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            if (
                task_metadata.get("native_approval_level") == level
                and task_metadata.get("native_permission_scope_id") == permission_scope_id
            ):
                continue
            task.metadata = {
                **task_metadata,
                "native_approval_level": level,
                "native_permission_scope_id": permission_scope_id,
            }
            save_task = getattr(store, "save_task", None)
            if callable(save_task):
                await save_task(task)
    state.native_approval_level = level
    state.native_permission_scope_id = permission_scope_id
    approval_engine = getattr(state.engine, "approval_engine", None)
    native_policy = getattr(approval_engine, "native_policy", None)
    if native_policy is not None and permission_scope_id:
        native_policy.set_session_level(permission_scope_id, level)
    return level


async def _set_interactive_native_permission(
    state: _InteractiveChatState,
    level: str,
) -> NativeApprovalLevel:
    normalized = _parse_native_approval_level(level)
    state.native_approval_level = normalized
    permission_scope_id = str(
        state.native_permission_scope_id or state.session_id
    ).strip()
    store = getattr(state.engine, "store", None)
    if store is not None and state.session_id:
        session = await store.get_session(state.session_id)
        if session is not None:
            session.metadata = {
                **dict(getattr(session, "metadata", {}) or {}),
                "native_approval_level": normalized,
                "native_permission_scope_id": permission_scope_id,
            }
            await store.save_session(session)
        getter = getattr(store, "get_tasks", None)
        all_tasks = await getter(
            project_id=_current_project_id(state.engine),
        ) if callable(getter) else []
        tasks = tasks_in_native_permission_scope(
            all_tasks,
            root_session_id=state.session_id,
            scope_id=permission_scope_id,
        )
        for task in tasks:
            task.metadata = {
                **dict(getattr(task, "metadata", {}) or {}),
                "native_approval_level": normalized,
                "native_permission_scope_id": permission_scope_id,
            }
            await store.save_task(task)
    approval_engine = getattr(state.engine, "approval_engine", None)
    native_policy = getattr(approval_engine, "native_policy", None)
    if native_policy is not None and permission_scope_id:
        native_policy.set_session_level(permission_scope_id, normalized)
    return normalized


def _render_transcript_parts(parts: list[Any]) -> str:
    lines: list[str] = []
    for part in parts:
        part_type = getattr(part, "part_type", "")
        payload = getattr(part, "payload", {})
        if isinstance(part, dict):
            part_type = str(part.get("part_type", part_type) or "")
            payload = part.get("payload", payload)
        if not isinstance(payload, dict):
            payload = {}
        if part_type == "text":
            text = payload.get("text", "")
            if text:
                lines.append(str(text))
        elif part_type in {"subtask_result", "task_result"}:
            title = payload.get("task_title", "Task")
            summary = payload.get("summary", "")
            lines.append(f"{title}: {summary}".strip(": "))
    return "\n".join(line for line in lines if line).strip()


def _render_transcript_table(transcript: list[dict[str, Any]], *, limit: int) -> None:
    rows: list[tuple[str, str, str, str]] = []
    for item in transcript:
        message = item.get("message") if isinstance(item, dict) else None
        if not message or getattr(message, "summary_flag", False):
            continue
        content = _render_transcript_parts(item.get("parts", []))
        if not content:
            continue
        role = str(getattr(message, "role", "") or "").strip().lower()
        agent_id = str(getattr(message, "agent_id", "") or "").strip()
        sender = {
            "user": "You",
            "assistant": "SuperOnes",
            "system": "System",
            "subagent": agent_id.replace("_", " ").replace("-", " ").title() if agent_id else "Subagent",
        }.get(role, agent_id or role.title() or "SuperOnes")
        rows.append((
            _format_datetime(getattr(message, "created_at", None)),
            role or "assistant",
            sender,
            _clip_text(content),
        ))
    rows = rows[-limit:]
    if not rows:
        console.print("[info]No transcript messages found.[/info]")
        return
    table = Table(title=f"Transcript (last {len(rows)})")
    table.add_column("Created")
    table.add_column("Role")
    table.add_column("Sender")
    table.add_column("Content")
    for row in rows:
        table.add_row(*row)
    console.print(table)


async def _resolve_session_or_task(state: _InteractiveChatState, token: str) -> tuple[Any | None, Any | None, str]:
    store = getattr(state.engine, "store", None)
    if not store:
        return None, None, ""
    session = await store.get_session(token) if hasattr(store, "get_session") else None
    task = None
    session_id = str(getattr(session, "session_id", "") or "") if session else ""
    if not session and hasattr(store, "get_task"):
        task = await store.get_task(token)
        session_id = str(getattr(task, "session_id", "") or "") if task else ""
        if session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
    return session, task, session_id


async def _resolve_runtime_control_target(state: _InteractiveChatState, target: str = "") -> tuple[str, str]:
    from ampuly.layer2_organization.company_runtime_identity import load_company_runtime_identity_index

    store = _require_chat_store(state, label="Session store")
    if store is None:
        return "", ""
    raw_target = str(target or "").strip() or str(state.session_id or "").strip()
    if not raw_target:
        console.print("[warning]No current session. Use /session list or /session create first.[/warning]")
        return "", ""
    project_id = _current_project_id(state.engine)
    identity_index = await load_company_runtime_identity_index(store, project_id)
    task = await store.get_task(raw_target) if hasattr(store, "get_task") else None
    if task is not None:
        task_project_id = str(getattr(task, "project_id", "") or "default")
        if task_project_id != project_id:
            console.print(f"[warning]Target belongs to project '{task_project_id}'. Switch project first.[/warning]")
            return "", ""
        runtime_identity = identity_index.resolve(task_id=raw_target)
        if runtime_identity is not None:
            return raw_target, runtime_identity.runtime_session_id
        return str(getattr(task, "id", "") or ""), str(getattr(task, "session_id", "") or getattr(task, "parent_session_id", "") or "")
    runtime_identity = (
        identity_index.resolve(runtime_session_id=raw_target)
        or identity_index.resolve(task_session_id=raw_target)
    )
    if runtime_identity is not None:
        control_task_id = (
            runtime_identity.ui_anchor_task_id
            or runtime_identity.config_source_task_id
        )
        if control_task_id:
            return control_task_id, runtime_identity.runtime_session_id
    session = await store.get_session(raw_target) if hasattr(store, "get_session") else None
    if session is None:
        console.print(f"[warning]Task or session not found: {raw_target}[/warning]")
        return "", ""
    session_project_id = str(getattr(session, "project_id", "") or "default")
    if session_project_id != project_id:
        console.print(f"[warning]Target belongs to project '{session_project_id}'. Switch project first.[/warning]")
        return "", ""
    tasks = list(identity_index.tasks)
    candidates = [
        item for item in tasks
        if str(getattr(item, "session_id", "") or "") == raw_target
    ]
    if not candidates:
        console.print(f"[warning]Session is not task-backed: {raw_target}[/warning]")
        return "", raw_target
    task_mode_anchor = min(
        candidates,
        key=lambda item: (
            bool(str(getattr(item, "parent_session_id", "") or "")),
            str(getattr(item, "created_at", "") or ""),
            str(getattr(item, "id", "") or ""),
        ),
    )
    return str(getattr(task_mode_anchor, "id", "") or ""), raw_target


def _make_cli_runtime_control_context(state: _InteractiveChatState, controller: ChatTurnController | None = None) -> Any:
    from ampuly.plugins.office_ui.services import ModeState, OfficeServiceContext

    context = OfficeServiceContext(
        engine=state.engine,
        agent_store=None,
        chat_store=None,
        event_adapter=None,
        mode_state=ModeState(
            exec_mode=state.mode,
            company_profile=state.company_profile,
            task_preferred_agent=state.preferred_agent or "native",
        ),
    )
    context.session_to_task = state.session_to_task
    context.active_runtime_children = state.active_runtime_children

    def cancel_session_tasks(_task_id: str) -> None:
        if controller is not None and controller.active_task is not None and not controller.active_task.done():
            controller.active_task.cancel()

    context.cancel_session_tasks = cancel_session_tasks
    return context


async def _company_runtime_identity_for_session(
    state: _InteractiveChatState,
    session_id: str | None = None,
) -> Any | None:
    from ampuly.layer2_organization.company_runtime_identity import (
        load_company_runtime_identity_index,
    )

    runtime_session_id = str(session_id or state.session_id or "").strip()
    store = getattr(state.engine, "store", None)
    if not runtime_session_id or store is None:
        return None
    try:
        identity_index = await load_company_runtime_identity_index(
            store,
            _current_project_id(state.engine),
        )
        # The interactive session may be a role/work-item session.  Resolve it
        # as a task session after trying the canonical root session so both UI
        # roots and child channels converge on the same company runtime.
        identity = (
            identity_index.resolve(runtime_session_id=runtime_session_id)
            or identity_index.resolve(task_session_id=runtime_session_id)
        )
    except Exception:
        return None
    return identity


async def _latest_company_suspend_checkpoint(
    state: _InteractiveChatState,
    session_id: str | None = None,
) -> Any | None:
    identity = await _company_runtime_identity_for_session(state, session_id)
    return identity.checkpoint if identity is not None and identity.resumable else None


async def _company_runtime_execution_identity(
    state: _InteractiveChatState,
    runtime_identity: Any,
) -> Any:
    """Read execution configuration from the durable runtime config source."""
    from ampuly.plugins.office_ui.execution_identity import execution_identity_from_task

    config_source_task_id = str(
        getattr(runtime_identity, "config_source_task_id", "") or ""
    ).strip()
    store = getattr(state.engine, "store", None)
    if not config_source_task_id or store is None or not hasattr(store, "get_task"):
        raise RuntimeError("Company runtime has no durable configuration source.")
    config_task = await store.get_task(config_source_task_id)
    if config_task is None:
        raise RuntimeError("Company runtime configuration source no longer exists.")
    return execution_identity_from_task(
        config_task,
        default_exec_mode=state.mode,
        default_company_profile=state.company_profile,
        default_preferred_agent=state.preferred_agent or "native",
        default_org_id=state.org_id,
    )


async def _handle_stop_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    if len(args) > 1:
        console.print("[warning]Usage: /stop [task_id|session_id][/warning]")
        return
    target = args[0] if args else ""
    task_id, session_id = await _resolve_runtime_control_target(state, target)
    if not task_id:
        return
    from ampuly.plugins.office_ui.services import OfficeServices, ServiceError

    context = _make_cli_runtime_control_context(state, controller)
    try:
        result = await OfficeServices(context).session.stop(project_id=_current_project_id(state.engine), target=target or session_id or task_id)
    except ServiceError as exc:
        console.print(f"[warning]{escape(exc.message)}[/warning]")
        return
    payload = dict(result.payload)
    state.runtime_control_state = str(payload.get("runtime_control_state") or payload.get("status") or "stopped")
    state.runtime_control_task_id = str(payload.get("task_id") or task_id)
    state.runtime_control_session_id = str(payload.get("resume_parent_session_id") or payload.get("session_id") or session_id)
    state.runtime_control_checkpoint_id = str(payload.get("checkpoint_id") or "")
    console.print("[success]Stopped.[/success] [dim]Send a message to revise, or /continue to resume.[/dim]")


async def _runtime_control_execution_identity(state: _InteractiveChatState, task_id: str) -> Any:
    from ampuly.layer2_organization.company_runtime_identity import load_company_runtime_identity_index
    from ampuly.plugins.office_ui.execution_identity import execution_identity_from_task

    store = getattr(state.engine, "store", None)
    task = None
    if store is not None and task_id:
        try:
            identity_index = await load_company_runtime_identity_index(
                store,
                _current_project_id(state.engine),
            )
            runtime_identity = identity_index.resolve(task_id=task_id)
            task = identity_index.task(
                runtime_identity.config_source_task_id if runtime_identity is not None else task_id
            )
        except Exception:
            task = None
        if task is None and hasattr(store, "get_task"):
            try:
                task = await store.get_task(task_id)
            except Exception:
                task = None
    return execution_identity_from_task(
        task,
        default_exec_mode=state.mode,
        default_company_profile=state.company_profile,
        default_preferred_agent=state.preferred_agent or "native",
        default_org_id=state.org_id,
    )


async def _handle_continue_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    target = ""
    message_parts = list(args)
    if message_parts:
        first = message_parts[0]
        store = getattr(state.engine, "store", None)
        is_target = False
        if store is not None:
            try:
                is_target = bool((hasattr(store, "get_task") and await store.get_task(first)) or (hasattr(store, "get_session") and await store.get_session(first)))
            except Exception:
                is_target = False
        if is_target:
            target = message_parts.pop(0)
    task_id, session_id = await _resolve_runtime_control_target(state, target)
    if not task_id:
        return
    identity = await _runtime_control_execution_identity(state, task_id)
    content = " ".join(message_parts).strip() or "Resume the existing runtime."
    checkpoint = await _latest_company_suspend_checkpoint(state, session_id)
    if identity.exec_mode in {"company", "org", "custom"} and checkpoint is None:
        console.print(
            "[warning]No suspended or interrupted company runtime is available to continue.[/warning]"
        )
        return
    if controller is not None and controller.is_busy:
        if checkpoint is None and state.runtime_control_state not in {"suspended", "stopped"}:
            console.print("[warning]Busy: wait for the current turn or /stop it before /continue.[/warning]")
            return
    metadata: dict[str, Any] = {"ui_force_resume": True}
    if checkpoint is not None:
        metadata["response_to_checkpoint_id"] = str(getattr(checkpoint, "checkpoint_id", "") or "")
        metadata["response_to_checkpoint_type"] = str(
            getattr(checkpoint, "checkpoint_type", "") or "company_runtime_suspended"
        )
    item = QueuedChatInput(
        text=content,
        project_id=_current_project_id(state.engine),
        session_id=session_id or state.session_id,
        mode=identity.exec_mode,
        company_profile=identity.company_profile,
        org_id=identity.org_id,
        preferred_agent=identity.preferred_agent,
        domains=list(state.domains),
        message_metadata=metadata,
    )
    state.runtime_control_state = "resuming"
    state.runtime_control_task_id = task_id
    state.runtime_control_session_id = session_id or state.session_id
    state.runtime_control_checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "")
    if controller is not None:
        await controller.submit_item(item)
    else:
        previous_session_id = state.session_id
        previous_mode = state.mode
        previous_profile = state.company_profile
        previous_org_id = state.org_id
        previous_agent = state.preferred_agent
        state.session_id = session_id or state.session_id
        state.mode = identity.exec_mode
        state.company_profile = identity.company_profile
        state.org_id = identity.org_id
        state.preferred_agent = identity.preferred_agent
        try:
            await _process_interactive_chat_message(state, content, message_metadata=metadata)
        finally:
            state.session_id = previous_session_id
            state.mode = previous_mode
            state.company_profile = previous_profile
            state.org_id = previous_org_id
            state.preferred_agent = previous_agent


async def _show_session_detail(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Session store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if not args:
        console.print("[warning]Usage: /session show <session_id|task_id> [--limit N] [--full][/warning]")
        return
    session, task, session_id = await _resolve_session_or_task(state, args[0])
    if not session_id:
        console.print(f"[warning]Session or task not found: {args[0]}[/warning]")
        return
    project_id = str(getattr(session, "project_id", None) or getattr(task, "project_id", None) or "default")
    if project_id != _current_project_id(state.engine):
        console.print(f"[warning]Target belongs to project '{project_id}'. Switch project first.[/warning]")
        return
    table = Table(title=f"Session {session_id}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Session ID", session_id)
    table.add_row("Project", project_id)
    if task:
        table.add_row("Task", str(getattr(task, "id", "") or ""))
        table.add_row("Task title", _clip_text(getattr(task, "title", "") or "(untitled)", 120, full=full))
    if session:
        table.add_row("Title", _clip_text(getattr(session, "title", "") or "(untitled)", 120, full=full))
        table.add_row("Mode", str(getattr(session, "mode", "") or ""))
        table.add_row("Status", str(getattr(session, "status", "") or ""))
        table.add_row("Updated", _format_datetime(getattr(session, "updated_at", None)))
    console.print(table)
    transcript = await state.engine.store.get_session_transcript(session_id)
    _render_transcript_table(transcript, limit=limit)


async def _handle_session_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    if not args:
        console.print(f"[info]Current session: {state.session_id}[/info]")
        await _render_sessions(state, [])
        console.print("[dim]Use /session list, /session resume <session_id>, /session <session_id>, /session new, /session create [title], or /session delete <task_id> --yes.[/dim]")
        return
    subcommand = args[0].lower()
    rest = args[1:]
    known_subcommands = {
        "list", "ls", "new", "create", "resume", "show", "config", "send", "rename", "delete", "stop", "continue", "complete",
    }
    if len(args) == 1 and subcommand not in known_subcommands:
        await _resume_chat_session(state, args[0])
        return
    if subcommand in {"list", "ls"}:
        await _render_sessions(state, rest)
        return
    if subcommand == "new":
        _start_new_chat_session(state)
        return
    if subcommand == "create":
        try:
            rest, mode = _extract_option(rest, "--mode", default=state.mode)
            rest, profile = _extract_option(rest, "--company-profile", default=state.company_profile)
            rest, agent = _extract_option(rest, "--agent", default=state.preferred_agent)
            rest, org_id = _extract_option(rest, "--org", default=None)
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        title = " ".join(rest).strip() or "New Chat"
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.session.create(
                project_id=_current_project_id(state.engine),
                title=title,
                exec_mode=mode,
                company_profile=profile,
                preferred_agent=agent,
                org_id=org_id,
                native_approval_level=state.native_approval_level,
                interface="cli",
            ),
        )
        if payload:
            state.session_id = str(payload.get("session_id") or state.session_id)
            state.native_approval_level = normalize_native_approval_level(
                payload.get("native_approval_level"),
                default=state.native_approval_level,
            )
            state.native_permission_scope_id = str(
                payload.get("native_permission_scope_id") or state.session_id
            )
            console.print(f"[success]Created session:[/success] {state.session_id} task={payload.get('task_id')}")
        return
    if subcommand == "resume":
        await _resume_chat_session(state, rest[0] if rest else "")
        return
    if subcommand == "show":
        await _show_session_detail(state, rest)
        return
    if subcommand == "config":
        if not rest:
            console.print("[warning]Usage: /session config <task_id> [--mode ...] [--agent ...] [--org ...] [--approval-level ...][/warning]")
            return
        task_id = rest[0]
        opts = rest[1:]
        try:
            opts, mode = _extract_option(opts, "--mode", default=None)
            opts, profile = _extract_option(opts, "--company-profile", default=None)
            opts, agent = _extract_option(opts, "--agent", default=None)
            opts, org_id = _extract_option(opts, "--org", default=None)
            opts, approval_level = _extract_option(opts, "--approval-level", default=None)
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if opts:
            console.print("[warning]Usage: /session config <task_id> [--mode ...] [--agent ...] [--org ...] [--approval-level ...][/warning]")
            return
        native_level = (
            _parse_native_approval_level(approval_level)
            if approval_level is not None
            else None
        )
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.session.update_config(
                project_id=_current_project_id(state.engine),
                task_id=task_id,
                exec_mode=mode,
                company_profile=profile,
                preferred_agent=agent,
                org_id=org_id,
                native_approval_level=native_level,
            ),
        )
        if payload:
            console.print(f"[success]Updated session config:[/success] {task_id}")
        return
    if subcommand == "send":
        if len(rest) < 2:
            console.print("[warning]Usage: /session send <task_id> <message>[/warning]")
            return
        task_id = rest[0]
        message = " ".join(rest[1:]).strip()
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.session.send(
                project_id=_current_project_id(state.engine),
                task_id=task_id,
                content=message,
                mode=state.mode,
                company_profile=state.company_profile,
                preferred_agent=state.preferred_agent,
                domains=list(state.domains),
            ),
        )
        if payload and payload.get("response"):
            _print_response(str(payload["response"]), state.no_markdown)
        return
    if subcommand == "rename":
        if len(rest) < 2:
            console.print("[warning]Usage: /session rename <task_id|session_id> <title>[/warning]")
            return
        await _rename_task_and_session(state, rest[0], " ".join(rest[1:]))
        return
    if subcommand == "delete":
        if not rest:
            console.print("[warning]Usage: /session delete <task_id> --yes[/warning]")
            return
        await _delete_task_and_session(state, rest[0], rest[1:], usage="/session delete <task_id> --yes")
        return
    if subcommand == "stop":
        await _handle_stop_slash(state, rest, controller=controller)
        return
    if subcommand == "continue":
        await _handle_continue_slash(state, rest, controller=controller)
        return
    if subcommand == "complete":
        if not rest:
            console.print("[warning]Usage: /session complete <task_id>[/warning]")
            return
        def operation(svc):
            return svc.session.complete(
                project_id=_current_project_id(state.engine),
                task_id=rest[0],
            )

        payload = await _run_chat_office_service(state, operation)
        if payload:
            console.print(f"[success]Completed session:[/success] {rest[0]}")
        return
    console.print("[warning]Usage: /session [list|new|create|resume <session_id>|show <session_id|task_id>|config <task_id>|send <task_id> <message>|rename <task_id|session_id> <title>|delete <task_id> --yes|stop [target]|continue [target] [message]|complete <task_id>][/warning]")


def _task_title_for_view(task: Any, *, full: bool = False) -> str:
    return _clip_text(getattr(task, "title", "") or getattr(task, "description", "") or "(untitled)", 80, full=full)


def _require_chat_store(state: _InteractiveChatState, *, label: str = "Task store") -> Any | None:
    store = getattr(state.engine, "store", None)
    if not store:
        console.print(f"[warning]{label} is not available.[/warning]")
        return None
    return store


async def _get_task_for_current_project(state: _InteractiveChatState, task_id: str) -> Any | None:
    store = _require_chat_store(state)
    if store is None:
        return None
    if not task_id:
        console.print("[warning]Missing task id.[/warning]")
        return None
    task = await store.get_task(task_id)
    if not task:
        console.print(f"[warning]Task not found: {task_id}[/warning]")
        return None
    project_id = str(getattr(task, "project_id", "") or "default")
    if project_id != _current_project_id(state.engine):
        console.print(f"[warning]Task belongs to project '{project_id}'. Switch project first with /project {project_id}.[/warning]")
        return None
    return task


def _parse_delete_yes(args: list[str], *, usage: str) -> bool:
    unknown = [arg for arg in args if arg != "--yes"]
    if unknown:
        console.print(f"[warning]Usage: {usage}[/warning]")
        return False
    if "--yes" not in args:
        console.print(f"[warning]Destructive command requires --yes. Usage: {usage}[/warning]")
        return False
    return True


def _extract_option(args: list[str], name: str, *, default: str | None = None) -> tuple[list[str], str | None]:
    remaining: list[str] = []
    value = default
    idx = 0
    prefix = f"{name}="
    while idx < len(args):
        token = args[idx]
        if token == name:
            if idx + 1 >= len(args):
                raise ValueError(f"Missing value for {name}.")
            value = args[idx + 1]
            idx += 2
            continue
        if token.startswith(prefix):
            value = token.split("=", 1)[1]
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    return remaining, value


def _require_yes_arg(args: list[str], *, usage: str) -> tuple[list[str], bool]:
    remaining = [arg for arg in args if arg != "--yes"]
    if "--yes" not in args:
        console.print(f"[warning]Destructive command requires --yes. Usage: {usage}[/warning]")
        return remaining, False
    return remaining, True


def _save_cli_config(state: _InteractiveChatState) -> None:
    state.config.save(get_ampuly_home() / "config")


def _refresh_cli_org_runtime(state: _InteractiveChatState) -> None:
    engine = state.engine
    if hasattr(engine, "config"):
        engine.config = state.config
    market = getattr(engine, "talent_market", None)
    if market is not None and hasattr(market, "config"):
        market.config = state.config
    org_engine = getattr(engine, "org_engine", None)
    if org_engine is None:
        return
    try:
        if hasattr(org_engine, "config"):
            org_engine.config = state.config
        reload_from_config = getattr(org_engine, "reload_from_config", None)
        if callable(reload_from_config):
            reload_from_config()
        configure_tools = getattr(org_engine, "configure_task_mode_tools", None)
        tool_names = getattr(engine, "_task_mode_tool_names", None)
        if callable(configure_tools) and callable(tool_names):
            configure_tools(tool_names())
    except Exception as exc:
        console.print(f"[warning]Saved config, but current org runtime refresh failed: {exc}. Restart ampuly chat if needed.[/warning]")


def _coerce_task_status(value: Any) -> Any | None:
    from ampuly.core.models import TaskStatus

    raw = value.value if hasattr(value, "value") else str(value or "")
    try:
        return TaskStatus(raw)
    except ValueError:
        return None


async def _transition_task_status(
    state: _InteractiveChatState,
    *,
    task_id: str,
    target_status: Any,
    reason: str,
    target_label: str,
) -> None:
    from ampuly.core.models import TaskStatus
    from ampuly.layer2_organization.work_item_transition import apply_task_status_transition

    store = _require_chat_store(state)
    if store is None:
        return
    task = await _get_task_for_current_project(state, task_id)
    if task is None:
        return

    old_status = _coerce_task_status(getattr(task, "status", None))
    terminal_statuses = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    if old_status in terminal_statuses and target_status not in terminal_statuses:
        old_label = old_status.value if old_status else _value_text(getattr(task, "status", ""))
        console.print(f"[warning]Cannot move terminal task {old_label} back to {target_label}.[/warning]")
        return

    old_label = _value_text(getattr(task, "status", ""))
    try:
        await apply_task_status_transition(
            store,
            task,
            target_status_or_phase=target_status,
            reason=reason,
        )
    except Exception as exc:
        console.print(f"[error]Failed to transition task {task_id}: {exc}[/error]")
        return
    console.print(f"[success]Task {task_id} moved: {old_label} -> {target_status.value}[/success]")


async def _rename_task_and_session(
    state: _InteractiveChatState,
    target: str,
    title: str,
    *,
    usage: str = "/session rename <task_id|session_id> <title>",
) -> None:
    store = _require_chat_store(state)
    if store is None:
        return
    title = str(title or "").strip()
    if not title:
        console.print(f"[warning]Usage: {usage}[/warning]")
        return
    target = str(target or "").strip()
    if not target:
        console.print(f"[warning]Usage: {usage}[/warning]")
        return
    current_project = _current_project_id(state.engine)
    task = await store.get_task(target) if hasattr(store, "get_task") else None
    if task and str(getattr(task, "project_id", "") or "default") != current_project:
        console.print(f"[warning]Task belongs to project '{getattr(task, 'project_id', '')}'. Switch project first with /project {getattr(task, 'project_id', '')}.[/warning]")
        return
    session = None
    session_id = ""
    if task is None and hasattr(store, "get_session"):
        session = await store.get_session(target)
        if session and str(getattr(session, "project_id", "") or "default") != current_project:
            console.print(f"[warning]Session belongs to project '{getattr(session, 'project_id', '')}'. Switch project first with /project {getattr(session, 'project_id', '')}.[/warning]")
            return
        if session and hasattr(store, "get_tasks"):
            session_id = str(getattr(session, "session_id", "") or target)
            tasks = await store.get_tasks(project_id=current_project)
            task = next((item for item in tasks if str(getattr(item, "session_id", "") or "") == session_id), None)
    if task is None and session is None:
        console.print(f"[warning]Task or session not found: {target}[/warning]")
        return
    if task is not None:
        if not hasattr(store, "save_task"):
            console.print("[warning]Task store cannot save tasks.[/warning]")
            return
        task.title = title
        await store.save_task(task)
        session_id = str(getattr(task, "session_id", "") or session_id)
    memory = getattr(state.engine, "memory", None)
    if session_id and memory and hasattr(memory, "update_session_title"):
        await memory.update_session_title(session_id, title)
    elif session is not None and hasattr(store, "save_session"):
        session.title = title
        await store.save_session(session)
        session_id = str(getattr(session, "session_id", "") or session_id)
    task_id = str(getattr(task, "id", "") or "") if task is not None else ""
    console.print(f"[success]Renamed {f'task {task_id}' if task_id else 'session'}{f' / session {session_id}' if session_id else ''}: {title}[/success]")


async def _delete_task_and_session(state: _InteractiveChatState, task_id: str, args: list[str], *, usage: str) -> None:
    store = _require_chat_store(state)
    if store is None:
        return
    if not _parse_delete_yes(args, usage=usage):
        return
    if not hasattr(store, "hard_delete_task"):
        console.print("[warning]Task store cannot hard-delete tasks.[/warning]")
        return
    task = await _get_task_for_current_project(state, task_id)
    if task is None:
        return
    session_id = str(getattr(task, "session_id", "") or "")
    await store.hard_delete_task(task_id, session_id or None)
    console.print(f"[success]Deleted task {task_id}{f' / session {session_id}' if session_id else ''}.[/success]")


async def _handle_task_move_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if len(args) != 2:
        console.print("[warning]Usage: /task move <task_id> todo|in-progress|done|blocked|failed|cancelled[/warning]")
        return
    from ampuly.presentation.kanban import column_to_task_status

    target_label = args[1].strip().lower().replace("_", "-")
    target_status = column_to_task_status(target_label)
    if target_status is None:
        console.print("[warning]Unsupported target. Use todo, in-progress, done, blocked, failed, or cancelled.[/warning]")
        return
    await _transition_task_status(
        state,
        task_id=args[0],
        target_status=target_status,
        reason="cli_chat_task_move",
        target_label=target_label,
    )


async def _handle_task_done_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if len(args) != 1:
        console.print("[warning]Usage: /task done <task_id>[/warning]")
        return
    from ampuly.core.models import TaskStatus

    await _transition_task_status(
        state,
        task_id=args[0],
        target_status=TaskStatus.DONE,
        reason="cli_chat_task_done",
        target_label="done",
    )


async def _handle_tasks_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Task store is not available.[/warning]")
        return
    from ampuly.core.models import TaskStatus

    try:
        args, limit, full = _parse_view_args(args)
        if args and args[0].isdigit():
            limit = _coerce_limit(args[0])
            args = args[1:]
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    status = None
    if args:
        status_token = args[0].strip().lower().replace("-", "_")
        try:
            status = TaskStatus(status_token)
        except ValueError:
            valid = ", ".join(item.value for item in TaskStatus)
            console.print(f"[warning]Unknown task status '{args[0]}'. Valid: {valid}[/warning]")
            return
    tasks = await state.engine.store.get_tasks(project_id=_current_project_id(state.engine), status=status)
    tasks = tasks[:limit]
    if not tasks:
        console.print("[info]No tasks found for this project.[/info]")
        return
    table = Table(title=f"Tasks ({len(tasks)})")
    table.add_column("Task ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Priority", justify="right")
    table.add_column("Assigned")
    table.add_column("Session ID")
    table.add_column("Created")
    for task in tasks:
        table.add_row(
            str(getattr(task, "id", "") or ""),
            _task_title_for_view(task, full=full),
            _value_text(getattr(task, "status", "")),
            str(getattr(task, "priority", "") or ""),
            str(getattr(task, "assigned_to", "") or ""),
            str(getattr(task, "session_id", "") or ""),
            _format_datetime(getattr(task, "created_at", None)),
        )
    console.print(table)


async def _show_task_detail(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Task store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if not args:
        console.print("[warning]Usage: /task show <task_id> [--limit N] [--full][/warning]")
        return
    task = await state.engine.store.get_task(args[0])
    if not task:
        console.print(f"[warning]Task not found: {args[0]}[/warning]")
        return
    project_id = str(getattr(task, "project_id", "") or "default")
    if project_id != _current_project_id(state.engine):
        console.print(f"[warning]Task belongs to project '{project_id}'. Switch project first.[/warning]")
        return
    table = Table(title=f"Task {getattr(task, 'id', '')}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Title", _clip_text(getattr(task, "title", "") or "(untitled)", 120, full=full))
    table.add_row("Description", _clip_text(getattr(task, "description", "") or "", 240, full=full))
    table.add_row("Status", _value_text(getattr(task, "status", "")))
    table.add_row("Project", project_id)
    table.add_row("Session", str(getattr(task, "session_id", "") or ""))
    table.add_row("Assignee", str(getattr(task, "assigned_to", "") or ""))
    table.add_row("Priority", str(getattr(task, "priority", "") or ""))
    tags = getattr(task, "tags", []) or []
    table.add_row("Tags", ", ".join(str(tag) for tag in tags) if tags else "")
    result = getattr(task, "result", None) or {}
    if isinstance(result, dict):
        table.add_row("Result", _clip_text(result.get("content", "") or result.get("summary", ""), 240, full=full))
        artifacts = result.get("artifacts", []) or []
        if not isinstance(artifacts, list):
            artifacts = [artifacts]
        table.add_row("Artifacts", _clip_text(", ".join(str(item) for item in artifacts), 240, full=full))
    metadata = getattr(task, "metadata", {}) or {}
    table.add_row("Metadata keys", ", ".join(sorted(str(key) for key in metadata.keys())) if isinstance(metadata, dict) else "")
    console.print(table)
    session_id = str(getattr(task, "session_id", "") or "")
    if session_id:
        transcript = await state.engine.store.get_session_transcript(session_id)
        _render_transcript_table(transcript, limit=limit)


async def _handle_task_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /task [show|move|done|rename|delete] ...[/warning]")
        return
    subcommand = args[0].lower()
    if subcommand == "show":
        await _show_task_detail(state, args[1:])
        return
    if subcommand == "move":
        await _handle_task_move_slash(state, args[1:])
        return
    if subcommand == "done":
        await _handle_task_done_slash(state, args[1:])
        return
    if subcommand == "rename":
        rest = args[1:]
        if len(rest) < 2:
            console.print("[warning]Usage: /task rename <task_id> <title>[/warning]")
            return
        await _rename_task_and_session(state, rest[0], " ".join(rest[1:]), usage="/task rename <task_id> <title>")
        return
    if subcommand == "delete":
        rest = args[1:]
        if not rest:
            console.print("[warning]Usage: /task delete <task_id> --yes[/warning]")
            return
        await _delete_task_and_session(state, rest[0], rest[1:], usage="/task delete <task_id> --yes")
        return
    await _show_task_detail(state, args)


async def _handle_checkpoints_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Checkpoint store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if args:
        console.print("[warning]Usage: /checkpoints [--limit N] [--full][/warning]")
        return
    checkpoints = await state.engine.store.get_pending_checkpoints(project_id=_current_project_id(state.engine))
    checkpoints = checkpoints[:limit]
    if not checkpoints:
        console.print("[info]No pending checkpoints for this project.[/info]")
        return
    table = Table(title=f"Pending Checkpoints ({len(checkpoints)})")
    table.add_column("Checkpoint ID")
    table.add_column("Type")
    table.add_column("Task ID")
    table.add_column("Session ID")
    table.add_column("Updated")
    table.add_column("Payload")
    for checkpoint in checkpoints:
        table.add_row(
            str(getattr(checkpoint, "checkpoint_id", "") or ""),
            str(getattr(checkpoint, "checkpoint_type", "") or ""),
            str(getattr(checkpoint, "task_id", "") or ""),
            str(getattr(checkpoint, "session_id", "") or ""),
            _format_datetime(getattr(checkpoint, "updated_at", None)),
            _json_summary(getattr(checkpoint, "payload", {}) or {}, full=full),
        )
    console.print(table)


def _render_org_slash(state: _InteractiveChatState) -> None:
    org = state.config.org
    summary = Table(title="Organization", show_header=False)
    summary.add_column("Field", style="cyan", no_wrap=True)
    summary.add_column("Value")
    summary.add_row("Organization", str(getattr(org, "organization_name", "") or getattr(org, "company_name", "") or ""))
    summary.add_row("Company", str(getattr(org, "company_name", "") or ""))
    summary.add_row("Default mode", str(getattr(org, "default_mode", "") or ""))
    summary.add_row("Company profile", str(getattr(org, "company_profile", "") or ""))
    summary.add_row("Topology", str(getattr(org, "topology", "") or ""))
    summary.add_row("Execution model", str(getattr(org, "execution_model", "") or ""))
    summary.add_row("Final decider", str(getattr(org, "final_decider_role_id", "") or ""))
    console.print(summary)

    roles = list(getattr(org, "roles", []) or [])
    if roles:
        table = Table(title=f"Roles ({len(roles)})")
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Reports To")
        table.add_column("Type")
        table.add_column("Agent")
        table.add_column("Responsibility")
        for role in roles:
            table.add_row(
                str(getattr(role, "id", "") or ""),
                str(getattr(role, "name", "") or ""),
                str(getattr(role, "reports_to", "") or ""),
                str(getattr(role, "role_type", "") or ""),
                str(getattr(role, "preferred_external_agent", "") or ""),
                _clip_text(getattr(role, "responsibility", "") or "", 120),
            )
        console.print(table)

    employees = list(getattr(org, "employees", []) or [])
    if employees:
        table = Table(title=f"Employees ({len(employees)})")
        table.add_column("Employee ID")
        table.add_column("Name")
        table.add_column("Role")
        table.add_column("Template")
        table.add_column("Status")
        table.add_column("Agent")
        for employee in employees:
            table.add_row(
                str(getattr(employee, "employee_id", "") or ""),
                str(getattr(employee, "name", "") or ""),
                str(getattr(employee, "role_id", "") or ""),
                str(getattr(employee, "template_id", "") or ""),
                str(getattr(employee, "status", "") or ""),
                str(getattr(employee, "preferred_external_agent", "") or ""),
            )
        console.print(table)

    teams = list(getattr(org, "teams", []) or [])
    if teams:
        table = Table(title=f"Teams ({len(teams)})")
        table.add_column("Team ID")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("Seat IDs")
        for team in teams:
            table.add_row(
                str(getattr(team, "team_id", "") or ""),
                str(getattr(team, "name", "") or ""),
                _clip_text(getattr(team, "description", "") or "", 100),
                ", ".join(str(item) for item in (getattr(team, "seat_ids", []) or [])),
            )
        console.print(table)

        seats = []
        for team in teams:
            for seat in getattr(team, "seats", []) or []:
                seats.append((team, seat))
        if seats:
            seat_table = Table(title=f"Seats ({len(seats)})")
            seat_table.add_column("Team")
            seat_table.add_column("Seat ID")
            seat_table.add_column("Name")
            seat_table.add_column("Role")
            seat_table.add_column("Manager")
            seat_table.add_column("Kind")
            for team, seat in seats:
                seat_table.add_row(
                    str(getattr(team, "team_id", "") or ""),
                    str(getattr(seat, "seat_id", "") or ""),
                    str(getattr(seat, "name", "") or ""),
                    str(getattr(seat, "role_id", "") or ""),
                    str(getattr(seat, "manager_role_id", "") or getattr(seat, "manager_seat_id", "") or ""),
                    str(getattr(seat, "seat_kind", "") or ""),
                )
            console.print(seat_table)


async def _handle_org_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args or args[0].lower() in {"info", "show"}:
        if not args:
            _render_org_slash(state)
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.org.info())
        if payload:
            _emit_payload(payload)
        return
    command = args[0].lower()
    rest = args[1:]

    if command == "role":
        if not rest:
            console.print("[warning]Usage: /org role add|update|delete|bulk-add ...[/warning]")
            return
        action = rest[0].lower()
        values = rest[1:]
        if action == "add":
            try:
                values, name = _extract_option(values, "--name")
                values, responsibility = _extract_option(values, "--responsibility", default="")
                values, reports_to = _extract_option(values, "--reports-to", default="owner")
            except ValueError as exc:
                console.print(f"[warning]{exc}[/warning]")
                return
            if len(values) != 1:
                console.print("[warning]Usage: /org role add <role_id> [--name ...] [--responsibility ...] [--reports-to ...][/warning]")
                return
            role_id = values[0]
            payload = await _run_chat_office_service(
                state,
                lambda svc: svc.org.add_role({
                    "role_id": role_id,
                    "name": name or role_id,
                    "responsibility": responsibility or "",
                    "reports_to": reports_to or "owner",
                }),
            )
        elif action == "update":
            try:
                values, name = _extract_option(values, "--name")
                values, responsibility = _extract_option(values, "--responsibility")
                values, reports_to = _extract_option(values, "--reports-to")
                values, can_spawn = _extract_option(values, "--can-spawn")
                values, tools = _extract_option(values, "--tools")
                values, agent = _extract_option(values, "--agent")
            except ValueError as exc:
                console.print(f"[warning]{exc}[/warning]")
                return
            if len(values) != 1:
                console.print("[warning]Usage: /org role update <role_id> [--name ...] [--responsibility ...][/warning]")
                return
            updates = {
                key: value
                for key, value in {
                    "name": name,
                    "responsibility": responsibility,
                    "reports_to": reports_to,
                    "preferred_external_agent": agent,
                }.items()
                if value is not None
            }
            if can_spawn is not None:
                updates["can_spawn"] = _split_csv(can_spawn)
            if tools is not None:
                updates["tools"] = _split_csv(tools)
            payload = await _run_chat_office_service(state, lambda svc: svc.org.update_role(values[0], updates))
        elif action == "delete":
            if not values:
                console.print("[warning]Usage: /org role delete <role_id> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(values[1:], usage="/org role delete <role_id> --yes")
            if not ok or remaining:
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.org.delete_role(values[0]))
        elif action == "bulk-add":
            if len(values) != 1:
                console.print("[warning]Usage: /org role bulk-add <json-or-yaml-file>[/warning]")
                return
            loaded = _load_structured_payload(file_path=values[0])
            roles = loaded.get("roles", []) if isinstance(loaded, dict) else loaded if isinstance(loaded, list) else []
            payload = await _run_chat_office_service(state, lambda svc: svc.org.bulk_add_roles(list(roles or [])))
        else:
            console.print("[warning]Usage: /org role add|update|delete|bulk-add ...[/warning]")
            return
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "policy" and rest[:1] == ["update"]:
        values = rest[1:]
        try:
            values, payload_text = _extract_option(values, "--payload")
            values, file_path = _extract_option(values, "--file")
            values, profile = _extract_option(values, "--profile", default="custom")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if values:
            console.print("[warning]Usage: /org policy update [--payload JSON/YAML|--file path] [--profile custom][/warning]")
            return
        data = _load_structured_payload(payload=payload_text, file_path=file_path)
        payload = await _run_chat_office_service(state, lambda svc: svc.org.update_runtime_policy(data, profile=profile or "custom"))
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "strategy" and rest[:1] == ["update"]:
        values = rest[1:]
        try:
            values, final_decider = _extract_option(values, "--final-decider")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if values:
            console.print("[warning]Usage: /org strategy update --final-decider <role_id>[/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.org.update_org_strategy(final_decider_role_id=final_decider))
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "reset":
        remaining, ok = _require_yes_arg(rest, usage="/org reset --yes")
        if not ok or remaining:
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.org.reset_architecture())
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "saved":
        if not rest:
            console.print("[warning]Usage: /org saved list|save|load|delete ...[/warning]")
            return
        action = rest[0].lower()
        values = rest[1:]
        if action == "list":
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_list())
        elif action == "save":
            if not values:
                console.print("[warning]Usage: /org saved save <name> [--overwrite][/warning]")
                return
            overwrite = "--overwrite" in values[1:]
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_save_as(values[0], overwrite=overwrite))
        elif action == "load":
            if len(values) != 1:
                console.print("[warning]Usage: /org saved load <name>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_load(values[0]))
        elif action == "delete":
            if not values:
                console.print("[warning]Usage: /org saved delete <name> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(values[1:], usage="/org saved delete <name> --yes")
            if not ok or remaining:
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_delete(values[0]))
        else:
            console.print("[warning]Usage: /org saved list|save|load|delete ...[/warning]")
            return
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "export":
        payload = await _run_chat_office_service(state, lambda svc: svc.org.export_config())
        if payload:
            _emit_payload(payload)
        return

    if command == "import":
        if not rest:
            console.print("[warning]Usage: /org import <path> [--dry-run][/warning]")
            return
        dry_run = "--dry-run" in rest[1:]
        raw = Path(rest[0]).read_text(encoding="utf-8")
        payload = await _run_chat_office_service(state, lambda svc: svc.org.import_config(raw, dry_run=dry_run))
        if payload:
            if not dry_run:
                _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    console.print("[warning]Usage: /org [info|role|policy|strategy|reset|saved|export|import] ...[/warning]")


def _render_talent_templates(templates: list[Any], *, title: str = "Talent Templates") -> None:
    if not templates:
        console.print("[info]No talent templates found.[/info]")
        return
    table = Table(title=f"{title} ({len(templates)})")
    table.add_column("Template ID")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Domains")
    table.add_column("Agent")
    table.add_column("Description")
    for template in templates:
        table.add_row(
            str(getattr(template, "id", "") or ""),
            str(getattr(template, "name", "") or ""),
            str(getattr(template, "category", "") or ""),
            ", ".join(str(item) for item in (getattr(template, "domains", []) or [])[:4]),
            str(getattr(template, "preferred_external_agent", "") or ""),
            _clip_text(getattr(template, "description", "") or "", 100),
        )
    console.print(table)


def _render_talent_employees(employees: list[Any]) -> None:
    if not employees:
        console.print("[info]No employees hired yet.[/info]")
        return
    table = Table(title=f"Employees ({len(employees)})")
    table.add_column("Employee ID")
    table.add_column("Name")
    table.add_column("Role")
    table.add_column("Template")
    table.add_column("Status")
    table.add_column("Agent")
    for employee in employees:
        table.add_row(
            str(getattr(employee, "employee_id", "") or ""),
            str(getattr(employee, "name", "") or ""),
            str(getattr(employee, "role_id", "") or ""),
            str(getattr(employee, "template_id", "") or ""),
            str(getattr(employee, "status", "") or ""),
            str(getattr(employee, "preferred_external_agent", "") or ""),
        )
    console.print(table)


async def _handle_talent_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /talent [list|employees|scan|import|import-repo|hire|employee|import-agent] ...[/warning]")
        return
    market = TalentMarket(get_ampuly_home(), state.config)
    command = args[0].lower()
    rest = args[1:]
    try:
        if command in {"list", "templates"}:
            _render_talent_templates(market.list_templates())
            return
        if command == "employees":
            _render_talent_employees(market.list_employees())
            return
        if command == "scan":
            _render_talent_templates(market.scan_local_talent(), title="Local Talent Templates")
            return
        if command == "import":
            if not rest:
                console.print("[warning]Usage: /talent import <template_id...>[/warning]")
                return
            templates = market.import_local_templates(rest)
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Resolved {len(templates)} local talent templates.[/success]")
            _render_talent_templates(templates)
            return
        if command == "import-repo":
            if len(rest) != 1:
                console.print("[warning]Usage: /talent import-repo <path>[/warning]")
                return
            templates = market.import_from_repo(_talent_repo_path(rest[0]))
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Imported {len(templates)} talent templates from repo.[/success]")
            _render_talent_templates(templates)
            return
        if command == "hire":
            rest, employee_name = _extract_option(rest, "--name")
            if len(rest) != 2:
                console.print("[warning]Usage: /talent hire <template_id> <role_id> [--name ...][/warning]")
                return
            employee = market.hire_template(rest[0], rest[1], employee_name=employee_name)
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Hired {employee.name} into role `{employee.role_id}` as `{employee.employee_id}`.[/success]")
            return
        if command in {"employee", "detail"}:
            if len(rest) != 1:
                console.print("[warning]Usage: /talent employee <employee_id>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.talent.employee_detail(rest[0]))
            if payload:
                _emit_payload(payload)
            return
        if command in {"import-agent", "import-employee-as-agent"}:
            if len(rest) != 1:
                console.print("[warning]Usage: /talent import-agent <employee_id>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.talent.import_employee_as_agent(employee_id=rest[0]))
            if payload:
                _emit_payload(payload)
            return
    except Exception as exc:
        console.print(f"[error]Talent command failed: {exc}[/error]")
        return
    console.print("[warning]Usage: /talent [list|employees|scan|import|import-repo|hire|employee|import-agent] ...[/warning]")


def _package_value(package: Any, name: str, default: Any = "") -> Any:
    if hasattr(package, name):
        return getattr(package, name)
    if isinstance(package, dict):
        return package.get(name, default)
    return default


def _render_market_packages(packages: list[Any]) -> None:
    if not packages:
        console.print("[info]No packages installed.[/info]")
        return
    table = Table(title=f"Installed Packages ({len(packages)})")
    table.add_column("Package ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Roles")
    table.add_column("Templates")
    for package in packages:
        role_ids = _package_value(package, "role_ids", []) or []
        template_ids = _package_value(package, "template_ids", []) or []
        table.add_row(
            str(_package_value(package, "package_id", "?") or "?"),
            str(_package_value(package, "name", "") or ""),
            str(_package_value(package, "version", "") or ""),
            str(len(role_ids)),
            str(len(template_ids)),
        )
    console.print(table)


async def _handle_market_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /market [browse|preview|list|presets|apply-preset|install|uninstall|export] ...[/warning]")
        return
    command = args[0].lower()
    rest = args[1:]
    try:
        if command == "browse":
            payload = await _run_chat_office_service(state, lambda svc: svc.market.browse())
            if payload:
                _emit_payload(payload)
            return
        if command == "preview":
            if len(rest) != 1:
                console.print("[warning]Usage: /market preview <preset_id>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.market.preview(rest[0]))
            if payload:
                _emit_payload(payload)
            return
        if command == "list":
            _render_market_packages(list(getattr(state.config.org, "installed_packages", []) or []))
            return
        if command == "presets":
            from ampuly.market.architecture_registry import get_all_presets

            _render_architecture_presets(get_all_presets())
            return
        if command == "apply-preset":
            rest, strategy = _extract_option(rest, "--strategy", default="overwrite")
            if len(rest) != 1 or strategy not in {"namespace", "overwrite"}:
                console.print("[warning]Usage: /market apply-preset <preset_id> [--strategy namespace|overwrite][/warning]")
                return
            from ampuly.market.architecture_registry import apply_architecture_preset_to_config

            info = apply_architecture_preset_to_config(
                state.config,
                rest[0],
                strategy=strategy or "overwrite",
                clear_existing=True,
            )
            state.mode = "company"
            state.company_profile = "custom"
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(
                f"[success]Applied preset {info.package_id}: "
                f"{len(info.role_ids)} roles, {len(info.work_item_template_ids or info.template_ids)} templates.[/success]"
            )
            console.print("[info]Mode set to company; company_profile=custom[/info]")
            return
        if command == "install":
            rest, strategy = _extract_option(rest, "--strategy", default="namespace")
            if len(rest) != 1 or strategy not in {"namespace", "overwrite"}:
                console.print("[warning]Usage: /market install <path> [--strategy namespace|overwrite][/warning]")
                return
            from ampuly.market import PackageLoader, SandboxChecker

            loader = PackageLoader(state.config, get_ampuly_home())
            package = loader.load_from_path(Path(rest[0]))
            report = SandboxChecker().validate(package)
            for warning in report.warnings:
                console.print(f"[warning]{warning}[/warning]")
            if not report.passed:
                for error in report.errors:
                    console.print(f"[error]{error}[/error]")
                console.print("[error]Package failed security check. Installation aborted.[/error]")
                return
            conflicts = loader.detect_conflicts(package)
            if conflicts.has_conflicts:
                for role_id in conflicts.role_conflicts:
                    console.print(f"[warning]Role conflict: {role_id}[/warning]")
                for template_id in conflicts.template_conflicts:
                    console.print(f"[warning]Template conflict: {template_id}[/warning]")
            info = loader.install(package, strategy=strategy or "namespace")
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Installed {info.package_id}: {len(info.role_ids)} roles, {len(info.template_ids)} templates.[/success]")
            return
        if command == "uninstall":
            if not rest:
                console.print("[warning]Usage: /market uninstall <package_id> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(rest[1:], usage="/market uninstall <package_id> --yes")
            if not ok or remaining:
                if remaining:
                    console.print("[warning]Usage: /market uninstall <package_id> --yes[/warning]")
                return
            from ampuly.market import PackageLoader

            loader = PackageLoader(state.config, get_ampuly_home())
            if not loader.uninstall(rest[0]):
                console.print(f"[warning]Package not found: {rest[0]}[/warning]")
                return
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Uninstalled package {rest[0]}.[/success]")
            return
        if command == "export":
            rest, package_id = _extract_option(rest, "--id")
            rest, name = _extract_option(rest, "--name")
            rest, description = _extract_option(rest, "--desc", default="")
            rest, version = _extract_option(rest, "--version", default="1.0.0")
            rest, output_dir = _extract_option(rest, "--output-dir", default=".")
            if rest or not package_id or not name:
                console.print("[warning]Usage: /market export --id <package_id> --name <name> [--desc ...] [--version ...] [--output-dir ...][/warning]")
                return
            from ampuly.market import PackageExporter

            exporter = PackageExporter(state.config, get_ampuly_home())
            package = exporter.export_current(
                package_id=package_id,
                name=name,
                description=description or "",
                version=version or "1.0.0",
            )
            out_path = exporter.write_to_path(package, Path(output_dir or "."))
            console.print(f"[success]Exported package {package_id} to {out_path}.[/success]")
            return
    except Exception as exc:
        console.print(f"[error]Market command failed: {exc}[/error]")
        return
    console.print("[warning]Usage: /market [browse|preview|list|presets|apply-preset|install|uninstall|export] ...[/warning]")


def _render_reorg_proposals(proposals: list[Any]) -> None:
    if not proposals:
        console.print("[info]No reorg proposals found.[/info]")
        return
    table = Table(title=f"Reorg Proposals ({len(proposals)})")
    table.add_column("Proposal ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Risk")
    table.add_column("Scope")
    table.add_column("Updated")
    table.add_column("Summary")
    for proposal in proposals:
        table.add_row(
            str(getattr(proposal, "proposal_id", "") or ""),
            _clip_text(getattr(proposal, "title", "") or "", 50),
            _value_text(getattr(proposal, "status", "")),
            _value_text(getattr(proposal, "risk_level", "")),
            _value_text(getattr(proposal, "scope", "")),
            _format_datetime(getattr(proposal, "updated_at", None)),
            _clip_text(getattr(proposal, "summary", "") or "", 100),
        )
    console.print(table)


def _render_reorg_detail(proposal: Any) -> None:
    table = Table(title=f"Reorg {getattr(proposal, 'proposal_id', '')}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for field_name in ("proposal_id", "title", "status", "scope", "risk_level", "initiated_by", "summary", "rationale", "approval_notes"):
        table.add_row(field_name, _clip_text(_value_text(getattr(proposal, field_name, "")), 260))
    console.print(table)
    detail = {
        "changeset": getattr(getattr(proposal, "changeset", None), "__dict__", getattr(proposal, "changeset", None)),
        "impact_summary": getattr(proposal, "impact_summary", {}) or {},
        "metadata": getattr(proposal, "metadata", {}) or {},
    }
    _print_json(detail)


async def _handle_reorg_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /reorg [list|show|approve|deny|apply] ...[/warning]")
        return
    command = args[0].lower()
    rest = args[1:]
    try:
        if command == "list":
            if not getattr(state.engine, "store", None):
                console.print("[warning]Reorg store is not available.[/warning]")
                return
            rest, limit = _parse_limit_args(rest)
            if rest:
                console.print("[warning]Usage: /reorg list [--limit N][/warning]")
                return
            proposals = await state.engine.store.list_reorg_proposals(_current_project_id(state.engine), limit=limit)
            _render_reorg_proposals(proposals)
            return
        if command == "show":
            if len(rest) != 1:
                console.print("[warning]Usage: /reorg show <proposal_id>[/warning]")
                return
            proposal = await state.engine.show_company_reorg(rest[0])
            if not proposal:
                console.print(f"[warning]Reorg proposal not found: {rest[0]}[/warning]")
                return
            _render_reorg_detail(proposal)
            return
        if command in {"approve", "deny"}:
            rest, notes = _extract_option(rest, "--notes", default="")
            if len(rest) != 1:
                console.print(f"[warning]Usage: /reorg {command} <proposal_id> [--notes ...][/warning]")
                return
            approved = command == "approve"
            decision_notes = notes or (
                "Approved via CLI." if approved else "Denied via CLI."
            )
            checkpoint = await _reorg_interaction(state.engine, rest[0])
            if checkpoint is not None:
                receipt = await _submit_reorg_interaction(
                    state.engine,
                    checkpoint,
                    approved=approved,
                    notes=decision_notes,
                )
                console.print(
                    f"[success]Submitted reorg decision for {rest[0]} "
                    f"({receipt.get('status', 'answered')}).[/success]"
                )
                return
            proposal = await state.engine.approve_company_reorg(
                rest[0],
                approved=approved,
                notes=decision_notes,
            )
            console.print(
                f"[success]Reorg {proposal.proposal_id} "
                f"{'approved' if approved else 'denied'}.[/success]"
            )
            return
        if command == "apply":
            if not rest:
                console.print("[warning]Usage: /reorg apply <proposal_id> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(rest[1:], usage="/reorg apply <proposal_id> --yes")
            if not ok or remaining:
                if remaining:
                    console.print("[warning]Usage: /reorg apply <proposal_id> --yes[/warning]")
                return
            result = await state.engine.apply_company_reorg(rest[0])
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Applied reorg {rest[0]}.[/success]")
            _print_json(result)
            return
    except Exception as exc:
        console.print(f"[error]Reorg command failed: {exc}[/error]")
        return
    console.print("[warning]Usage: /reorg [list|show|approve|deny|apply] ...[/warning]")


def _render_key_value_table(title: str, rows: list[tuple[str, Any]], *, full: bool = False) -> None:
    table = Table(title=title, show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for key, value in rows:
        table.add_row(key, _clip_text(_value_text(value), 260, full=full))
    console.print(table)


def _runtime_session_id_from_task(task: Any) -> str:
    metadata = dict(getattr(task, "metadata", {}) or {})
    runtime_v2 = dict(metadata.get("runtime_v2", {}) or {})
    return str(runtime_v2.get("runtime_session_id", "") or "").strip()


def _row_runtime_session_id(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("runtime_session_id", "") or "").strip()
    return str(getattr(row, "runtime_session_id", "") or "").strip()


def _dedupe_runtime_rows(rows: list[Any]) -> list[Any]:
    seen: set[str] = set()
    results: list[Any] = []
    for row in rows:
        runtime_session_id = _row_runtime_session_id(row)
        if not runtime_session_id or runtime_session_id in seen:
            continue
        seen.add(runtime_session_id)
        results.append(row)
    return results


def _runtime_row_metadata(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row.get("metadata", {}) or {})
    return dict(getattr(row, "metadata", {}) or {})


def _runtime_row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


async def _handle_work_items_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        args = ["list"]
    command = args[0].lower()
    rest = args[1:]
    try:
        if command in {"list", "ls"}:
            rest, limit, full = _parse_view_args(rest, default=20)
            role_id = None
            status = None
            rest, role_id = _extract_option(rest, "--role", default=None)
            rest, status = _extract_option(rest, "--status", default=None)
            if rest:
                console.print("[warning]Usage: /work-items list [--role <role_id>] [--status <status>] [--limit N][/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.list(
                    project_id=_current_project_id(state.engine),
                    session_id=state.session_id,
                    role_id=role_id,
                    status=status,
                    limit=limit,
                ),
            )
            if payload:
                items = list(payload.get("items", []) or payload.get("work_items", []) or [])
                if not items:
                    console.print("[info]No work items found.[/info]")
                    return
                table = Table(title=f"Work Items ({len(items)})")
                table.add_column("Work Item")
                table.add_column("Title")
                table.add_column("Role")
                table.add_column("Status")
                table.add_column("Task")
                table.add_column("Session")
                for item in items:
                    table.add_row(
                        str(item.get("work_item_id", "") or item.get("id", "") or ""),
                        _clip_text(item.get("title", "") or "", 70, full=full),
                        str(item.get("role_id", "") or ""),
                        str(item.get("status", "") or item.get("phase", "") or ""),
                        str(item.get("task_id", "") or item.get("runtime_task_id", "") or ""),
                        str(item.get("session_id", "") or ""),
                    )
                console.print(table)
            return
        if command == "show":
            rest, limit, _full = _parse_view_args(rest, default=50)
            if len(rest) != 1:
                console.print("[warning]Usage: /work-items show <work_item_id> [--limit N][/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.show(project_id=_current_project_id(state.engine), work_item_id=rest[0], limit=limit),
            )
            if payload:
                _emit_payload(payload)
            return
        if command == "logs":
            rest, limit, _full = _parse_view_args(rest, default=50)
            rest, role_id = _extract_option(rest, "--role", default=None)
            work_item_id = rest[0] if rest else ""
            if len(rest) > 1:
                console.print("[warning]Usage: /work-items logs [work_item_id] [--role <role_id>] [--limit N][/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.logs(
                    project_id=_current_project_id(state.engine),
                    session_id=state.session_id,
                    work_item_id=work_item_id,
                    role_id=role_id or "",
                    limit=limit,
                ),
            )
            if payload:
                _render_work_item_logs_payload(payload, limit=limit, full=_full)
            return
        if command in {"role-status", "status-by-role"}:
            if rest:
                console.print("[warning]Usage: /work-items role-status[/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.status_by_role(
                    project_id=_current_project_id(state.engine),
                    session_id=state.session_id,
                ),
            )
            if payload:
                _emit_payload(payload)
            return
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    console.print("[warning]Usage: /work-items list|show|logs|role-status ...[/warning]")


async def _list_runtime_sessions_for_scope(
    store: Any,
    *,
    project_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    limit: int = _SLASH_DEFAULT_LIMIT,
) -> list[Any]:
    if not hasattr(store, "list_runtime_sessions"):
        return []
    try:
        return await store.list_runtime_sessions(
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
        )
    except TypeError:
        rows = await store.list_runtime_sessions(project_id=project_id)
        if task_id:
            rows = [row for row in rows if str(_runtime_row_value(row, "task_id", "") or "") == task_id]
        if session_id:
            rows = [row for row in rows if str(_runtime_row_value(row, "session_id", "") or "") == session_id]
        return rows[:limit]


def _render_runtime_sessions(rows: list[Any], *, title: str = "Runtime Sessions", full: bool = False) -> None:
    if not rows:
        console.print("[info]No runtime sessions found.[/info]")
        return
    table = Table(title=f"{title} ({len(rows)})")
    table.add_column("Runtime ID")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Session")
    table.add_column("Updated")
    table.add_column("Summary")
    for row in rows:
        metadata = _runtime_row_metadata(row)
        table.add_row(
            _row_runtime_session_id(row),
            str(_runtime_row_value(row, "status", "") or ""),
            str(_runtime_row_value(row, "task_id", "") or ""),
            str(_runtime_row_value(row, "session_id", "") or ""),
            _format_datetime(_runtime_row_value(row, "updated_at", "")),
            _json_summary(metadata, full=full),
        )
    console.print(table)


def _render_external_sessions(rows: list[Any], *, full: bool = False) -> None:
    if not rows:
        console.print("[info]No external agent sessions found.[/info]")
        return
    table = Table(title=f"External Agent Sessions ({len(rows)})")
    table.add_column("Agent")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("AMPULY Session")
    table.add_column("Provider Session")
    table.add_column("Workspace")
    table.add_column("Updated")
    for row in rows:
        table.add_row(
            str(getattr(row, "agent_type", "") or ""),
            str(getattr(row, "status", "") or ""),
            str(getattr(row, "task_id", "") or ""),
            str(getattr(row, "ampuly_session_id", "") or ""),
            _clip_text(getattr(row, "session_id", "") or "", 48, full=full),
            _clip_text(getattr(row, "workspace_path", "") or "", 80, full=full),
            _format_datetime(getattr(row, "updated_at", None)),
        )
    console.print(table)


def _render_checkpoint_table(checkpoints: list[Any], *, title: str, full: bool = False) -> None:
    if not checkpoints:
        console.print("[info]No checkpoints found.[/info]")
        return
    table = Table(title=f"{title} ({len(checkpoints)})")
    table.add_column("Checkpoint ID")
    table.add_column("Status")
    table.add_column("Type")
    table.add_column("Task")
    table.add_column("Session")
    table.add_column("Updated")
    table.add_column("Payload")
    for checkpoint in checkpoints:
        table.add_row(
            str(getattr(checkpoint, "checkpoint_id", "") or ""),
            str(getattr(checkpoint, "status", "") or ""),
            str(getattr(checkpoint, "checkpoint_type", "") or ""),
            str(getattr(checkpoint, "task_id", "") or ""),
            str(getattr(checkpoint, "session_id", "") or ""),
            _format_datetime(getattr(checkpoint, "updated_at", None)),
            _json_summary(getattr(checkpoint, "payload", {}) or {}, full=full),
        )
    console.print(table)


async def _handle_runtime_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Runtime store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /runtime --limit 20.[/warning]")
        return
    if args:
        console.print("[warning]Usage: /runtime [--limit N] [--full]. Try /help.[/warning]")
        return

    snapshot = {}
    if hasattr(state.runtime_display, "status_snapshot"):
        snapshot = state.runtime_display.status_snapshot()
    if snapshot:
        _render_key_value_table(
            "Current Runtime Snapshot",
            [
                ("agent", snapshot.get("agent_id") or snapshot.get("role_id") or ""),
                ("task", snapshot.get("task_id") or ""),
                ("tool", snapshot.get("current_tool") or ""),
                ("queue", snapshot.get("queue_depth", 0)),
                ("stream_queue", snapshot.get("stream_queue_depth", 0)),
                ("context_remaining", f"{snapshot.get('context_remaining_pct')}%" if snapshot.get("context_remaining_pct") not in (None, "") else ""),
                ("turn_cost", f"${float(snapshot.get('turn_cost_usd', 0.0) or 0.0):.4f}"),
                ("session_cost", f"${float(snapshot.get('session_cost_usd', 0.0) or 0.0):.4f}"),
                ("approvals", snapshot.get("pending_permission_count", 0)),
                ("checkpoint", snapshot.get("checkpoint_hint", "")),
            ],
            full=full,
        )

    from ampuly.core.models import TaskStatus

    tasks = await store.get_tasks(project_id=_current_project_id(state.engine))
    terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    active_tasks = [task for task in tasks if getattr(task, "status", None) not in terminal]
    if active_tasks:
        table = Table(title=f"Active Tasks ({len(active_tasks[:limit])})")
        table.add_column("Task ID")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Agent")
        table.add_column("Session")
        for task in active_tasks[:limit]:
            table.add_row(
                str(getattr(task, "id", "") or ""),
                _clip_text(getattr(task, "title", "") or "", 70, full=full),
                _value_text(getattr(task, "status", "")),
                str(getattr(task, "assigned_to", "") or ""),
                str(getattr(task, "session_id", "") or ""),
            )
        console.print(table)
    else:
        console.print("[info]No active tasks for this project.[/info]")

    runtime_rows = await _list_runtime_sessions_for_scope(
        store,
        project_id=_current_project_id(state.engine),
        limit=limit,
    )
    _render_runtime_sessions(_dedupe_runtime_rows(runtime_rows), full=full)

    if hasattr(store, "list_external_sessions"):
        external = await store.list_external_sessions(project_id=_current_project_id(state.engine), limit=limit)
    else:
        external = []
    _render_external_sessions(external, full=full)

    checkpoints = await store.get_pending_checkpoints(project_id=_current_project_id(state.engine))
    _render_checkpoint_table(checkpoints[:limit], title="Pending Checkpoints", full=full)


async def _resolve_logs_target(
    state: _InteractiveChatState,
    target: str,
    *,
    limit: int,
) -> tuple[Any | None, Any | None, list[Any], str | None]:
    store = _require_chat_store(state, label="Log store")
    if store is None:
        return None, None, [], None
    project_id = _current_project_id(state.engine)
    task = await store.get_task(target) if hasattr(store, "get_task") else None
    session = None
    runtime_rows: list[Any] = []
    runtime_row = None
    if task:
        task_project = str(getattr(task, "project_id", "") or "default")
        if task_project != project_id:
            console.print(f"[warning]Task belongs to project '{task_project}'. Switch project first with /project {task_project}.[/warning]")
            return None, None, [], None
        session_id = str(getattr(task, "session_id", "") or "")
        if session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
        runtime_rows.extend(await _list_runtime_sessions_for_scope(store, project_id=project_id, task_id=target, limit=limit))
        runtime_id = _runtime_session_id_from_task(task)
        if runtime_id and hasattr(store, "get_runtime_session"):
            row = await store.get_runtime_session(runtime_id)
            if row:
                runtime_rows.append(row)
        return task, session, _dedupe_runtime_rows(runtime_rows), None
    if hasattr(store, "get_session"):
        session = await store.get_session(target)
    if session:
        session_project = str(getattr(session, "project_id", "") or "default")
        if session_project != project_id:
            console.print(f"[warning]Session belongs to project '{session_project}'. Switch project first with /project {session_project}.[/warning]")
            return None, None, [], None
        runtime_rows.extend(await _list_runtime_sessions_for_scope(store, project_id=project_id, session_id=target, limit=limit))
        return None, session, _dedupe_runtime_rows(runtime_rows), None
    if hasattr(store, "get_runtime_session"):
        runtime_row = await store.get_runtime_session(target)
    if runtime_row:
        runtime_project = str(runtime_row.get("project_id", "") or "default")
        if runtime_project != project_id:
            console.print(f"[warning]Runtime session belongs to project '{runtime_project}'. Switch project first with /project {runtime_project}.[/warning]")
            return None, None, [], None
        task_id = str(runtime_row.get("task_id", "") or "")
        session_id = str(runtime_row.get("session_id", "") or "")
        if task_id and hasattr(store, "get_task"):
            task = await store.get_task(task_id)
        if session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
        return task, session, [runtime_row], None
    return None, None, [], target


def _render_task_progress(task: Any, *, limit: int, full: bool = False) -> None:
    if not task:
        return
    metadata = dict(getattr(task, "metadata", {}) or {})
    progress = list(metadata.get("progress_log", []) or [])
    if not progress:
        console.print("[info]No task progress log found.[/info]")
        return
    table = Table(title=f"Task Progress ({len(progress[-limit:])})")
    table.add_column("Entry")
    for entry in progress[-limit:]:
        table.add_row(_clip_text(entry, 300, full=full))
    console.print(table)


async def _render_runtime_log_tables(store: Any, runtime_rows: list[Any], *, limit: int, full: bool = False) -> None:
    runtime_ids = [_row_runtime_session_id(row) for row in runtime_rows if _row_runtime_session_id(row)]
    if not runtime_ids:
        console.print("[info]No runtime session logs found.[/info]")
        return
    _render_runtime_sessions(runtime_rows, title="Runtime Log Scope", full=full)
    for runtime_id in runtime_ids[:limit]:
        if hasattr(store, "list_runtime_events"):
            events = await store.list_runtime_events(runtime_id, limit=limit)
            if events:
                table = Table(title=f"Runtime Events: {runtime_id} ({len(events)})")
                table.add_column("Created")
                table.add_column("Type")
                table.add_column("Payload")
                for event in events:
                    table.add_row(
                        _format_datetime(event.get("created_at")),
                        str(event.get("event_type", "") or ""),
                        _json_summary(event.get("payload", {}) or {}, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_transcript_entries"):
            entries = (await store.list_runtime_transcript_entries(runtime_id))[-limit:]
            if entries:
                table = Table(title=f"Runtime Transcript: {runtime_id} ({len(entries)})")
                table.add_column("Created")
                table.add_column("Role")
                table.add_column("Type")
                table.add_column("Content")
                for entry in entries:
                    table.add_row(
                        _format_datetime(entry.get("created_at")),
                        str(entry.get("role", "") or ""),
                        str(entry.get("entry_type", "") or ""),
                        _clip_text(entry.get("content", "") or "", 300, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_tool_calls"):
            calls = (await store.list_runtime_tool_calls(runtime_id))[-limit:]
            if calls:
                table = Table(title=f"Tool Calls: {runtime_id} ({len(calls)})")
                table.add_column("Created")
                table.add_column("Tool")
                table.add_column("Arguments")
                for call in calls:
                    table.add_row(
                        _format_datetime(call.get("created_at")),
                        str(call.get("tool_name", "") or ""),
                        _json_summary(call.get("arguments", {}) or {}, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_tool_results"):
            results = (await store.list_runtime_tool_results(runtime_id))[-limit:]
            if results:
                table = Table(title=f"Tool Results: {runtime_id} ({len(results)})")
                table.add_column("Created")
                table.add_column("Tool")
                table.add_column("Payload")
                for result in results:
                    table.add_row(
                        _format_datetime(result.get("created_at")),
                        str(result.get("tool_name", "") or ""),
                        _json_summary(result.get("payload", {}) or {}, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_permission_grants"):
            grants = (await store.list_runtime_permission_grants(runtime_session_id=runtime_id))[-limit:]
            if grants:
                table = Table(title=f"Permission Grants: {runtime_id} ({len(grants)})")
                table.add_column("Scope")
                table.add_column("Tool")
                table.add_column("Candidate")
                table.add_column("Created")
                for grant in grants:
                    table.add_row(
                        str(grant.get("scope", "") or ""),
                        str(grant.get("tool_name", "") or ""),
                        _clip_text(grant.get("candidate", "") or "", 120, full=full),
                        _format_datetime(grant.get("created_at")),
                    )
                console.print(table)


def _clip_log_text(value: Any, limit: int = 1600, *, full: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if full or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


_EXTERNAL_TRANSCRIPT_RE = re.compile(r"^\[External:(?P<agent>[^:\]]+):(?P<kind>[^\]]+)\]\s*(?P<body>.*)$", re.S)
_STAFFING_CONTROL_REPLIES = {"approve", "approved", "auto", "auto recruit", "deny", "stop", "cancel"}
_IMPORTANT_WORK_ITEM_EVENTS = {
    "approval_requested",
    "approval_resolved",
    "checkpoint_created",
    "checkpoint_resolved",
    "work_item_failed",
    "work_item_blocked",
    "work_item_completed",
    "runtime_failed",
    "error",
}


@dataclass
class _WorkItemLogCell:
    kind: str
    created_at: Any
    label: str
    content: str
    order: int = 0
    status: str = ""
    exit_code: Any = None


def _runtime_tool_payload_text(value: dict[str, Any], *, full: bool = False) -> str:
    if not isinstance(value, dict):
        return _clip_log_text(value, full=full)
    for key in ("command", "cmd", "summary", "result_summary", "stdout", "stderr", "output", "text", "message"):
        if value.get(key):
            text = str(value.get(key) or "")
            if key in {"command", "cmd"}:
                return f"$ {text}"
            return text
    return _json_summary(value, limit=1200, full=full)


def _runtime_tool_command_text(value: Any, *, full: bool = False) -> str:
    if not isinstance(value, dict):
        return _clip_log_text(value, full=full)
    for key in ("command", "cmd"):
        if value.get(key):
            return f"$ {str(value.get(key) or '').strip()}"
    return _runtime_tool_payload_text(value, full=full)


def _runtime_tool_result_status(value: Any) -> tuple[str, Any]:
    if not isinstance(value, dict):
        return "", None
    status = str(value.get("status", "") or "").strip()
    exit_code = value.get("exit_code")
    if exit_code is None:
        exit_code = value.get("returncode")
    if not status and "success" in value:
        status = "completed" if bool(value.get("success")) else "failed"
    if not status and exit_code is not None:
        status = "completed" if str(exit_code) == "0" else "failed"
    return status, exit_code


def _runtime_tool_result_text(value: Any, *, full: bool = False) -> str:
    if not isinstance(value, dict):
        return _clip_log_text(value, full=full)
    parts: list[str] = []
    for key in ("stdout", "stderr", "output", "result_summary", "summary", "text", "message"):
        if value.get(key):
            parts.append(str(value.get(key) or "").strip())
    if parts:
        return "\n".join(part for part in parts if part)
    return _json_summary(value, limit=1200, full=full)


def _runtime_agent_label(entry: dict[str, Any]) -> str:
    runtime_id = str(entry.get("runtime_session_id", "") or "")
    if ":" in runtime_id:
        prefix = runtime_id.split(":", 1)[0].strip()
        if prefix:
            return prefix
    return str(entry.get("role", "") or "runtime").strip() or "runtime"


def _parse_runtime_stream_event(content: str) -> dict[str, Any] | None:
    try:
        event = json.loads(str(content or "").strip())
    except Exception:
        return None
    if isinstance(event, dict) and isinstance(event.get("msg"), dict):
        event = event["msg"]
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type", "") or "").strip()
    return event if event_type else None


def _command_text_from_codex_item(item: dict[str, Any], *, full: bool = False) -> str:
    command = item.get("command")
    if isinstance(command, list):
        text = " ".join(str(part) for part in command if str(part).strip())
    else:
        text = str(command or "").strip()
    if not text:
        text = "command execution"
    return f"$ {_clip_log_text(text, limit=420, full=full)}"


def _codex_command_output_text(item: dict[str, Any], *, full: bool = False) -> str:
    output = str(item.get("aggregated_output", "") or "").strip()
    if not output:
        return ""
    if full:
        return output
    if len(output) > 600 and output[:1] in {"{", "["}:
        return "output omitted; use --full to inspect raw JSON output"
    return _clip_log_text(output, limit=900, full=False)


def _runtime_stream_event_cells(
    entry: dict[str, Any],
    *,
    full: bool = False,
    skip_tool_events: bool = False,
) -> list[_WorkItemLogCell]:
    event = _parse_runtime_stream_event(str(entry.get("content", "") or ""))
    if not event:
        return []
    event_type = str(event.get("type", "") or "").strip()
    created_at = entry.get("created_at")
    agent = _runtime_agent_label(entry)
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = str(item.get("type", "") or "").strip()

    if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed"}:
        if not full:
            return []
        return [_WorkItemLogCell(
            kind="event",
            created_at=created_at,
            label=f"runtime {event_type}",
            content=_json_summary(event, limit=1000, full=full),
            order=50,
        )]

    if item_type == "agent_message" and event_type in {"item.completed", "item.started"}:
        text = str(item.get("text", "") or "").strip()
        if not text:
            return []
        return [_WorkItemLogCell(
            kind="thinking",
            created_at=created_at,
            label=agent,
            content=text,
            order=15,
        )]

    if item_type == "command_execution":
        if skip_tool_events:
            return []
        if event_type == "item.started":
            return [_WorkItemLogCell(
                kind="tool_call",
                created_at=created_at,
                label="command_execution",
                content=_command_text_from_codex_item(item, full=full),
                order=30,
            )]
        if event_type == "item.completed":
            status = str(item.get("status", "") or "").strip()
            exit_code = item.get("exit_code")
            content = _codex_command_output_text(item, full=full)
            return [_WorkItemLogCell(
                kind="tool_result",
                created_at=created_at,
                label="command_execution",
                content=content,
                order=35,
                status=status,
                exit_code=exit_code,
            )]

    if not full:
        return []
    return [_WorkItemLogCell(
        kind="event",
        created_at=created_at,
        label=f"runtime {event_type}",
        content=_json_summary(event, limit=1000, full=full),
        order=50,
    )]


def _is_staffing_transcript_noise(role: str, content: str) -> bool:
    normalized = " ".join(str(content or "").strip().lower().split())
    if not normalized:
        return False
    if role == "assistant" and normalized.startswith("company mode has a pending manual staffing selection"):
        return True
    if role == "user" and normalized in _STAFFING_CONTROL_REPLIES:
        return True
    return False


def _short_runtime_ref(value: str, *, full: bool = False) -> str:
    text = str(value or "").strip()
    if full or len(text) <= 48:
        return text
    parts = text.split("::")
    if len(parts) >= 3 and parts[0] == "role-runtime":
        return f"{parts[0]}::{parts[1][:8]}::{parts[-1]}"
    return _clip_text(text, 48, full=False)


def _event_is_high_signal(event_type: str) -> bool:
    normalized = str(event_type or "").strip().lower()
    if not normalized:
        return False
    if normalized in _IMPORTANT_WORK_ITEM_EVENTS:
        return True
    return any(token in normalized for token in ("approval", "checkpoint", "fail", "error", "blocked"))


def _render_codex_block(content: Any, *, prefix: str = "  ", style: str = "", full: bool = False) -> None:
    body = _clip_log_text(content, full=full)
    if not body:
        return
    for line in body.splitlines():
        text = f"{prefix}{line}" if line else ""
        if style:
            console.print(f"[{style}]{escape(text)}[/{style}]")
        else:
            console.print(escape(text))


def _render_codex_assistant_body(content: str, *, full: bool = False) -> None:
    body = _clip_log_text(content, full=full)
    if not body:
        return
    try:
        console.print(Markdown(body))
    except Exception:
        _render_codex_block(body, prefix="  ", full=full)


def _render_work_item_log_cell(cell: _WorkItemLogCell, *, full: bool = False) -> None:
    time_text = _format_datetime(cell.created_at)
    timestamp = f"[dim]{escape(time_text)}[/dim] " if time_text else ""
    label = escape(cell.label)
    if cell.kind == "user":
        console.print(f"{timestamp}[success]user[/success]")
        _render_codex_block(cell.content, prefix="› ", style="success", full=full)
    elif cell.kind == "assistant":
        console.print(f"{timestamp}[agent]• {label}[/agent]")
        _render_codex_assistant_body(cell.content, full=full)
    elif cell.kind == "thinking":
        console.print(f"{timestamp}[dim]thinking {label}[/dim]")
        _render_codex_block(cell.content, prefix="  ", style="dim", full=full)
    elif cell.kind == "tool_call":
        text = cell.content.strip()
        if text.startswith("$ "):
            console.print(f"{timestamp}[tool]{escape(text)}[/tool]")
        else:
            console.print(f"{timestamp}[tool]tool {label}[/tool]")
            _render_codex_block(text, prefix="  ", full=full)
    elif cell.kind == "tool_result":
        heading = f"tool result {cell.label}".strip()
        status_bits: list[str] = []
        if cell.status:
            status_bits.append(f"status={cell.status}")
        if cell.exit_code is not None and str(cell.exit_code) != "":
            status_bits.append(f"exit_code={cell.exit_code}")
        suffix = f" ({', '.join(status_bits)})" if status_bits else ""
        style = "success" if cell.status in {"", "completed", "success", "succeeded"} else "error"
        console.print(f"{timestamp}[{style}]{escape(heading + suffix)}[/{style}]")
        _render_codex_block(cell.content, prefix="  ", full=full)
    elif cell.kind in {"permission", "event", "handoff"}:
        style = "warning" if cell.kind == "permission" else "info"
        console.print(f"{timestamp}[{style}]• {label}[/{style}]")
        _render_codex_block(cell.content, prefix="  ", full=full)
    else:
        console.print(f"{timestamp}[info]• {label}[/info]")
        _render_codex_block(cell.content, prefix="  ", full=full)


def _render_work_item_logs_payload(payload: dict[str, Any], *, limit: int, full: bool = False) -> None:
    title_bits = []
    if payload.get("role_id"):
        title_bits.append(f"role={payload['role_id']}")
    if payload.get("work_item_id"):
        title_bits.append(f"work_item={str(payload['work_item_id'])[:12]}")
    if payload.get("session_id"):
        title_bits.append(f"session={str(payload['session_id'])[:8]}")
    scope_title = "Work Item Logs" + (f" ({', '.join(title_bits)})" if title_bits else "")
    console.print(f"[bold]{escape(scope_title)}[/bold]")

    work_items = list(payload.get("work_items", []) or [])[:limit]
    for item in work_items:
        status = str(item.get("phase", "") or item.get("kanban_column", "") or "")
        runtime = str(item.get("role_runtime_session_id", "") or item.get("claimed_by_role_runtime_session_id", "") or "").strip()
        suffix = f" [{status}]" if status else ""
        if runtime:
            suffix += f" runtime={_short_runtime_ref(runtime, full=full)}"
        console.print(
            f"[dim]work-item[/dim] [bold]{escape(str(item.get('work_item_id', '') or '')[:12])}[/bold] "
            f"{escape(str(item.get('role_id', '') or ''))} "
            f"{escape(_clip_text(item.get('title', '') or '', 96, full=full))}{escape(suffix)}"
        )
    if work_items:
        console.print()

    timeline: list[_WorkItemLogCell] = []
    structured_tool_calls = list(payload.get("runtime_tool_calls", []) or [])

    for item in list(payload.get("transcript", []) or [])[-limit:]:
        message = item.get("message") if isinstance(item, dict) else None
        if not message or getattr(message, "summary_flag", False):
            continue
        content = _render_transcript_parts(item.get("parts", []))
        if not content:
            continue
        role = str(getattr(message, "role", "") or "assistant").strip().lower()
        if not full and _is_staffing_transcript_noise(role, content):
            continue
        agent_id = str(getattr(message, "agent_id", "") or "").strip()
        sender = {
            "user": "user",
            "assistant": "assistant",
            "system": "system",
            "subagent": agent_id or "subagent",
        }.get(role, agent_id or role or "assistant")
        timeline.append(_WorkItemLogCell(
            kind="user" if role == "user" else "assistant",
            created_at=getattr(message, "created_at", None),
            label=sender,
            content=content,
            order=10,
        ))

    for entry in list(payload.get("runtime_transcript_entries", []) or [])[-limit:]:
        content = str(entry.get("content", "") or "")
        if not content:
            continue
        stream_cells = _runtime_stream_event_cells(entry, full=full, skip_tool_events=bool(structured_tool_calls))
        if stream_cells:
            timeline.extend(stream_cells)
            continue
        if _parse_runtime_stream_event(content):
            continue
        label = str(entry.get("entry_type", "") or "runtime")
        kind = "assistant"
        order = 20
        match = _EXTERNAL_TRANSCRIPT_RE.match(content)
        if match:
            agent = match.group("agent")
            external_kind = match.group("kind").strip().lower()
            content = match.group("body").strip()
            label = agent
            if external_kind == "tool":
                if structured_tool_calls:
                    continue
                kind = "tool_call"
                order = 30
            elif external_kind == "thinking":
                kind = "thinking"
                order = 15
            elif external_kind in {"error", "failed", "failure"}:
                kind = "event"
                label = f"{agent} {external_kind}"
                order = 55
            else:
                kind = "assistant"
                label = agent
        timeline.append(_WorkItemLogCell(
            kind=kind,
            created_at=entry.get("created_at"),
            label=label,
            content=content,
            order=order,
        ))

    for call in structured_tool_calls[-limit:]:
        tool_name = str(call.get("tool_name", "") or "tool")
        timeline.append(_WorkItemLogCell(
            kind="tool_call",
            created_at=call.get("created_at"),
            label=tool_name,
            content=_runtime_tool_command_text(call.get("arguments", {}) or {}, full=full),
            order=30,
        ))

    for result in list(payload.get("runtime_tool_results", []) or [])[-limit:]:
        tool_name = str(result.get("tool_name", "") or "tool")
        status, exit_code = _runtime_tool_result_status(result.get("payload", {}) or {})
        timeline.append(_WorkItemLogCell(
            kind="tool_result",
            created_at=result.get("created_at"),
            label=tool_name,
            content=_runtime_tool_result_text(result.get("payload", {}) or {}, full=full),
            order=35,
            status=status,
            exit_code=exit_code,
        ))

    for event in list(payload.get("events", []) or [])[-limit:]:
        event_type = str(event.get("event_type", "") or "event")
        if not full and not _event_is_high_signal(event_type):
            continue
        work_item_id = str(event.get("work_item_id", "") or "")[:12]
        timeline.append(_WorkItemLogCell(
            kind="event",
            created_at=event.get("created_at"),
            label=f"event {event_type}",
            content=f"{work_item_id} {_json_summary(event.get('payload', {}) or {}, limit=1000, full=full)}".strip(),
            order=50,
        ))

    for event in list(payload.get("runtime_events", []) or [])[-limit:]:
        event_type = str(event.get("event_type", "") or "runtime")
        if not full and not _event_is_high_signal(event_type):
            continue
        timeline.append(_WorkItemLogCell(
            kind="event",
            created_at=event.get("created_at"),
            label=f"runtime {event_type}",
            content=_json_summary(event.get("payload", {}) or {}, limit=1000, full=full),
            order=50,
        ))

    for grant in list(payload.get("runtime_permission_grants", []) or [])[-limit:]:
        timeline.append(_WorkItemLogCell(
            kind="permission",
            created_at=grant.get("created_at"),
            label="permission",
            content=f"{grant.get('scope', '')} {grant.get('tool_name', '')}: {grant.get('candidate', '')}".strip(),
            order=45,
        ))

    for handoff in list(payload.get("handoffs", []) or [])[-limit:]:
        timeline.append(_WorkItemLogCell(
            kind="handoff",
            created_at=handoff.get("created_at"),
            label=f"handoff {handoff.get('from_role', '')}->{handoff.get('to_role', '')}",
            content=str(handoff.get("summary", "") or _json_summary(handoff, limit=1000, full=full)),
            order=40,
        ))

    if not timeline and not work_items:
        console.print("[info]No role/work-item logs found for the current session.[/info]")
        return

    timeline.sort(key=lambda item: (str(item.created_at or ""), item.order, item.label))
    for index, item in enumerate(timeline[-limit:]):
        if index:
            console.print()
        _render_work_item_log_cell(item, full=full)


async def _handle_logs_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Log store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /logs <task_id|session_id> --limit 20.[/warning]")
        return
    if len(args) != 1:
        console.print("[warning]Usage: /logs <task_id|session_id> [--limit N] [--full]. Try /session list or /tasks.[/warning]")
        return
    task, session, runtime_rows, missing = await _resolve_logs_target(state, args[0], limit=limit)
    if missing:
        console.print(f"[warning]No task, session, or runtime session found for {missing}. Try /session list or /tasks.[/warning]")
        return
    if task:
        _render_key_value_table(
            f"Task Log Target {getattr(task, 'id', '')}",
            [
                ("title", getattr(task, "title", "")),
                ("status", _value_text(getattr(task, "status", ""))),
                ("session", getattr(task, "session_id", "")),
                ("runtime", _runtime_session_id_from_task(task)),
            ],
            full=full,
        )
        _render_task_progress(task, limit=limit, full=full)
    if session:
        session_id = str(getattr(session, "session_id", "") or "")
        _render_key_value_table(
            f"Session Log Target {session_id}",
            [
                ("title", getattr(session, "title", "")),
                ("status", getattr(session, "status", "")),
                ("mode", getattr(session, "mode", "")),
            ],
            full=full,
        )
        if hasattr(store, "get_session_transcript"):
            transcript = await store.get_session_transcript(session_id)
            _render_transcript_table(transcript, limit=limit)
    await _render_runtime_log_tables(store, runtime_rows, limit=limit, full=full)


async def _task_scope_ids_for_comms(state: _InteractiveChatState, task: Any) -> list[str]:
    task_id = str(getattr(task, "id", "") or "").strip()
    metadata = dict(getattr(task, "metadata", {}) or {})
    scope = {task_id}
    scope.update(str(item).strip() for item in list(metadata.get("execution_task_ids", []) or []) if str(item).strip())
    parent_session_id = str(getattr(task, "session_id", "") or "").strip()
    store = getattr(state.engine, "store", None)
    if parent_session_id and store and hasattr(store, "get_tasks"):
        try:
            tasks = await store.get_tasks(project_id=_current_project_id(state.engine))
            for candidate in tasks:
                if str(getattr(candidate, "parent_session_id", "") or "").strip() == parent_session_id:
                    scope.add(str(getattr(candidate, "id", "") or "").strip())
        except Exception:
            pass
    scope.discard("")
    return sorted(scope)


def _render_agent_messages(messages: list[Any], *, full: bool = False) -> None:
    if not messages:
        console.print("[info]No company-mode messages found for this task scope.[/info]")
        return
    table = Table(title=f"Company Messages ({len(messages)})")
    table.add_column("Time")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Subject")
    table.add_column("Body")
    for message in messages:
        to_agents = getattr(message, "to_agents", []) or []
        table.add_row(
            _format_datetime(getattr(message, "timestamp", None)),
            str(getattr(message, "from_agent", "") or ""),
            ", ".join(str(item) for item in to_agents),
            _value_text(getattr(message, "msg_type", "")),
            _value_text(getattr(message, "status", "")),
            _clip_text(getattr(message, "subject", "") or "", 80, full=full),
            _clip_text(getattr(message, "body", "") or "", 180, full=full),
        )
    console.print(table)


def _render_handoff_records(records: list[Any], *, full: bool = False) -> None:
    if not records:
        console.print("[info]No handoff records found for this task scope.[/info]")
        return
    table = Table(title=f"Handoffs ({len(records)})")
    table.add_column("Handoff ID")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Summary")
    table.add_column("Created")
    for record in records:
        table.add_row(
            str(getattr(record, "handoff_id", "") or ""),
            str(getattr(record, "from_role", "") or ""),
            str(getattr(record, "to_role", "") or ""),
            str(getattr(record, "status", "") or ""),
            str(getattr(record, "task_id", "") or ""),
            _clip_text(getattr(record, "summary", "") or "", 180, full=full),
            _format_datetime(getattr(record, "created_at", None)),
        )
    console.print(table)


async def _handle_comms_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Comms store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /comms <task_id>.[/warning]")
        return
    if len(args) != 1:
        console.print("[warning]Usage: /comms <task_id> [--limit N] [--full]. Try /tasks.[/warning]")
        return
    task = await _get_task_for_current_project(state, args[0])
    if task is None:
        return
    scope_ids = await _task_scope_ids_for_comms(state, task)
    console.print(f"[info]Comms task scope: {', '.join(scope_ids)}[/info]")
    messages = await store.list_agent_messages_for_tasks(scope_ids, limit=limit) if hasattr(store, "list_agent_messages_for_tasks") else []
    _render_agent_messages(messages, full=full)

    records: list[Any] = []
    if hasattr(store, "get_handoff_records"):
        seen: set[str] = set()
        for task_id in scope_ids:
            for record in await store.get_handoff_records(project_id=_current_project_id(state.engine), task_id=task_id, limit=limit):
                handoff_id = str(getattr(record, "handoff_id", "") or "")
                if handoff_id and handoff_id not in seen:
                    seen.add(handoff_id)
                    records.append(record)
    _render_handoff_records(records[:limit], full=full)

    metadata = dict(getattr(task, "metadata", {}) or {})
    context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
    notes = []
    for key in ("structured_review_verdict", "review_verdict", "review_notes", "handoff_context", "handoff_to"):
        if metadata.get(key):
            notes.append((key, _json_summary(metadata.get(key), full=full)))
        if context_snapshot.get(key):
            notes.append((f"context.{key}", _json_summary(context_snapshot.get(key), full=full)))
    if notes:
        _render_key_value_table("Review and Handoff Notes", notes, full=full)
    else:
        console.print("[info]No review notes or handoff context found on the task.[/info]")


def _collect_attachment_refs(value: Any, source: str, out: list[tuple[str, dict[str, Any]]]) -> None:
    if isinstance(value, dict):
        if value.get("attachment_id") or value.get("disk_path"):
            if value.get("filename") or value.get("mime_type") or value.get("disk_path"):
                out.append((source, dict(value)))
        for key, nested in value.items():
            if key in {"attachment_refs", "attachments"} or isinstance(nested, (dict, list)):
                _collect_attachment_refs(nested, source, out)
        return
    if isinstance(value, list):
        for item in value:
            _collect_attachment_refs(item, source, out)


async def _handle_attachments_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Attachment store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /attachments --limit 20.[/warning]")
        return
    if args:
        console.print("[warning]Usage: /attachments [--limit N] [--full].[/warning]")
        return
    refs: list[tuple[str, dict[str, Any]]] = []
    if state.session_id and hasattr(store, "get_session"):
        session = await store.get_session(state.session_id)
        if session:
            _collect_attachment_refs(getattr(session, "metadata", {}) or {}, f"session:{state.session_id}", refs)
    if state.session_id and hasattr(store, "get_session_transcript"):
        transcript = await store.get_session_transcript(state.session_id)
        for idx, item in enumerate(transcript):
            if isinstance(item, dict):
                message = item.get("message")
                _collect_attachment_refs(getattr(message, "metadata", {}) or {}, f"message:{idx}", refs)
                for part in item.get("parts", []) or []:
                    payload = part.get("payload", {}) if isinstance(part, dict) else getattr(part, "payload", {})
                    _collect_attachment_refs(payload, f"message:{idx}", refs)
    if hasattr(store, "get_tasks"):
        for task in await store.get_tasks(project_id=_current_project_id(state.engine)):
            if str(getattr(task, "session_id", "") or "") == state.session_id:
                _collect_attachment_refs(getattr(task, "metadata", {}) or {}, f"task:{getattr(task, 'id', '')}", refs)
                _collect_attachment_refs(getattr(task, "result", {}) or {}, f"task:{getattr(task, 'id', '')}", refs)
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for source, ref in refs:
        key = str(ref.get("attachment_id") or ref.get("disk_path") or ref)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((source, ref))
    if not deduped:
        console.print("[info]No attachments found for the current session.[/info]")
        return
    table = Table(title=f"Attachments ({len(deduped[:limit])})")
    table.add_column("Attachment ID")
    table.add_column("Filename")
    table.add_column("MIME")
    table.add_column("Size")
    table.add_column("Source")
    table.add_column("Path")
    for source, ref in deduped[:limit]:
        table.add_row(
            str(ref.get("attachment_id", "") or ""),
            str(ref.get("filename", "") or ""),
            str(ref.get("mime_type", "") or ""),
            str(ref.get("size_bytes", "") or ""),
            source,
            _clip_text(ref.get("disk_path", "") or "", 100, full=full),
        )
    console.print(table)


async def _handle_mode_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        table = Table(title="Mode")
        table.add_column("Mode", style="cyan")
        table.add_column("Current", justify="center")
        table.add_column("Command")
        table.add_row("task", "*" if state.mode == "task" else "", "/mode task")
        table.add_row("company/corporate", "*" if state.mode == "company" and state.company_profile == "corporate" else "", "/mode company corporate")
        table.add_row("company/custom", "*" if state.mode == "company" and state.company_profile == "custom" else "", "/mode company custom")
        console.print(table)
        console.print(f"[info]Current mode: {state.mode}; company_profile={state.company_profile}[/info]")
        return
    mode = args[0].strip().lower()
    if mode == "project":
        mode = "task"
    if mode not in _VALID_CHAT_MODES:
        console.print("[warning]Usage: /mode [task|company] [corporate|custom][/warning]")
        return
    next_company_profile = state.company_profile
    if mode == "company":
        if len(args) >= 2:
            profile = args[1].strip().lower()
            if profile not in _VALID_COMPANY_PROFILES:
                console.print("[warning]Company profile must be corporate or custom.[/warning]")
                return
            next_company_profile = profile
    state.mode = mode
    state.company_profile = next_company_profile
    await _persist_chat_context(state)
    console.print(f"[success]Mode set to {state.mode}[/success]")
    if state.mode == "company":
        console.print(f"[info]Company profile: {state.company_profile}[/info]")


async def _handle_permissions_slash(
    state: _InteractiveChatState,
    args: list[str],
) -> None:
    if not args:
        _render_native_permissions(
            state.config,
            current=state.native_approval_level,
        )
        console.print("[dim]Change this session with /permissions read-only, /permissions auto, or /permissions full-access.[/dim]")
        return
    if len(args) != 1:
        console.print("[warning]Usage: /permissions [read-only|auto|full-access][/warning]")
        return
    try:
        level = await _set_interactive_native_permission(state, args[0])
    except typer.BadParameter as exc:
        console.print(f"[warning]{escape(str(exc))}[/warning]")
        return
    console.print(f"[success]SuperOnes Native permissions set to {level} for this session.[/success]")
    console.print("[dim]The change takes effect at the next tool-call boundary; pending approvals are unchanged.[/dim]")


async def _handle_agent_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        table = Table(title="Agent")
        table.add_column("Agent", style="cyan")
        table.add_column("Current", justify="center")
        table.add_column("Command")
        for agent in ["native", "codex", "jiuwen", "jiuwenswarm", "claude_code", "cursor", "opencode"]:
            table.add_row(execution_agent_label(agent), "*" if state.preferred_agent == agent else "", f"/agent {agent}")
        table.add_row("system default", "*" if state.preferred_agent is None else "", "/agent none")
        console.print(table)
        preferred_label = (
            execution_agent_label(state.preferred_agent)
            if state.preferred_agent
            else "(system default)"
        )
        console.print(f"[info]Preferred agent: {preferred_label}[/info]")
        return
    command = args[0].strip().lower()
    if command in {"list", "ls"}:
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.list())
        if payload:
            _emit_payload(payload)
        return
    if command == "detail":
        if len(args) != 2:
            console.print("[warning]Usage: /agent detail <agent_id>[/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.detail(project_id=_current_project_id(state.engine), agent_id=args[1]))
        if payload:
            _emit_payload(payload)
        return
    if command == "create":
        rest = args[1:]
        try:
            rest, description = _extract_option(rest, "--description", default="")
            rest, office_id = _extract_option(rest, "--office", default="office-0")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 2:
            console.print("[warning]Usage: /agent create <name> <role_id> [--description ...] [--office ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.agent.create(name=rest[0], role_id=rest[1], office_id=office_id or "office-0", description=description or ""),
        )
        if payload:
            _emit_payload(payload)
        return
    if command == "create-from-template":
        rest = args[1:]
        try:
            rest, role_id = _extract_option(rest, "--role")
            rest, office_id = _extract_option(rest, "--office", default="office-0")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 1:
            console.print("[warning]Usage: /agent create-from-template <template_id> [--role <role_id>] [--office ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.agent.create_from_template(template_id=rest[0], role_id=role_id or rest[0], office_id=office_id or "office-0"),
        )
        if payload:
            _emit_payload(payload)
        return
    if command in {"import-employee", "import-agent"}:
        rest = args[1:]
        try:
            rest, office_id = _extract_option(rest, "--office", default="office-0")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 1:
            console.print("[warning]Usage: /agent import-employee <employee_id> [--office ...][/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.import_employee(employee_id=rest[0], office_id=office_id or "office-0"))
        if payload:
            _emit_payload(payload)
        return
    if command == "delete":
        if len(args) < 2:
            console.print("[warning]Usage: /agent delete <agent_id> --yes[/warning]")
            return
        remaining, ok = _require_yes_arg(args[2:], usage="/agent delete <agent_id> --yes")
        if not ok or remaining:
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.delete(args[1]))
        if payload:
            _emit_payload(payload)
        return
    if command == "move":
        rest = args[1:]
        try:
            rest, seat_zone = _extract_option(rest, "--seat-zone")
            rest, desk_id = _extract_option(rest, "--desk")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 2:
            console.print("[warning]Usage: /agent move <agent_id> <office_id> [--seat-zone ...] [--desk ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.agent.move(agent_id=rest[0], office_id=rest[1], seat_zone=seat_zone, desk_id=desk_id),
        )
        if payload:
            _emit_payload(payload)
        return
    agent = args[0].strip().lower().replace("-", "_")
    if agent in {"none", "auto", "default"}:
        state.preferred_agent = None
        await _persist_chat_context(state)
        console.print("[success]Preferred agent cleared; using system default.[/success]")
        return
    if agent not in _VALID_PREFERRED_AGENTS:
        valid = ", ".join(sorted([*_VALID_PREFERRED_AGENTS, "none"]))
        console.print(f"[warning]Unknown agent '{args[0]}'. Valid: {valid}[/warning]")
        return
    state.preferred_agent = agent
    await _persist_chat_context(state)
    console.print(f"[success]Preferred agent set to {execution_agent_label(agent)}[/success]")


def _handle_domains_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print(f"[info]Domains: {', '.join(state.domains) if state.domains else '(none)'}[/info]")
        return
    if len(args) == 1 and args[0].strip().lower() in {"clear", "none", "reset"}:
        state.domains = []
        console.print("[success]Domain hints cleared.[/success]")
        return
    state.domains = [arg.strip().lower() for arg in args if arg.strip()]
    console.print(f"[success]Domains set to: {', '.join(state.domains) if state.domains else '(none)'}[/success]")


def _staffing_checkpoint_key(checkpoint: Any) -> str:
    return str(
        getattr(checkpoint, "checkpoint_id", "")
        or getattr(checkpoint, "session_id", "")
        or "company_staffing_selection"
    )


def _company_staffing_payload(checkpoint: Any) -> dict[str, Any]:
    payload = getattr(checkpoint, "payload", {}) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _normalize_cli_staffing_selection(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"kind": "fallback", "id": ""}
    kind = str(value.get("kind", "") or "").strip().lower()
    if kind in {"employee", "emp"}:
        selected_id = str(value.get("id") or value.get("employee_id") or "").strip()
        return {"kind": "employee", "id": selected_id} if selected_id else {"kind": "fallback", "id": ""}
    if kind in {"template", "tpl"}:
        selected_id = str(value.get("id") or value.get("template_id") or "").strip()
        return {"kind": "template", "id": selected_id} if selected_id else {"kind": "fallback", "id": ""}
    return {"kind": "fallback", "id": ""}


def _company_staffing_default_draft(payload: dict[str, Any]) -> dict[str, Any]:
    selections: dict[str, dict[str, str]] = {}
    role_agents: dict[str, str] = {}
    for role in list(payload.get("staffing_roles", []) or []):
        role_id = str(role.get("role_id", "") or "").strip()
        if not role_id:
            continue
        selections[role_id] = _normalize_cli_staffing_selection(role.get("default_selection"))
        role_agents[role_id] = (
            str(role.get("selected_agent") or role.get("default_agent") or "codex").strip().lower().replace("-", "_")
            or "codex"
        )
    return {"staffing_selections": selections, "recruitment_role_agents": role_agents}


def _company_staffing_resume_metadata(draft: dict[str, Any], checkpoint: Any | None = None) -> dict[str, Any]:
    metadata = {
        "staffing_action": "manual_approve",
        "staffing_selections": dict(draft.get("staffing_selections", {}) or {}),
        "recruitment_role_agents": dict(draft.get("recruitment_role_agents", {}) or {}),
    }
    if checkpoint is not None:
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        if checkpoint_id:
            metadata["response_to_checkpoint_id"] = checkpoint_id
        metadata["response_to_checkpoint_type"] = "company_staffing_selection"
    return metadata


def _staffing_pool_maps(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    pool = dict(payload.get("staffing_pool", {}) or {})
    employees = {
        str(item.get("employee_id", "") or "").strip(): dict(item)
        for item in list(pool.get("employees", []) or [])
        if str(item.get("employee_id", "") or "").strip()
    }
    templates = {
        str(item.get("template_id", "") or "").strip(): dict(item)
        for item in list(pool.get("templates", []) or [])
        if str(item.get("template_id", "") or "").strip()
    }
    return employees, templates


def _company_staffing_selection_label(selection: Any, payload: dict[str, Any]) -> str:
    normalized = _normalize_cli_staffing_selection(selection)
    employees, templates = _staffing_pool_maps(payload)
    selected_id = normalized.get("id", "")
    if normalized["kind"] == "employee":
        employee = employees.get(selected_id, {})
        name = str(employee.get("employee_name", "") or selected_id)
        return f"{name} ({selected_id})"
    if normalized["kind"] == "template":
        template = templates.get(selected_id, {})
        name = str(template.get("template_name", "") or selected_id)
        return f"Hire template: {name} ({selected_id})"
    return "role-only fallback"


def _company_staffing_agent_choices(state: _InteractiveChatState) -> list[dict[str, Any]]:
    available: set[str] = {"native"}
    registry = getattr(state.engine, "adapter_registry", None)
    if registry and callable(getattr(registry, "list_available", None)):
        try:
            # The engine's own filter, not the registry's raw list: this is the literal 「offer」, and an agent
            # whose service the owner has not placed is not offered under 只用境内.
            allowed = getattr(state.engine, "_available_external_agents", None)
            names = allowed() if callable(allowed) else registry.list_available()
            available.update(str(item).strip().lower().replace("-", "_") for item in names)
        except Exception:
            pass
    ordered = ["native", "codex", "jiuwen", "jiuwenswarm", "claude_code", "cursor", "opencode"]
    return [
        {
            "agent": agent,
            "label": f"{execution_agent_label(agent)} ({'available' if agent in available else 'unavailable'})",
            "available": agent in available,
            "search": agent.replace("_", " "),
        }
        for agent in ordered
    ]


def _company_staffing_employee_choices(payload: dict[str, Any], role: dict[str, Any]) -> list[dict[str, Any]]:
    employees, templates = _staffing_pool_maps(payload)
    role_id = str(role.get("role_id", "") or "").strip()
    same_role_ids = [
        str(item or "").strip()
        for item in list(role.get("same_role_employee_ids", []) or [])
        if str(item or "").strip()
    ]
    choices: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append_employee(employee_id: str, prefix: str) -> None:
        employee = employees.get(employee_id)
        if not employee or employee_id in seen:
            return
        seen.add(employee_id)
        label = f"{prefix}: {employee.get('employee_name') or employee_id} ({employee_id})"
        choices.append(
            {
                "kind": "employee",
                "id": employee_id,
                "label": label,
                "search": " ".join(
                    str(part or "")
                    for part in [
                        employee_id,
                        employee.get("employee_name"),
                        employee.get("role_id"),
                        employee.get("category"),
                        " ".join(employee.get("domains", []) or []),
                        " ".join(employee.get("tags", []) or []),
                        employee.get("description"),
                    ]
                ).lower(),
            }
        )

    for employee_id in same_role_ids:
        _append_employee(employee_id, "same role")
    for employee_id, employee in employees.items():
        employee_role = str(employee.get("role_id", "") or "").strip()
        prefix = "same role" if employee_role == role_id else "employee"
        _append_employee(employee_id, prefix)
    for template_id, template in templates.items():
        choices.append(
            {
                "kind": "template",
                "id": template_id,
                "label": f"template: {template.get('template_name') or template_id} ({template_id})",
                "search": " ".join(
                    str(part or "")
                    for part in [
                        template_id,
                        template.get("template_name"),
                        template.get("category"),
                        " ".join(template.get("domains", []) or []),
                        " ".join(template.get("tags", []) or []),
                        template.get("description"),
                    ]
                ).lower(),
            }
        )
    if bool(role.get("fallback_available", True)):
        choices.append({"kind": "fallback", "id": "", "label": "fallback: role-only execution", "search": f"fallback role only {role_id}"})
    return choices


def _company_staffing_filter_options(options: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    normalized = str(query or "").strip().lower()
    if not normalized:
        return options
    tokens = [token for token in re.split(r"\s+", normalized) if token]
    if not tokens:
        return options
    return [
        option
        for option in options
        if all(token in f"{option.get('label', '')} {option.get('search', '')}".lower() for token in tokens)
    ]


def _render_company_staffing_summary(
    state: _InteractiveChatState,
    checkpoint: Any,
    draft: dict[str, Any],
) -> None:
    payload = _company_staffing_payload(checkpoint)
    selections = dict(draft.get("staffing_selections", {}) or {})
    role_agents = dict(draft.get("recruitment_role_agents", {}) or {})
    agent_status = {item["agent"]: item for item in _company_staffing_agent_choices(state)}
    table = Table(title="Company Staffing Preflight")
    table.add_column("Role", style="cyan")
    table.add_column("Employee / Template")
    table.add_column("Agent")
    table.add_column("Status")
    for role in list(payload.get("staffing_roles", []) or []):
        role_id = str(role.get("role_id", "") or "").strip()
        if not role_id:
            continue
        role_label = str(role.get("role_label", "") or role_id)
        agent = str(role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex").strip().lower().replace("-", "_")
        status = "available" if agent_status.get(agent, {}).get("available") else "unavailable"
        table.add_row(
            f"{role_label}\n[dim]{role_id}[/dim]",
            _company_staffing_selection_label(selections.get(role_id), payload),
            agent,
            status,
        )
    console.print(table)
    recommended = str(payload.get("recommended_action", "") or "").strip()
    summary = str(payload.get("summary", "") or "").strip()
    if summary:
        console.print(f"[dim]{summary}[/dim]")
    if recommended == "auto_recruit":
        console.print("[info]r auto recruit (recommended) | e edit | a approve selections | c context | d deny | /staffing reopen this editor later[/info]")
    else:
        console.print("[info]a approve selections | e edit | c context | r auto recruit | d deny | /staffing reopen this editor later[/info]")


def _render_company_staffing_context_preview(
    state: _InteractiveChatState,
    checkpoint: Any,
    draft: dict[str, Any],
) -> None:
    payload = _company_staffing_payload(checkpoint)
    original_message = _clip_text(payload.get("original_message"), 360, full=False) or "(empty request)"
    selections = dict(draft.get("staffing_selections", {}) or {})
    role_agents = dict(draft.get("recruitment_role_agents", {}) or {})
    table = Table(title="Initial Role Context Preview")
    table.add_column("Role", style="cyan")
    table.add_column("Agent")
    table.add_column("Staffing")
    table.add_column("Context Preview")
    for role in list(payload.get("staffing_roles", []) or []):
        role_id = str(role.get("role_id", "") or "").strip()
        if not role_id:
            continue
        role_label = str(role.get("role_label", "") or role_id).strip()
        role_responsibility = _clip_text(role.get("role_responsibility"), 180, full=False)
        selected_agent = str(role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex").strip()
        staffing_label = _company_staffing_selection_label(selections.get(role_id), payload)
        context_lines = [
            f"Owner request: {original_message}",
        ]
        if role_responsibility:
            context_lines.append(f"Role responsibility: {role_responsibility}")
        context_lines.append(
            "Runtime note: exact child WorkItem briefs are created by the manager at execution time; "
            "inspect them later with /work-items show or role logs."
        )
        table.add_row(
            f"{role_label}\n[dim]{role_id}[/dim]",
            selected_agent,
            _clip_text(staffing_label, 120, full=False),
            "\n".join(context_lines),
        )
    console.print(table)


async def _latest_company_staffing_checkpoint(state: _InteractiveChatState) -> Any | None:
    getter = getattr(state.engine, "get_latest_pending_checkpoint_for_session", None)
    if not callable(getter) or not state.session_id:
        return None
    try:
        checkpoint = await getter(state.session_id)
    except Exception:
        return None
    if str(getattr(checkpoint, "checkpoint_type", "") or "") != "company_staffing_selection":
        return None
    return checkpoint


def _apply_staffing_choice(draft: dict[str, Any], role_id: str, choice: dict[str, Any], *, column: str) -> None:
    if column == "agent":
        agent = str(choice.get("agent", "") or "").strip().lower().replace("-", "_")
        if agent:
            draft.setdefault("recruitment_role_agents", {})[role_id] = agent
        return
    kind = str(choice.get("kind", "") or "fallback").strip().lower()
    selected_id = str(choice.get("id", "") or "").strip()
    if kind not in {"employee", "template"}:
        draft.setdefault("staffing_selections", {})[role_id] = {"kind": "fallback", "id": ""}
    else:
        draft.setdefault("staffing_selections", {})[role_id] = {"kind": kind, "id": selected_id}


def _render_staffing_editor_text(
    state: _InteractiveChatState,
    payload: dict[str, Any],
    draft: dict[str, Any],
    row_index: int,
    column_index: int,
    query: str,
    choice_index: int,
) -> str:
    roles = [dict(role) for role in list(payload.get("staffing_roles", []) or []) if str(role.get("role_id", "") or "").strip()]
    if not roles:
        return "No staffing roles are available.\nPress q to exit."
    row_index = max(0, min(row_index, len(roles) - 1))
    role = roles[row_index]
    role_id = str(role.get("role_id", "") or "")
    column_name = "Employee/Template" if column_index == 0 else "Agent"
    options = (
        _company_staffing_employee_choices(payload, role)
        if column_index == 0
        else _company_staffing_agent_choices(state)
    )
    matches = _company_staffing_filter_options(options, query)[:8]
    lines = [
        "Company staffing editor",
        "Up/Down: role or match | Left/Right/Tab: column | type to search | Enter: select | Esc/q: save | Ctrl-C: cancel",
        "",
    ]
    selections = dict(draft.get("staffing_selections", {}) or {})
    role_agents = dict(draft.get("recruitment_role_agents", {}) or {})
    for idx, item in enumerate(roles):
        item_role_id = str(item.get("role_id", "") or "")
        marker = ">" if idx == row_index else " "
        selected = _company_staffing_selection_label(selections.get(item_role_id), payload)
        agent = str(role_agents.get(item_role_id) or item.get("selected_agent") or item.get("default_agent") or "codex")
        lines.append(f"{marker} {item.get('role_label') or item_role_id} [{item_role_id}]")
        lines.append(f"    Employee/Template: {selected}")
        lines.append(f"    Agent: {agent}")
    lines.extend(["", f"Editing {role.get('role_label') or role_id} / {column_name}", f"Search: {query or '(all)'}"])
    if not matches:
        lines.append("  no matches")
    for idx, option in enumerate(matches):
        marker = ">" if idx == choice_index else " "
        lines.append(f"  {marker} {option.get('label')}")
    return "\n".join(lines)


async def _edit_company_staffing_draft(
    state: _InteractiveChatState,
    checkpoint: Any,
    draft: dict[str, Any],
) -> dict[str, Any] | None:
    payload = _company_staffing_payload(checkpoint)
    roles = [dict(role) for role in list(payload.get("staffing_roles", []) or []) if str(role.get("role_id", "") or "").strip()]
    if not roles:
        console.print("[warning]This staffing checkpoint has no roles to edit.[/warning]")
        return draft
    working = {
        "staffing_selections": dict(draft.get("staffing_selections", {}) or {}),
        "recruitment_role_agents": dict(draft.get("recruitment_role_agents", {}) or {}),
    }
    try:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except Exception:
        return _edit_company_staffing_draft_fallback(payload, working)

    row_index = 0
    column_index = 0
    query = ""
    choice_index = 0
    kb = KeyBindings()

    def _current_options() -> list[dict[str, Any]]:
        role = roles[row_index]
        options = (
            _company_staffing_employee_choices(payload, role)
            if column_index == 0
            else _company_staffing_agent_choices(state)
        )
        return _company_staffing_filter_options(options, query)

    def _reset_choice() -> None:
        nonlocal choice_index
        choice_index = 0

    def _render() -> str:
        return _render_staffing_editor_text(state, payload, working, row_index, column_index, query, choice_index)

    @kb.add("up")
    def _up(event) -> None:  # pragma: no cover - exercised through prompt_toolkit in real terminals
        nonlocal row_index, choice_index
        if query:
            choice_index = max(0, choice_index - 1)
        else:
            row_index = max(0, row_index - 1)
            _reset_choice()

    @kb.add("down")
    def _down(event) -> None:  # pragma: no cover
        nonlocal row_index, choice_index
        if query:
            choice_index = min(max(0, len(_current_options()) - 1), choice_index + 1)
        else:
            row_index = min(len(roles) - 1, row_index + 1)
            _reset_choice()

    @kb.add("left")
    def _left(event) -> None:  # pragma: no cover
        nonlocal column_index, query
        column_index = 0
        query = ""
        _reset_choice()

    @kb.add("right")
    @kb.add("tab")
    def _right(event) -> None:  # pragma: no cover
        nonlocal column_index, query
        column_index = 1 if column_index == 0 else 0
        query = ""
        _reset_choice()

    @kb.add("backspace")
    def _backspace(event) -> None:  # pragma: no cover
        nonlocal query
        query = query[:-1]
        _reset_choice()

    @kb.add("enter")
    def _enter(event) -> None:  # pragma: no cover
        nonlocal query
        options = _current_options()
        if not options:
            return
        role_id = str(roles[row_index].get("role_id", "") or "")
        _apply_staffing_choice(working, role_id, options[min(choice_index, len(options) - 1)], column="agent" if column_index else "employee")
        query = ""
        _reset_choice()

    @kb.add("escape")
    @kb.add("q")
    def _save(event) -> None:  # pragma: no cover
        event.app.exit(result=True)

    @kb.add("c-c")
    def _cancel(event) -> None:  # pragma: no cover
        event.app.exit(result=False)

    @kb.add("/")
    def _slash(event) -> None:  # pragma: no cover
        nonlocal query
        query = ""
        _reset_choice()

    @kb.add("<any>")
    def _typing(event) -> None:  # pragma: no cover
        nonlocal query
        data = getattr(event, "data", "") or ""
        if data and data.isprintable():
            query += data
            _reset_choice()

    app_editor = Application(
        layout=Layout(HSplit([Window(content=FormattedTextControl(_render), wrap_lines=False)])),
        key_bindings=kb,
        full_screen=False,
    )
    try:
        saved = await app_editor.run_async()
    except Exception:
        return _edit_company_staffing_draft_fallback(payload, working)
    return working if saved else None


def _edit_company_staffing_draft_fallback(payload: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    console.print("[warning]Interactive editor unavailable. Use text overrides; blank line saves.[/warning]")
    console.print("Examples: senior_engineer=emp:employee-id | senior_engineer=tpl:template-id | senior_engineer=fallback | agent senior_engineer=codex")
    roles = {
        str(role.get("role_id", "") or "").strip()
        for role in list(payload.get("staffing_roles", []) or [])
        if str(role.get("role_id", "") or "").strip()
    }
    while True:
        try:
            raw = console.input("[bold]staffing edit> [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            return draft
        if not raw or raw.lower() in {"q", "quit", "save", "done"}:
            return draft
        if raw.lower().startswith("agent "):
            raw = raw.split(" ", 1)[1].strip()
            if "=" not in raw:
                console.print("[warning]Usage: agent <role_id>=<agent>[/warning]")
                continue
            role_id, agent = [part.strip() for part in raw.split("=", 1)]
            if role_id not in roles:
                console.print(f"[warning]Unknown role: {role_id}[/warning]")
                continue
            draft.setdefault("recruitment_role_agents", {})[role_id] = agent.lower().replace("-", "_")
            continue
        if "=" not in raw:
            console.print("[warning]Usage: <role_id>=emp:<id> | <role_id>=tpl:<id> | <role_id>=fallback[/warning]")
            continue
        role_id, selection = [part.strip() for part in raw.split("=", 1)]
        if role_id not in roles:
            console.print(f"[warning]Unknown role: {role_id}[/warning]")
            continue
        if selection.lower().startswith("emp:"):
            draft.setdefault("staffing_selections", {})[role_id] = {"kind": "employee", "id": selection.split(":", 1)[1].strip()}
        elif selection.lower().startswith("tpl:"):
            draft.setdefault("staffing_selections", {})[role_id] = {"kind": "template", "id": selection.split(":", 1)[1].strip()}
        else:
            draft.setdefault("staffing_selections", {})[role_id] = {"kind": "fallback", "id": ""}


async def _resume_company_staffing_checkpoint(
    state: _InteractiveChatState,
    checkpoint: Any,
    *,
    action: str,
    draft: dict[str, Any] | None = None,
    controller: ChatTurnController | None = None,
) -> None:
    metadata: dict[str, Any]
    content: str
    if action == "auto":
        content = "auto"
        metadata = {"staffing_action": "auto_recruit"}
    elif action == "deny":
        content = "deny"
        metadata = {"staffing_action": "deny"}
    else:
        content = "approve"
        metadata = _company_staffing_resume_metadata(draft or _company_staffing_default_draft(_company_staffing_payload(checkpoint)), checkpoint)
    if action in {"auto", "deny"}:
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        if checkpoint_id:
            metadata["response_to_checkpoint_id"] = checkpoint_id
        metadata["response_to_checkpoint_type"] = "company_staffing_selection"

    if controller is not None:
        await controller.submit_control_message(
            content,
            message_metadata=metadata,
            mode="company",
            company_profile=state.company_profile,
            preferred_agent=state.preferred_agent,
            domains=list(state.domains),
        )
        return

    state.runtime_display.begin_turn()
    response = await state.engine.process_message(
        content,
        project_id=getattr(state.engine, "project_id", None),
        session_id=state.session_id,
        mode="company",
        company_profile=state.company_profile,
        preferred_agent=state.preferred_agent,
        domains=list(state.domains),
        message_metadata=metadata,
    )
    await state.runtime_display.flush()
    await _sync_runtime_checkpoint_hint(state)
    if hasattr(state.runtime_display, "render_status"):
        await state.runtime_display.render_status(force=True)
    if not state.runtime_display.has_streamed_content:
        _print_response(response, state.no_markdown)


async def _maybe_run_company_staffing_preflight(state: _InteractiveChatState, controller: ChatTurnController | None = None) -> bool:
    checkpoint = await _latest_company_staffing_checkpoint(state)
    if checkpoint is None:
        return False
    key = _staffing_checkpoint_key(checkpoint)
    draft = state.company_staffing_drafts.setdefault(key, _company_staffing_default_draft(_company_staffing_payload(checkpoint)))
    while True:
        _render_company_staffing_summary(state, checkpoint, draft)
        try:
            choice = console.input("[bold]Company preflight [a/e/c/r/d]: [/bold]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("[warning]Staffing selection left pending. Reopen with /staffing.[/warning]")
            return True
        if choice in {"a", "approve", "y", "yes", ""}:
            await _resume_company_staffing_checkpoint(state, checkpoint, action="approve", draft=draft, controller=controller)
            state.company_staffing_drafts.pop(key, None)
            return True
        if choice in {"e", "edit"}:
            edited = await _edit_company_staffing_draft(state, checkpoint, draft)
            if edited is not None:
                draft.clear()
                draft.update(edited)
                state.company_staffing_drafts[key] = draft
            continue
        if choice in {"c", "context", "ctx", "preview"}:
            _render_company_staffing_context_preview(state, checkpoint, draft)
            continue
        if choice in {"r", "auto", "auto recruit", "autorecruit"}:
            await _resume_company_staffing_checkpoint(state, checkpoint, action="auto", draft=draft, controller=controller)
            state.company_staffing_drafts.pop(key, None)
            return True
        if choice in {"d", "deny", "cancel", "stop", "n", "no"}:
            await _resume_company_staffing_checkpoint(state, checkpoint, action="deny", draft=draft, controller=controller)
            state.company_staffing_drafts.pop(key, None)
            return True
        console.print("[warning]Choose a, e, c, r, or d.[/warning]")


async def _handle_company_staffing_shortcut(
    state: _InteractiveChatState,
    user_input: str,
    controller: ChatTurnController | None,
) -> bool:
    choice = str(user_input or "").strip().lower()
    if choice not in {
        "a",
        "approve",
        "y",
        "yes",
        "e",
        "edit",
        "c",
        "context",
        "ctx",
        "preview",
        "r",
        "auto",
        "auto recruit",
        "autorecruit",
        "d",
        "deny",
        "cancel",
        "stop",
        "n",
        "no",
    }:
        return False
    checkpoint = await _latest_company_staffing_checkpoint(state)
    if checkpoint is None:
        return False
    key = _staffing_checkpoint_key(checkpoint)
    draft = state.company_staffing_drafts.setdefault(
        key,
        _company_staffing_default_draft(_company_staffing_payload(checkpoint)),
    )
    if choice in {"a", "approve", "y", "yes"}:
        await _resume_company_staffing_checkpoint(state, checkpoint, action="approve", draft=draft, controller=controller)
        state.company_staffing_drafts.pop(key, None)
        return True
    if choice in {"e", "edit"}:
        edited = await _edit_company_staffing_draft(state, checkpoint, draft)
        if edited is not None:
            draft.clear()
            draft.update(edited)
            state.company_staffing_drafts[key] = draft
        _render_company_staffing_summary(state, checkpoint, draft)
        return True
    if choice in {"c", "context", "ctx", "preview"}:
        _render_company_staffing_context_preview(state, checkpoint, draft)
        return True
    if choice in {"r", "auto", "auto recruit", "autorecruit"}:
        await _resume_company_staffing_checkpoint(state, checkpoint, action="auto", draft=draft, controller=controller)
        state.company_staffing_drafts.pop(key, None)
        return True
    if choice in {"d", "deny", "cancel", "stop", "n", "no"}:
        await _resume_company_staffing_checkpoint(state, checkpoint, action="deny", draft=draft, controller=controller)
        state.company_staffing_drafts.pop(key, None)
        return True
    return False


async def _handle_staffing_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    if args and args[0].strip().lower() not in {"context", "ctx", "preview"}:
        console.print("[warning]Usage: /staffing [context][/warning]")
        return
    if not args and not await _maybe_run_company_staffing_preflight(state, controller=controller):
        console.print("[info]No pending company staffing selection for this session.[/info]")
        return
    if args:
        checkpoint = await _latest_company_staffing_checkpoint(state)
        if checkpoint is None:
            console.print("[info]No pending company staffing selection for this session.[/info]")
            return
        key = _staffing_checkpoint_key(checkpoint)
        draft = state.company_staffing_drafts.setdefault(key, _company_staffing_default_draft(_company_staffing_payload(checkpoint)))
        _render_company_staffing_context_preview(state, checkpoint, draft)


def _busy_slash_policy(command: str, args: list[str]) -> BusyCommandPolicy:
    readonly_roots = {
        "help",
        "?",
        "status",
        "permissions",
        "cost",
        "runtime",
        "tasks",
        "checkpoints",
        "checkpoint",
        "logs",
        "comms",
        "attachments",
        "work-items",
        "work-item",
        "queue",
        "kanban",
        "board",
        "stop",
        "continue",
    }
    if command in readonly_roots:
        return BusyCommandPolicy.IMMEDIATE_READONLY
    if command == "task":
        if args and args[0].lower() == "show":
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    if command == "project":
        if not args or args[0].lower() in {"list", "show"}:
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    if command == "session":
        if not args or args[0].lower() in {"list", "show", "stop", "continue"}:
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    if command == "staffing":
        if args and args[0].lower() == "context":
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    return BusyCommandPolicy.BLOCKED_WHEN_BUSY


def _print_busy_slash_block(command: str) -> None:
    console.print(
        f"[warning]Busy: /{escape(command)} changes chat context or runtime state, so it is blocked while a turn is running.[/warning]"
    )
    console.print("[dim]Available now: /stop, /kanban, /runtime, /work-items, /logs, /comms, /queue, /status.[/dim]")


async def _handle_queue_slash(controller: ChatTurnController | None, args: list[str]) -> None:
    if controller is None:
        console.print("[warning]/queue is only available in interactive chat.[/warning]")
        return
    command = args[0].lower() if args else "list"
    if command in {"list", "ls"}:
        items = controller.queued_items()
        if not items:
            console.print("[info]No queued prompts.[/info]")
            return
        table = Table(title=f"Queued Prompts ({len(items)})")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Project")
        table.add_column("Session")
        table.add_column("Mode")
        table.add_column("Prompt")
        for idx, item in enumerate(items, start=1):
            table.add_row(
                str(idx),
                item.project_id,
                _session_short_id(item.session_id),
                item.mode,
                _clip_text(item.text, 100),
            )
        console.print(table)
        return
    if command == "drop":
        if len(args) != 2:
            console.print("[warning]Usage: /queue drop <n>[/warning]")
            return
        try:
            index = int(args[1])
        except ValueError:
            console.print("[warning]Queue index must be a number.[/warning]")
            return
        item = controller.drop(index)
        if item is None:
            console.print("[warning]No queued prompt at that index.[/warning]")
            return
        console.print(f"[success]Dropped queued prompt #{index}:[/success] {_clip_text(item.text, 80)}")
        return
    if command == "clear":
        count = controller.clear()
        console.print(f"[success]Cleared {count} queued prompt(s).[/success]")
        return
    console.print("[warning]Usage: /queue list|drop <n>|clear[/warning]")


def _board_launch_args(state: _InteractiveChatState, args: list[str], *, default_view: str = "kanban") -> list[str]:
    view = default_view
    work_item = ""
    role = ""
    target = ""
    rest = list(args)
    if rest:
        candidate = rest.pop(0).lower()
        if candidate in {"kanban", "pipeline", "work-item", "role", "logs"}:
            view = candidate
        else:
            rest.insert(0, candidate)
    if view == "work-item" and rest:
        work_item = rest[0]
    elif view == "role" and rest:
        role = rest[0]
    elif view == "logs" and rest:
        target = rest[0]

    cmd = [
        sys.executable,
        "-m",
        "ampuly.cli.app",
        "board",
        "--project",
        _current_project_id(state.engine),
        "--attach",
        "--readonly",
        "--refresh-interval",
        "1.0",
        "--view",
        view,
    ]
    if state.session_id:
        cmd.extend(["--session", state.session_id])
    if work_item:
        cmd.extend(["--work-item", work_item])
    if role:
        cmd.extend(["--role", role])
    if target:
        cmd.extend(["--target", target])
    return cmd


async def _launch_board_inspector(state: _InteractiveChatState, args: list[str], *, default_view: str = "kanban") -> None:
    cmd = _board_launch_args(state, args, default_view=default_view)
    console.print("[dim]Opening read-only board inspector. Press q or Ctrl-Q in board to return to chat.[/dim]")
    display = state.runtime_display
    if hasattr(display, "enter_sidecar_quiet"):
        display.enter_sidecar_quiet()
    try:
        process = await asyncio.create_subprocess_exec(*cmd)
        await process.wait()
    except FileNotFoundError as exc:
        console.print(f"[warning]Could not launch board inspector: {escape(str(exc))}[/warning]")
    finally:
        quiet_events = 0
        if hasattr(display, "exit_sidecar_quiet"):
            quiet_events = int(display.exit_sidecar_quiet() or 0)
        suffix = f" Suppressed {quiet_events} runtime event(s); use /logs or /board logs to inspect them." if quiet_events else ""
        console.print(f"[dim]Returned to chat.{suffix}[/dim]")


async def _fetch_kanban_items(
    state: _InteractiveChatState,
    *,
    limit: int = 100,
    session_id: str | None = None,
    project_scope: bool = False,
) -> list[dict[str, Any]]:
    from ampuly.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from ampuly.plugins.office_ui.services.work_item import WorkItemService

    context = OfficeServiceContext(
        engine=state.engine,
        agent_store=None,
        chat_store=None,
        event_adapter=None,
        mode_state=ModeState(
            exec_mode=state.mode,
            company_profile=state.company_profile,
            task_preferred_agent=state.preferred_agent or "native",
        ),
    )
    scoped_session_id = None if project_scope else str(session_id or state.session_id or "").strip()
    result = await WorkItemService(context).list(
        project_id=_current_project_id(state.engine),
        session_id=scoped_session_id,
        limit=limit,
        kanban_visible_only=True,
    )
    return list(result.payload.get("work_items", []) or [])


def _kanban_items_fingerprint(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        rows.append((
            str(item.get("work_item_id", "") or ""),
            str(item.get("title", "") or ""),
            str(item.get("role_id", "") or ""),
            str(item.get("phase", "") or ""),
            str(item.get("kanban_column", "") or ""),
            str(item.get("runtime_task_id", "") or ""),
            str(item.get("session_id", "") or ""),
            str(item.get("updated_at", "") or ""),
        ))
    return json.dumps(sorted(rows), ensure_ascii=False, sort_keys=True)


def _render_kanban_items(items: list[dict[str, Any]], *, title: str = "Kanban Work Items") -> None:
    if not items:
        console.print("[info]No company work items found yet.[/info]")
        return
    counts: dict[str, int] = {}
    for item in items:
        column = str(item.get("kanban_column") or item.get("phase") or "unknown")
        counts[column] = counts.get(column, 0) + 1
    summary = ", ".join(f"{column}:{count}" for column, count in sorted(counts.items()))
    table = Table(title=f"{title} ({len(items)} | {summary})")
    table.add_column("Work Item")
    table.add_column("Title")
    table.add_column("Role")
    table.add_column("Column")
    table.add_column("Phase")
    table.add_column("Runtime Task")
    table.add_column("Updated")
    for item in items:
        table.add_row(
            str(item.get("work_item_id", "") or "")[:12],
            _clip_text(item.get("title", "") or "", 64),
            str(item.get("role_id", "") or ""),
            str(item.get("kanban_column", "") or ""),
            str(item.get("phase", "") or ""),
            str(item.get("runtime_task_id", "") or "")[:12],
            _clip_text(item.get("updated_at", "") or "", 19),
        )
    console.print(table)


async def _handle_kanban_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    normalized = [arg.strip().lower() for arg in args]
    if normalized and normalized[0] in {"stop", "off", "clear"}:
        if controller is None:
            console.print("[warning]Kanban live watch is only available in interactive chat.[/warning]")
            return
        await controller.stop_kanban_watch()
        return
    watch = True
    project_scope = False
    for token in normalized:
        if token in {"once", "--once", "--summary", "summary"}:
            watch = False
        elif token in {"all", "project", "--all", "--project"}:
            project_scope = True
        else:
            console.print("[warning]Usage: /kanban [once|stop|all][/warning]")
            return
    session_id = str(state.session_id or "").strip()
    title = "Kanban Work Items (project)" if project_scope else f"Kanban Work Items (session {session_id[:8]})"
    try:
        items = await _fetch_kanban_items(state, limit=100, session_id=session_id, project_scope=project_scope)
    except Exception as exc:
        console.print(f"[warning]Could not load kanban work items: {escape(str(exc))}[/warning]")
        return
    _render_kanban_items(items, title=title)
    if watch and controller is not None:
        await controller.start_kanban_watch(
            initial_fingerprint=_kanban_items_fingerprint(items),
            session_id=session_id,
            project_scope=project_scope,
        )
    elif watch:
        console.print("[dim]Live watch is only available in interactive chat.[/dim]")


async def _handle_board_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        args = ["kanban"]
    await _launch_board_inspector(state, args, default_view="kanban")


async def _handle_chat_slash_command(state: _InteractiveChatState, user_input: str, controller: ChatTurnController | None = None) -> bool:
    if not user_input.startswith("/"):
        return False
    try:
        parts = _slash_parts(user_input)
    except ValueError as exc:
        console.print(f"[warning]Could not parse command: {exc}[/warning]")
        return True
    if not parts:
        _print_slash_help()
        return True

    command = _canonical_slash_command(parts[0])
    args = parts[1:]
    if controller is not None and controller.is_busy and command in _slash_command_names():
        policy = _busy_slash_policy(command, args)
        if policy == BusyCommandPolicy.BLOCKED_WHEN_BUSY:
            _print_busy_slash_block(command)
            return True
    if command in {"help", "?"}:
        _print_slash_help()
    elif command == "queue":
        await _handle_queue_slash(controller, args)
    elif command == "status":
        _print_interactive_status(state.engine, state)
    elif command == "cost":
        _print_interactive_cost(state.engine)
    elif command == "mode":
        await _handle_mode_slash(state, args)
    elif command == "permissions":
        await _handle_permissions_slash(state, args)
    elif command == "agent":
        await _handle_agent_slash(state, args)
    elif command == "domains":
        _handle_domains_slash(state, args)
    elif command == "project":
        await _handle_project_slash(state, args)
    elif command in {"new-session", "new_session"}:
        _start_new_chat_session(state)
    elif command == "sessions":
        await _render_sessions(state, args)
    elif command == "resume":
        await _resume_chat_session(state, args[0] if args else "")
    elif command == "session":
        await _handle_session_slash(state, args, controller=controller)
    elif command == "stop":
        await _handle_stop_slash(state, args, controller=controller)
    elif command == "continue":
        await _handle_continue_slash(state, args, controller=controller)
    elif command == "tasks":
        await _handle_tasks_slash(state, args)
    elif command == "task":
        await _handle_task_slash(state, args)
    elif command in {"checkpoints", "checkpoint"}:
        await _handle_checkpoints_slash(state, args)
    elif command == "org":
        await _handle_org_slash(state, args)
    elif command == "talent":
        await _handle_talent_slash(state, args)
    elif command == "market":
        await _handle_market_slash(state, args)
    elif command == "reorg":
        await _handle_reorg_slash(state, args)
    elif command == "runtime":
        await _handle_runtime_slash(state, args)
    elif command in {"work-items", "work-item"}:
        await _handle_work_items_slash(state, args)
    elif command == "logs":
        await _handle_logs_slash(state, args)
    elif command == "comms":
        await _handle_comms_slash(state, args)
    elif command == "attachments":
        await _handle_attachments_slash(state, args)
    elif command == "staffing":
        await _handle_staffing_slash(state, args, controller=controller)
    elif command == "kanban":
        await _handle_kanban_slash(state, args, controller=controller)
    elif command == "board":
        await _handle_board_slash(state, args)
    else:
        console.print(f"[warning]Unknown command: /{escape(command)}. Try /help for available commands.[/warning]")
    return True


async def _sync_runtime_checkpoint_hint(state: _InteractiveChatState) -> None:
    display = state.runtime_display
    if not hasattr(display, "set_checkpoint_hint"):
        return
    runtime_identity = await _company_runtime_identity_for_session(state)
    if runtime_identity is not None and runtime_identity.checkpoint is not None:
        checkpoint = runtime_identity.checkpoint
        checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "pending")
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "")
        display.set_checkpoint_hint(checkpoint_type or checkpoint_id)
        return
    getter = getattr(state.engine, "get_latest_pending_checkpoint_for_session", None)
    if not callable(getter) or not state.session_id:
        display.set_checkpoint_hint("")
        return
    try:
        checkpoint = await getter(state.session_id)
    except Exception:
        return
    if not checkpoint:
        display.set_checkpoint_hint("")
        return
    checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "pending")
    checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "")
    display.set_checkpoint_hint(checkpoint_type or checkpoint_id)


async def _process_interactive_chat_message(
    state: _InteractiveChatState,
    user_input: str,
    *,
    message_metadata: dict[str, Any] | None = None,
    interactive_followups: bool = True,
) -> None:
    console.print()
    try:
        state.runtime_display.begin_turn()
        effective_metadata = message_metadata
        execution_session_id = state.session_id
        execution_origin_task_id: str | None = None
        execution_mode = state.mode
        execution_company_profile = state.company_profile
        execution_org_id = state.org_id
        execution_preferred_agent = state.preferred_agent
        handoff_identity = None
        runtime_identity = await _company_runtime_identity_for_session(state)
        runtime_checkpoint = (
            runtime_identity.checkpoint
            if runtime_identity is not None
            else None
        )
        explicit_runtime_type = str(
            (effective_metadata or {}).get("response_to_checkpoint_type", "") or ""
        ).strip()
        explicit_runtime_id = str(
            (effective_metadata or {}).get("response_to_checkpoint_id", "") or ""
        ).strip()
        is_explicit_runtime_handoff = explicit_runtime_type in {
            "company_runtime_suspended",
            "company_runtime_interrupted",
        }

        if is_explicit_runtime_handoff:
            if runtime_identity is None or runtime_checkpoint is None:
                raise RuntimeError(
                    "Company runtime checkpoint does not match the current session."
                )
            checkpoint_id = str(
                getattr(runtime_checkpoint, "checkpoint_id", "") or ""
            ).strip()
            checkpoint_type = str(
                getattr(runtime_checkpoint, "checkpoint_type", "") or ""
            ).strip()
            checkpoint_status = str(
                getattr(runtime_checkpoint, "status", "") or ""
            ).strip().lower()
            if (
                not explicit_runtime_id
                or explicit_runtime_id != checkpoint_id
                or explicit_runtime_type != checkpoint_type
            ):
                raise RuntimeError(
                    "Company runtime checkpoint identity mismatch; refresh before continuing."
                )
            if checkpoint_status != "pending":
                raise RuntimeError(
                    f"Company runtime checkpoint is {checkpoint_status or 'not pending'}."
                )
            execution_session_id = runtime_identity.runtime_session_id
            execution_origin_task_id = runtime_identity.ui_anchor_task_id or None
            handoff_identity = runtime_identity
        elif effective_metadata is None and runtime_checkpoint is not None:
            checkpoint_status = str(
                getattr(runtime_checkpoint, "status", "") or ""
            ).strip().lower()
            if checkpoint_status != "pending":
                raise RuntimeError(
                    f"Company runtime checkpoint is {checkpoint_status or 'not pending'}."
                )
            if runtime_identity is None:
                raise RuntimeError(
                    "Company runtime checkpoint does not match the current session."
                )
            effective_metadata = {
                "response_to_checkpoint_id": str(getattr(runtime_checkpoint, "checkpoint_id", "") or ""),
                "response_to_checkpoint_type": str(getattr(runtime_checkpoint, "checkpoint_type", "") or "company_runtime_suspended"),
            }
            execution_session_id = runtime_identity.runtime_session_id
            execution_origin_task_id = runtime_identity.ui_anchor_task_id or None
            handoff_identity = runtime_identity
        elif effective_metadata is None:
            if runtime_identity is not None and runtime_identity.pending_checkpoint_id:
                # An active checkpoint may only be pending or resuming.  Never
                # let a malformed identity/status fall through as a new turn.
                raise RuntimeError(
                    "Company runtime checkpoint is not available for a new turn."
                )
            if state.mode == "company":
                effective_metadata = {"company_preflight": "manual"}
        if handoff_identity is not None:
            runtime_execution_identity = await _company_runtime_execution_identity(
                state,
                handoff_identity,
            )
            # A selected role channel resumes the root Company runtime.  Its
            # native permission scope follows that root, never the UI child
            # session that happened to be selected.
            state.native_permission_scope_id = execution_session_id
            execution_mode = runtime_execution_identity.exec_mode
            execution_company_profile = runtime_execution_identity.company_profile
            execution_org_id = runtime_execution_identity.org_id
            execution_preferred_agent = runtime_execution_identity.preferred_agent
        effective_metadata = {
            **dict(effective_metadata or {}),
            "native_approval_level": state.native_approval_level,
            "native_permission_scope_id": str(
                state.native_permission_scope_id or state.session_id
            ),
        }
        response = await state.engine.process_message(
            user_input,
            project_id=getattr(state.engine, "project_id", None),
            session_id=execution_session_id,
            mode=execution_mode,
            org_id=execution_org_id or None,
            company_profile=(
                execution_company_profile
                if execution_mode == "company"
                else None
            ),
            preferred_agent=execution_preferred_agent,
            domains=list(state.domains),
            origin_task_id=execution_origin_task_id,
            message_metadata=effective_metadata,
        )
        await state.runtime_display.flush()
        await _sync_runtime_checkpoint_hint(state)
        handled_staffing_preflight = False
        if state.mode == "company":
            if interactive_followups:
                handled_staffing_preflight = await _maybe_run_company_staffing_preflight(state)
            else:
                checkpoint = await _latest_company_staffing_checkpoint(state)
                if checkpoint is not None:
                    key = _staffing_checkpoint_key(checkpoint)
                    draft = state.company_staffing_drafts.setdefault(
                        key,
                        _company_staffing_default_draft(_company_staffing_payload(checkpoint)),
                    )
                    _render_company_staffing_summary(state, checkpoint, draft)
                    console.print("[dim]Type a/e/c/r/d at ampuly>; /staffing is only a fallback reopen command.[/dim]")
                    handled_staffing_preflight = True
        if hasattr(state.runtime_display, "render_status") and not handled_staffing_preflight:
            await state.runtime_display.render_status(force=True)
        if not state.runtime_display.has_streamed_content and not handled_staffing_preflight:
            _print_response(response, state.no_markdown)
        if effective_metadata and (
            effective_metadata.get("ui_force_resume")
            or str(effective_metadata.get("response_to_checkpoint_type", "") or "") in {
                "company_runtime_suspended",
                "company_runtime_interrupted",
            }
        ):
            remaining_checkpoint = await _latest_company_suspend_checkpoint(state)
            if remaining_checkpoint is not None:
                state.runtime_control_state = "suspended"
                state.runtime_control_checkpoint_id = str(
                    getattr(remaining_checkpoint, "checkpoint_id", "") or ""
                )
            else:
                state.runtime_control_state = "idle"
                state.runtime_control_checkpoint_id = ""
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted. Type /quit to exit.[/warning]")
    except Exception as e:
        console.print(f"[error]Error: {escape(str(e))}[/error]")


async def _interactive_mode(
    config,
    project: str | None,
    no_markdown: bool,
    *,
    mode: str = "task",
    preferred_agent: str | None = None,
    company_profile: str | None = None,
    explicit_mode: bool = False,
    explicit_agent: bool = False,
    explicit_company_profile: bool = False,
    native_approval_level: NativeApprovalLevel = "auto",
    explicit_native_approval_level: bool = False,
) -> None:
    engine, runtime_display = _create_cli_engine(config, project)

    try:
        await engine.initialize()
    except Exception as e:
        console.print(f"[error]Failed to initialize: {escape(str(e))}[/error]")
        return

    state = _InteractiveChatState(
        config=config,
        engine=engine,
        runtime_display=runtime_display,
        session_id="",
        no_markdown=no_markdown,
        mode="company" if str(mode or "").strip().lower() == "company" else "task",
        company_profile=company_profile or _initial_company_profile(config),
        preferred_agent=_normalize_interactive_preferred_agent(preferred_agent),
        native_approval_level=native_approval_level,
    )
    _attach_cli_runtime_callbacks(state)
    await _restore_chat_context(
        state,
        restore_mode=not explicit_mode,
        restore_company_profile=not explicit_company_profile,
        restore_agent=not explicit_agent,
    )

    console.print(Panel(
        f"[bold]Ampuly Engine v{__version__}[/bold]\n"
        f"Model: {config.llm.default_model}\n"
        f"Current mode: {state.mode}"
        f"{f' ({state.company_profile})' if state.mode == 'company' else ''}\n"
        f"Current agent: {execution_agent_label(state.preferred_agent) if state.preferred_agent else 'system'}\n"
        f"Native permissions: {state.native_approval_level}\n"
        f"Choose a project and session to begin.",
        border_style="blue",
    ))
    try:
        await _run_interactive_startup_selector(state, explicit_project=bool(project))
        if explicit_native_approval_level:
            await _set_interactive_native_permission(
                state,
                native_approval_level,
            )
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/warning]")
        await state.engine.shutdown()
        return

    controller = ChatTurnController(state)
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style

        history_path = state.engine.ampuly_home / "cli_history"
        session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=_AmpulySlashCompleter(),
            complete_while_typing=True,
            bottom_toolbar=lambda: _chat_bottom_toolbar_text(state, controller),
            style=Style.from_dict({"bottom-toolbar": "fg:#888888"}),
        )

        while True:
            await _print_pending_checkpoint_hint(state.engine, state.session_id)
            try:
                with patch_stdout(raw=True):
                    user_input = await session.prompt_async("ampuly> ")
            except (EOFError, KeyboardInterrupt):
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if user_input.startswith("/"):
                await _handle_chat_slash_command(state, user_input, controller=controller)
                continue
            if await _handle_company_staffing_shortcut(state, user_input, controller):
                continue

            await controller.submit_user_message(user_input)
            continue

    except ImportError:
        console.print("[warning]prompt-toolkit not available, using basic input[/warning]")
        while True:
            await _print_pending_checkpoint_hint(state.engine, state.session_id)
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("ampuly> ")
                )
                user_input = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                await _handle_chat_slash_command(state, user_input, controller=controller)
                continue
            if await _handle_company_staffing_shortcut(state, user_input, controller):
                continue
            await controller.submit_user_message(user_input)
            continue

    finally:
        await controller.shutdown()
        await state.engine.shutdown()
        console.print("[info]Goodbye![/info]")


async def _interactive_secretary_mode(config, project: str | None, no_markdown: bool) -> None:
    engine, runtime_display = _create_cli_engine(config, project)

    try:
        await engine.initialize()
    except Exception as e:
        console.print(f"[error]Failed to initialize: {escape(str(e))}[/error]")
        return

    console.print(Panel(
        f"[bold]AMPULY Secretary[/bold]\n"
        f"Model: {config.llm.default_model}\n"
        f"Project: {project or '(none)'}\n"
        f"Commands: /quit, /project <id>, /new-session, /sessions, /resume <session_id>, /policies, /help",
        border_style="green",
    ))

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        history_path = engine.ampuly_home / "secretary_cli_history"
        prompt = PromptSession(history=FileHistory(str(history_path)))
        current_session_id, restored_latest = await _resolve_secretary_session_id(engine)
        if restored_latest:
            console.print(f"[info]Restored recent secretary session: {current_session_id}[/info]")
        else:
            console.print(f"[info]Started a new secretary session: {current_session_id}[/info]")

        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: prompt.prompt("secretary> ")
                )
            except (EOFError, KeyboardInterrupt):
                break
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if user_input.startswith("/project "):
                new_project = user_input.split(" ", 1)[1].strip()
                await engine.shutdown()
                engine, runtime_display = _create_cli_engine(config, new_project)
                await engine.initialize()
                current_session_id, restored_latest = await _resolve_secretary_session_id(engine)
                console.print(f"[info]Switched to project: {new_project}[/info]")
                if restored_latest:
                    console.print(f"[info]Restored recent secretary session: {current_session_id}[/info]")
                else:
                    console.print(f"[info]Started a new secretary session: {current_session_id}[/info]")
                continue
            if user_input == "/new-session":
                current_session_id = str(uuid.uuid4())
                console.print(f"[info]Started a new secretary session: {current_session_id}[/info]")
                continue
            if user_input == "/sessions":
                sessions = await engine.secretary.list_sessions(engine.project_id, limit=20) if engine.secretary else []
                if not sessions:
                    console.print("[info]No secretary sessions found for this project.[/info]")
                else:
                    console.print("[bold]Recent secretary sessions:[/bold]")
                    for item in sessions:
                        title = item.title or "(untitled)"
                        console.print(f"  - {item.session_id} :: {title}")
                continue
            if user_input.startswith("/resume "):
                requested_session_id = user_input.split(" ", 1)[1].strip()
                target = await engine.store.get_session(requested_session_id) if engine.store else None
                if not target or target.metadata.get("interface") != "secretary":
                    console.print(f"[warning]Secretary session not found: {requested_session_id}[/warning]")
                    continue
                current_project = engine.project_id or "default"
                target_project = target.project_id or "default"
                if target_project != current_project:
                    console.print(
                        "[warning]"
                        f"Secretary session {requested_session_id} belongs to project '{target_project}'. "
                        f"Switch project first with /project {target_project}."
                        "[/warning]"
                    )
                    continue
                current_session_id = requested_session_id
                console.print(f"[info]Resumed secretary session: {current_session_id}[/info]")
                continue
            if user_input == "/policies":
                summary = engine.secretary.describe_policies(engine.project_id) if engine.secretary else ""
                _print_response(summary, no_markdown)
                continue
            if user_input == "/help":
                console.print(
                    "/quit — Exit\n"
                    "/project <id> — Switch project\n"
                    "/new-session — Start a fresh secretary session\n"
                    "/sessions — List secretary sessions for this project\n"
                    "/resume <session_id> — Resume an existing secretary session\n"
                    "/policies — Show active secretary policies\n"
                    "/help — This help"
                )
                continue

            runtime_display.begin_turn()
            payload = await engine.process_secretary_message(
                user_input,
                project_id=engine.project_id,
                session_id=current_session_id,
            )
            await runtime_display.flush()
            if not runtime_display.has_streamed_content:
                _print_response(payload.get("response", ""), no_markdown)

    except ImportError:
        console.print("[warning]prompt-toolkit not available, using basic input[/warning]")
    finally:
        await engine.shutdown()
        console.print("[info]Goodbye![/info]")


async def _show_cost_summary(config) -> None:
    from ampuly.core.config import get_ampuly_home
    db_path = get_ampuly_home() / "global.db"
    if not db_path.exists():
        return
    store = AmpulyStore(db_path)
    await store.initialize()
    try:
        costs = await store.get_total_cost()
        if costs["total_calls"] > 0:
            console.print("\n[bold]Cost Summary:[/bold]")
            console.print(f"  Total calls: {costs['total_calls']}")
            console.print(f"  Total tokens: {costs['total_tokens_in'] + costs['total_tokens_out']}")
            console.print(f"  Total cost: ${costs['total_cost']:.4f}")
    finally:
        await store.close()


async def _show_autonomy_summary(config, project: str | None = None) -> None:
    from ampuly.core.config import get_ampuly_home
    from ampuly.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from ampuly.layer5_memory.preference import PreferenceManager

    db_path = get_ampuly_home() / "global.db"
    if not db_path.exists():
        return
    store = AmpulyStore(db_path)
    await store.initialize()
    try:
        stats = await store.get_autonomy_stats(project_id=project)
        console.print("\n[bold]Autonomy Summary:[/bold]")
        console.print(f"  Decisions: {stats['total']}")
        console.print(f"  Auto-approved: {stats['auto_approved']}")
        console.print(f"  Escalated: {stats['escalated']}")
        console.print(f"  Rejected: {stats['rejected']}")
        console.print(f"  Auto-approval rate: {stats['auto_approval_rate']:.0%}")

        prefs = PreferenceManager(get_ampuly_home())
        learned = prefs.get_autonomy_preferences(project_id=project).get("learned_actions", {})
        if learned:
            console.print("  Learned actions:")
            for name, data in list(learned.items())[:10]:
                console.print(
                    f"    - {name}: approvals={data.get('approvals', 0)}, "
                    f"rejections={data.get('rejections', 0)}, "
                    f"explicit_allow={data.get('explicit_allow', False)}, "
                    f"explicit_deny={data.get('explicit_deny', False)}"
                )
        allowlist_lines = ApprovalAllowlistManager(get_ampuly_home()).summarize(project_id=project, limit=10)
        if allowlist_lines:
            console.print("  Persisted allowlist:")
            for line in allowlist_lines:
                console.print(f"    {line}")
    finally:
        await store.close()


def _print_response(text: str, no_markdown: bool = False) -> None:
    if no_markdown:
        console.print(text)
    else:
        try:
            md = Markdown(text)
            console.print(md)
        except Exception:
            console.print(text)
    console.print()


async def _print_pending_checkpoint_hint(engine, session_id: str) -> None:
    checkpoint = await engine.get_latest_pending_checkpoint_for_session(session_id)
    if not checkpoint:
        return
    if checkpoint.checkpoint_type == "company_recruitment_confirmation":
        console.print(
            "[info]Pending staffing confirmation for this session. "
            "Reply `1` or `approve` to continue, `2` or `deny` to cancel. "
            "Any other input will be treated as feedback/suggestions for revising the proposal.[/info]"
        )
        return
    if checkpoint.checkpoint_type == "company_staffing_selection":
        payload = dict(getattr(checkpoint, "payload", {}) or {})
        recommended = str(payload.get("recommended_action", "") or "").strip()
        if recommended == "auto_recruit":
            console.print(
                "[info]Pending company staffing preflight for this session. "
                "`auto recruit` is recommended; use `/staffing` to review/edit employee, template, and agent choices.[/info]"
            )
        else:
            console.print(
                "[info]Pending manual staffing selection for this session. "
                "Use `/staffing` to review/edit employee and agent choices, reply `approve` to use defaults, "
                "or `auto` / `auto recruit` to run automatic recruitment.[/info]"
            )
        return
    if checkpoint.checkpoint_type == "company_work_item_gate":
        console.print(
            "[info]Pending work-item confirmation for this session. "
            "Reply with `approve` / `continue` or `deny` / `stop`.[/info]"
        )
        return
    if checkpoint.checkpoint_type == "company_delivery_feedback":
        console.print(
            "[info]Pending delivery self-evolution review for this session. "
            "Reply with `approve` to record agreement, or explicit feedback to update employee experience. "
            "Start normal follow-up work from the regular conversation after this review is handled.[/info]"
        )
        return
    if checkpoint.checkpoint_type in {"task_user_input", "task_peer_wait", "company_peer_wait"}:
        console.print("[info]Pending checkpoint for this session. Your next reply will resume that confirmation flow.[/info]")


def _print_interactive_status(engine, state: _InteractiveChatState | None = None) -> None:
    if state is not None:
        _print_context_status(state)
    if getattr(engine, "llm", None):
        stats = getattr(engine.llm, "stats", {}) or {}
        model = getattr(getattr(getattr(engine, "config", None), "llm", None), "default_model", "unknown")
        console.print(f"  Model: {model}")
        console.print(f"  Project: {_current_project_id(engine)}")
        console.print(f"  Tokens: in={stats.get('tokens_in', 0)}, out={stats.get('tokens_out', 0)}")
        console.print(f"  Cost: ${float(stats.get('estimated_cost', 0.0) or 0.0):.4f}")
    if getattr(engine, "adapter_registry", None):
        available = engine.adapter_registry.list_available()
        console.print(f"  External agents: {', '.join(available) if available else 'none'}")


async def _load_recent_primary_session(store: AmpulyStore | None, project_id: str | None) -> tuple[str | None, str]:
    sessions = await _load_recent_primary_sessions(store, project_id, limit=1)
    if not sessions:
        return None, ""
    latest = sessions[0]
    return latest.session_id, (latest.title or "").strip()


async def _resolve_starting_session_id(engine) -> tuple[str, bool]:
    session_id, _ = await _load_recent_primary_session(engine.store, engine.project_id)
    if session_id:
        return session_id, True
    return str(uuid.uuid4()), False


async def _load_recent_secretary_session(engine) -> tuple[str | None, str]:
    if not engine.secretary:
        return None, ""
    sessions = await engine.secretary.list_sessions(engine.project_id, limit=1)
    if not sessions:
        return None, ""
    latest = sessions[0]
    return latest.session_id, (latest.title or "").strip()


async def _resolve_secretary_session_id(engine) -> tuple[str, bool]:
    session_id, _ = await _load_recent_secretary_session(engine)
    if session_id:
        return session_id, True
    return str(uuid.uuid4()), False


async def _run_channel_runtime(config, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        if not engine.channel_manager:
            console.print("[warning]Channel manager is not available.[/warning]")
            return
        await engine.channel_manager.start_all()
        if not engine.channel_manager.enabled_channels:
            console.print("[warning]No channels enabled. Update .ampuly/config/channel_config.yaml first.[/warning]")
            return
        _write_channel_runtime_state(engine.channel_manager.enabled_channels)
        console.print(f"[success]Channel runtime started:[/success] {', '.join(engine.channel_manager.enabled_channels)}")
        await engine.message_bus.start()
    except KeyboardInterrupt:
        console.print("\n[warning]Stopping channel runtime...[/warning]")
    finally:
        _clear_channel_runtime_state()
        await engine.shutdown()


# ── Plugins ────────────────────────────────────────────────────────────────
try:
    from ampuly.plugins.office_ui import register_cli
    register_cli(app)
except ImportError:
    pass

try:
    from ampuly.plugins.cli_board import register_cli as register_cli_board
    register_cli_board(app)
except ImportError:
    pass


def main():
    keylog_path = pop_windows_sslkeylogfile()
    if keylog_path:
        console.print(
            f"[warning]{escape(format_windows_sslkeylog_warning(_current_command_label(), keylog_path))}[/warning]"
        )
    app()


if __name__ == "__main__":
    main()
