"""SuperOnes-owned launch policy around the pinned, otherwise unmodified SDK.

The start() implementation is adapted from openai-codex 0.147.0 client.py.
Copyright 2025 OpenAI. Licensed under Apache-2.0; see licenses/codex/LICENSE
and licenses/codex/NOTICE. Modified by SuperOnes, 2026-09-12: exact environment,
bundled executable, version gate and hidden Windows launch. The SDK retains
its transport, router, notification threads, approvals, and shutdown logic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
import subprocess
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SDK_VERSION = "0.147.0"
MODEL_KEY = "SUPERONES_MODEL_API_KEY"


class ManagedRuntimeRefused(ValueError):
    """A product prerequisite failed before any runtime is launched."""


def _version_gate() -> None:
    for name in ("openai-codex", "openai-codex-cli-bin"):
        if version(name) != SDK_VERSION:
            raise ManagedRuntimeRefused("SDK and bundled runtime must match the product's pinned version")


# Written by the product into a home it created empty, before the first
# launch. It is what tells a later launch that the `config.toml` it finds
# there is the bundled binary's own bookkeeping and not somebody's CODEX_HOME.
PRODUCT_HOME_MARKER = ".superones-product-home"
_PRODUCT_HOME_MARKER_TEXT = "superones managed product home v1\n"
_CONFIG_BOOKKEEPING_LIMIT = 65536


def _binary_bookkeeping_only(config_file: Path) -> bool:
    """Whether `config.toml` holds only what the bundled binary writes by itself.

    0.147.0 writes `[projects.<git root>] trust_level = "trusted"` into
    CODEX_HOME on its first run (V2, real binary, 2026-09-12). That is the
    whole allowed shape: any other key -- a model provider, an MCP server, a
    feature -- would be read by the next launch and is refused unread.
    """
    try:
        if config_file.stat().st_size > _CONFIG_BOOKKEEPING_LIMIT:
            return False
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if set(data) - {"projects"}:
        return False
    projects = data.get("projects", {})
    return isinstance(projects, dict) and all(
        isinstance(entry, dict) and set(entry) <= {"trust_level"} and isinstance(entry.get("trust_level", ""), str)
        for entry in projects.values()
    )


def validate_product_home(home: Path) -> None:
    """Reject linked ancestors without traversing them, and any home that is not ours.

    A login file is refused outright. A `config.toml` is refused *without
    being read* unless the product's own marker says this home was created
    empty by the product -- then it is read, and accepted only when it is
    exactly the binary's trust bookkeeping. Before V2 the second launch on
    the same product home was refused, because the file the binary had just
    written looked like a personal configuration.
    """
    if not home.is_absolute():
        raise ManagedRuntimeRefused("product runtime home must be absolute")
    for entry in (*reversed(home.parents), home):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ManagedRuntimeRefused("product runtime home must not traverse a filesystem link")
    login = home / "auth.json"
    if login.exists() or login.is_symlink():
        raise ManagedRuntimeRefused("runtime home contains a login; select a clean product home")
    config_file = home / "config.toml"
    if config_file.exists() or config_file.is_symlink():
        marker = home / PRODUCT_HOME_MARKER
        if config_file.is_symlink() or not marker.is_file() or not _binary_bookkeeping_only(config_file):
            raise ManagedRuntimeRefused("runtime home contains unmanaged configuration; select a clean product home")


def claim_product_home(home: Path) -> None:
    """Mark a validated home as product-created, so the binary's own files are accepted later."""
    home.mkdir(parents=True, exist_ok=True)
    marker = home / PRODUCT_HOME_MARKER
    if not marker.exists():
        marker.write_text(_PRODUCT_HOME_MARKER_TEXT, encoding="utf-8")


def model_route(config: Any, env: dict[str, str]) -> tuple[tuple[str, ...], dict[str, str]]:
    """Compile server-owned configuration; never discover a personal login.

    Only the explicitly named API secret is read. Values go into the process
    environment, never config files, argv, receipts, or the route description.
    Responses is the current kernel protocol; a Chat endpoint needs a tested
    translation route, not a different label on the same endpoint.
    """
    if getattr(config, "model_wire_api", "") != "responses":
        raise ManagedRuntimeRefused("configure an explicit Responses route; Chat Completions requires the protocol bridge")
    model = str(getattr(config, "model", "") or "").strip()
    endpoint = str(getattr(config, "model_api_base", "") or "").strip()
    key_ref = str(getattr(config, "model_api_key_env", "") or "").strip()
    if not model or len(model) > 256 or any(ord(c) < 32 for c in model):
        raise ManagedRuntimeRefused("an explicit bounded model id is required")
    try:
        url = urlsplit(endpoint)
        valid_url = (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}))
        valid_url = valid_url and bool(url.hostname) and not (url.username or url.password or url.query or url.fragment)
        _ = url.port
    except ValueError:
        valid_url = False
    if not valid_url or any(ord(c) < 33 for c in endpoint):
        raise ManagedRuntimeRefused("model endpoint must be HTTPS or loopback HTTP, with no credentials or query")
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key_ref):
        raise ManagedRuntimeRefused("an explicit API credential environment reference is required")
    key = env.get(key_ref) or os.environ.get(key_ref)
    if not key or len(key) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise ManagedRuntimeRefused("the configured model API credential is unavailable")
    selected_env = dict(env)
    selected_env.pop(key_ref, None)
    selected_env.pop("OPENAI_API_KEY", None)
    selected_env[MODEL_KEY] = key
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    overrides = (
        f"model={quote(model)}", 'model_provider="superones"',
        # The 0.147.0 binary treats an unknown model slug as fallback metadata
        # that *supports* reasoning summaries and defaults the summary to
        # "auto", so without this it asks every request for a summary this
        # route cannot produce. Saying so in configuration is the honest form:
        # the request then carries no summary, and the bridge keeps refusing
        # one rather than dropping it.
        'model_reasoning_summary="none"',
        'model_providers.superones.name="SuperOnes model route"',
        f"model_providers.superones.base_url={quote(endpoint)}",
        'model_providers.superones.wire_api="responses"',
        f"model_providers.superones.env_key={quote(MODEL_KEY)}",
        "model_providers.superones.requires_openai_auth=false",
        "model_providers.superones.request_max_retries=0",
        "model_providers.superones.stream_max_retries=0",
    )
    return overrides, selected_env


# What the 0.147.0 binary would otherwise hand the model on its own (features
# shell_tool / view_image / multi_agent default true; web_search off): a shell
# over the workspace, a namespace tool the bridge cannot carry, and the plan /
# ask-the-user built-ins. A console attempt's authority is the business tools
# it was given, so that profile turns every built-in off by name; a workspace
# run keeps the shell, with every approval still routed to the owner. The
# keys were confirmed against the bundled binary itself on 2026-09-12
# (`scripts/managed_master_probe.py`): with all eight set, the request the
# binary sends carries exactly the dynamic business tools and nothing else.
# `tools.update_plan` / `tools.experimental_request_user_input` are structs
# with an `enabled` field, not booleans; a bare `=false` fails config load.
TOOL_POLICIES: dict[str, tuple[str, ...]] = {
    "business_tools_only": (
        "features.shell_tool=false", "features.unified_exec=false",
        "features.view_image=false", 'web_search="disabled"',
        "features.multi_agent=false", "features.goals=false",
        "tools.update_plan.enabled=false", "tools.experimental_request_user_input.enabled=false",
    ),
    "workspace_shell": ('web_search="disabled"',),
}


def tool_policy_overrides(tool_policy: str) -> tuple[str, ...]:
    """Named, or refused: an attempt never launches with an unnamed tool surface."""
    try:
        return TOOL_POLICIES[str(tool_policy or "")]
    except KeyError:
        raise ManagedRuntimeRefused("managed runtime requires a named tool policy") from None


def runtime_config(config: Any, *, cwd: str, env: dict[str, str], tool_policy: str) -> Any:
    """Construct SDK configuration, without starting a process or writing files."""
    from openai_codex import CodexConfig

    overrides, selected_env = model_route(config, env)
    overrides = overrides + tool_policy_overrides(tool_policy)
    return CodexConfig(
        cwd=cwd, env=selected_env, config_overrides=overrides,
        client_name="superones_engine", client_title="SuperOnes Engine",
    )


def routing_identity(config: Any) -> str:
    """Internal cache identity including secret rotation; never log this payload."""
    payload = {"overrides": config.config_overrides, "env": config.env}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def managed_client(config: Any) -> Any:
    """Reuse official facades with one product-owned process-launch seam."""
    from codex_cli_bin import bundled_codex_path
    from openai_codex import AsyncCodex
    from openai_codex.client import CodexClient, _installed_codex_path_dirs, _prepend_path_dirs

    _version_gate()
    if config is None or not config.env or not config.cwd:
        raise ManagedRuntimeRefused("a product workspace and exact runtime environment are required")
    if config.launch_args_override is not None or config.codex_bin is not None:
        raise ManagedRuntimeRefused("managed runtime uses the locked bundled executable, not a personal CLI")
    home = Path(config.env.get("CODEX_HOME", ""))
    if not home.is_absolute() or not Path(config.cwd).is_absolute():
        raise ManagedRuntimeRefused("product runtime home and workspace must be absolute")
    # Do not load or delete old mirrored files. Stop before interpreting them.
    validate_product_home(home)
    claim_product_home(home)
    if not config.env.get(MODEL_KEY) or 'model_provider="superones"' not in config.config_overrides:
        raise ManagedRuntimeRefused("managed runtime requires an explicit product model route")

    class ProductCodexClient(CodexClient):
        def start(self) -> None:
            if self._proc is not None:
                return
            args = [str(bundled_codex_path())]
            for kv in self.config.config_overrides:
                args.extend(["--config", kv])
            args.extend(["app-server", "--listen", "stdio://"])
            # Unlike the upstream method: do NOT merge os.environ back in.
            child_env = dict(self.config.env)
            _prepend_path_dirs(child_env, _installed_codex_path_dirs())
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=self.config.cwd, env=child_env,
                bufsize=1, **options,
            )
            try:
                self._start_stderr_drain_thread()
                self._start_reader_thread()
            except BaseException:
                self.close()
                raise

    client = AsyncCodex(config=config)
    transport = getattr(client, "_client", None)
    if transport is None or not isinstance(getattr(transport, "_sync", None), CodexClient):
        raise ManagedRuntimeRefused("SDK launch seam changed; product conformance must be rerun")
    # Neither facade has started yet; no process is leaked or replaced.
    transport._sync = ProductCodexClient(config=config)
    return client
