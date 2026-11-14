"""Office-UI Plugin — visual frontend for the AMPULY multi-agent system.

Registers the `ampuly ui` CLI command when aiohttp is available.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ampuly.core.windows_ssl import (
    format_windows_sslkeylog_warning,
    pop_windows_sslkeylogfile,
)
from ampuly.plugins.office_ui.terminal import error as terminal_error
from ampuly.plugins.office_ui.terminal import status as terminal_status

if TYPE_CHECKING:
    import typer

_PLUGIN_DIR = Path(__file__).parent
_FRONTEND_SRC = _PLUGIN_DIR / "frontend_src"
_FRONTEND_DIST = _PLUGIN_DIR / "frontend_dist"
# Committed next to index.html. Vite's emptyOutDir wipes it; _ensure_frontend
# writes it again after `npm run build`.
_FRONTEND_SOURCE_STAMP = "source-stamp.txt"
_TEXT_SOURCE_SUFFIXES = {
    ".cjs",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".svg",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def _latest_mtime(root: Path, *, exclude_dirs: set[str] | None = None) -> float:
    exclude = set(exclude_dirs or set())
    latest = 0.0
    if not root.is_dir():
        return latest
    for path in root.rglob("*"):
        if any(part in exclude for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _source_file_ignored(path: Path, src: Path) -> bool:
    """Skip dependency trees and OS junk. Those are not part of the bundle."""
    rel_parts = path.relative_to(src).parts
    if path.name in {".DS_Store", "Thumbs.db"}:
        return True
    return any(
        part == "node_modules" or part.startswith("node_modules.")
        for part in rel_parts[:-1]
    )


def _iter_source_files(src: Path) -> list[Path]:
    if not src.is_dir():
        return []
    files = [
        path
        for path in src.rglob("*")
        if path.is_file() and not _source_file_ignored(path, src)
    ]
    files.sort(key=lambda item: item.relative_to(src).as_posix())
    return files


def _frontend_source_hash(src: Path) -> str:
    """Hash frontend source so a checkout can be fresh without comparing mtimes.

    Text files are newline-normalized. ``* text=auto`` checks ``.txt`` out as
    CRLF on Windows, while ``eol=lf`` sources stay LF; either form must match
    the stamp written on the build machine. Dependency directories are omitted
    so a local ``node_modules`` does not look like a source change.
    """
    digest = hashlib.sha256()
    for path in _iter_source_files(src):
        data = path.read_bytes()
        if path.suffix.lower() in _TEXT_SOURCE_SUFFIXES:
            data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(path.relative_to(src).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return "v1 " + digest.hexdigest()


def _recorded_source_stamp(dist: Path) -> str | None:
    path = dist / _FRONTEND_SOURCE_STAMP
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _write_frontend_source_stamp(src: Path, dist: Path) -> None:
    """Record the source hash next to ``index.html`` after a successful build."""
    payload = (_frontend_source_hash(src) + "\n").encode("ascii")
    (dist / _FRONTEND_SOURCE_STAMP).write_bytes(payload)


def _frontend_needs_rebuild(src: Path | None = None, dist: Path | None = None) -> bool:
    """Return whether ``ampuly ui`` must run Vite before serving the console.

    A git checkout stamps every file with "now", and ``frontend_src`` is
    written after ``frontend_dist``, so source mtimes land newer than the
    bundle. On the owner's Windows machine that false stale result tries to
    spawn Vite/esbuild and dies with Access Denied. When ``index.html`` is
    present and ``source-stamp.txt`` matches the source hash, the committed
    bundle is fresh and must be served as-is. A missing stamp keeps the old
    mtime comparison for dist trees built before the stamp existed.
    """
    src = _FRONTEND_SRC if src is None else src
    dist = _FRONTEND_DIST if dist is None else dist
    if not (dist / "index.html").is_file():
        return True
    recorded = _recorded_source_stamp(dist)
    if recorded is None:
        latest_src = _latest_mtime(src, exclude_dirs={"node_modules"})
        latest_dist = _latest_mtime(dist)
        return latest_src > latest_dist
    try:
        current = _frontend_source_hash(src)
    except OSError:
        return True
    return recorded != current


def _sanitize_windows_ssl_env() -> None:
    """Avoid Windows OpenSSL crashes triggered by SSLKEYLOGFILE."""
    keylog_path = pop_windows_sslkeylogfile()
    if keylog_path:
        terminal_status(
            "SSL keylog environment removed",
            kind="warning",
            detail=format_windows_sslkeylog_warning("ampuly ui", keylog_path),
        )


def _pip_install(*packages: str) -> None:
    """Install packages using uv pip (if in uv env) or fallback to pip."""
    uv = shutil.which("uv")
    # Detect uv-managed venv: UV_VIRTUAL_ENV set or uv binary available with active venv
    in_uv = bool(uv) and ("UV_VIRTUAL_ENV" in os.environ or "uv" in (sys.prefix or ""))
    if in_uv:
        cmd = [uv, "pip", "install", *packages]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *packages]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL)


def _ensure_aiohttp() -> None:
    """Install aiohttp + aiosqlite if missing."""
    missing = []
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        missing.append("aiohttp>=3.9.0")
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        missing.append("aiosqlite>=0.19.0")
    if missing:
        terminal_status("Installing Python dependencies", detail=", ".join(missing))
        _pip_install(*missing)


def _run_npm(npm: str, *args: str) -> None:
    """Run npm in a way that also works when Windows resolves npm.cmd."""
    cmd = [npm, *args]
    if sys.platform == "win32":
        npm_path = Path(npm)
        npm_cmd = npm_path.with_suffix(".cmd")
        if npm_path.suffix.lower() in {".cmd", ".bat"}:
            cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, *args]
        elif npm_cmd.exists():
            cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(npm_cmd), *args]
    subprocess.check_call(cmd, cwd=_FRONTEND_SRC)


def _ensure_frontend() -> None:
    """Build frontend if frontend_dist is missing or does not match source."""
    if not _frontend_needs_rebuild():
        return

    if not _FRONTEND_SRC.is_dir():
        terminal_error("Frontend source not found.", detail=str(_FRONTEND_SRC))
        raise SystemExit(1)

    npm = shutil.which("npm")
    if not npm:
        terminal_error("npm not found.", detail="Install Node.js first: https://nodejs.org/")
        raise SystemExit(1)

    if _FRONTEND_DIST.is_dir() and any(_FRONTEND_DIST.iterdir()):
        terminal_status("Frontend source changed", detail="rebuilding Office UI bundle")
    else:
        terminal_status("Building Office UI frontend", detail="first run")

    # npm install
    node_modules = _FRONTEND_SRC / "node_modules"
    if not node_modules.is_dir():
        terminal_status("Installing npm dependencies")
        _run_npm(npm, "install")

    # npm run build → outputs to ../frontend_dist via vite config.
    # emptyOutDir deletes source-stamp.txt; write it only after index.html exists.
    terminal_status("Building frontend bundle")
    _run_npm(npm, "run", "build")

    if _FRONTEND_DIST.is_dir() and (_FRONTEND_DIST / "index.html").is_file():
        _write_frontend_source_stamp(_FRONTEND_SRC, _FRONTEND_DIST)
        terminal_status("Frontend built successfully", kind="success")
    else:
        terminal_error("Build completed but frontend_dist has no index.html.")
        raise SystemExit(1)


def register_cli(parent_app: typer.Typer) -> None:
    """Register the `ampuly ui` command on the parent Typer app."""
    import typer as _typer

    @parent_app.command()
    def ui(
        port: int = _typer.Option(8765, "--port", "-p", help="Server port"),
        host: str = _typer.Option(
            "127.0.0.1",
            "--host",
            help="Bind address; 0.0.0.0 exposes the console to the network and needs an owner account first",
        ),
        project: Optional[str] = _typer.Option(None, "--project", help="Project ID"),
        rebuild: bool = _typer.Option(False, "--rebuild", help="Force rebuild frontend"),
        complete_reply: bool = _typer.Option(False, "--complete-reply", help="Receive complete model replies for this process; do not change saved configuration"),
    ) -> None:
        """Launch the Office UI — visual frontend for AMPULY agents."""
        _sanitize_windows_ssl_env()
        # Auto-install aiohttp if needed
        _ensure_aiohttp()

        # Auto-build frontend if needed (or forced)
        if rebuild and _FRONTEND_DIST.is_dir():
            shutil.rmtree(_FRONTEND_DIST)
        _ensure_frontend()

        # Reuse the CLI bootstrap so project-controlled config is trusted
        # before the web server constructs and initializes its engine.
        from ampuly.cli.app import _get_config

        config = _get_config()
        if complete_reply:
            config = config.model_copy(deep=True)
            config.system.native_runtime.stream_llm = False

        from ampuly.plugins.office_ui.server import run_server

        run_server(host=host, port=port, config=config, project_id=project)
