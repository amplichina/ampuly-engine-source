from __future__ import annotations

import os
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory

from loguru import logger

from ampuly.core.config import AmpulyConfig
from ampuly.plugins.office_ui.server import _is_under_path, create_app


def test_run_server_reads_engine_from_created_app_before_remote_guard() -> None:
    source = Path(__file__).resolve().parents[1].joinpath("server.py").read_text(encoding="utf-8")
    assert 'engine = app["engine"]' in source
    assert "_bootstrap_needed(engine)" in source


class CleanHomeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_app_bootstraps_a_missing_ampuly_home(self) -> None:
        # Use the platform temp root rather than a repository-local path.  The
        # latter is not guaranteed to be writable in packaged/CI checkouts.
        with TemporaryDirectory(prefix="server-startup-") as raw:
            home = Path(raw) / ".ampuly"
            previous = os.environ.get("AMPULY_HOME")
            previous_workplace = os.environ.get("AMPULY_WORKPLACE_ROOT")
            os.environ["AMPULY_HOME"] = str(home)
            # Keep the test hermetic on packaged/CI checkouts whose repository
            # parent is intentionally not writable.
            os.environ["AMPULY_WORKPLACE_ROOT"] = str(Path(raw) / "workplace")
            app = None
            try:
                app = await create_app(config=AmpulyConfig(), project_id="startup-smoke")
                self.assertTrue((home / "ui_state.db").is_file())
                self.assertEqual(app["engine"].project_id, "startup-smoke")
            finally:
                if app is not None:
                    ws_handler = app.get("ws_handler")
                    if ws_handler is not None:
                        await ws_handler.shutdown()
                        await ws_handler.flush_all_progress()
                    engine = app.get("engine")
                    if engine is not None:
                        await engine.shutdown()
                    db = app.get("db")
                    if db is not None:
                        await db.close()
                    lock = app.get("instance_lock")
                    if lock is not None:
                        lock.close()
                    # Engine startup installs a loguru file sink under the
                    # isolated home. Close it before TemporaryDirectory
                    # removes the tree on Windows.
                    logger.remove()
                if previous is None:
                    os.environ.pop("AMPULY_HOME", None)
                else:
                    os.environ["AMPULY_HOME"] = previous
                if previous_workplace is None:
                    os.environ.pop("AMPULY_WORKPLACE_ROOT", None)
                else:
                    os.environ["AMPULY_WORKPLACE_ROOT"] = previous_workplace


def test_is_under_path_accepts_platform_child_paths() -> None:
    cases = [
        (
            PureWindowsPath(r"C:\work\Ampuly\ampuly\plugins\office_ui\frontend_dist\assets\index.js"),
            PureWindowsPath(r"C:\work\Ampuly\ampuly\plugins\office_ui\frontend_dist\assets"),
        ),
        (
            PurePosixPath("/work/Ampuly/ampuly/plugins/office_ui/frontend_dist/assets/index.js"),
            PurePosixPath("/work/Ampuly/ampuly/plugins/office_ui/frontend_dist/assets"),
        ),
    ]

    for child, base in cases:
        assert _is_under_path(child, base)


def test_is_under_path_rejects_sibling_prefixes() -> None:
    cases = [
        (
            PureWindowsPath(r"C:\work\Ampuly\ampuly\plugins\office_ui\frontend_dist\assets-old\index.js"),
            PureWindowsPath(r"C:\work\Ampuly\ampuly\plugins\office_ui\frontend_dist\assets"),
        ),
        (
            PurePosixPath("/work/Ampuly/ampuly/plugins/office_ui/frontend_dist/assets-old/index.js"),
            PurePosixPath("/work/Ampuly/ampuly/plugins/office_ui/frontend_dist/assets"),
        ),
    ]

    for child, base in cases:
        assert not _is_under_path(child, base)


def test_is_under_path_rejects_traversal_after_resolution() -> None:
    cases = [
        (
            PureWindowsPath(r"C:\work\Ampuly\ampuly\plugins\office_ui\frontend_dist\index.html"),
            PureWindowsPath(r"C:\work\Ampuly\ampuly\plugins\office_ui\frontend_dist\assets"),
        ),
        (
            PurePosixPath("/work/Ampuly/ampuly/plugins/office_ui/frontend_dist/index.html"),
            PurePosixPath("/work/Ampuly/ampuly/plugins/office_ui/frontend_dist/assets"),
        ),
    ]

    for escaped, base in cases:
        assert not _is_under_path(escaped, base)
