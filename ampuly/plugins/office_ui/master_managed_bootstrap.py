"""The production assembly for the managed master: implemented, not enabled.

P1 of the revision-3 plan. C10 gave the WS master turn a seam that reads a
server-assembled factory; C11 made admission demand a composed identity
computed by the server. Neither had an implementation: nothing in the
product could say *what* the composed runtime is, so nothing could ever be
admitted. This module is that implementation.

Two things, and a third that joins them:

- ``composed_identity`` -- the one recipe for "which runtime is this":
  the pinned SDK and bundled binary (by package metadata and file digest,
  never by running the binary), the modules that make up the gateway, the
  bridge and the attempt, the bridge's fixed protocol policy, the tool set
  the console exposes, and the bootstrap's own choices (product home, per-
  turn limits, selected model). No credential, no environment value.
- ``build_factory`` -- the factory ``run_managed_master_turn`` calls: it
  returns one ``ManagedSdkAttempt`` for the console task, adding only a
  missing budget id, with a launch environment reduced to the allow-list.
- ``install`` -- registers both on the engine, and only after the admission
  decision for that identity is already "admitted". No report, a stale
  report, or a report taken under another identity leaves the engine as it
  was: the default path stays Native.

Nothing here is called by product startup. Enabling it is a deployment
decision recorded elsewhere, and needs live behaviour evidence for this
exact identity first (a V item: it means running the real runtime).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass
from importlib.metadata import version as dist_version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ampuly.layer3_agent.adapters.base import ExternalAgentAdapter
from ampuly.layer3_agent.conformance_suite import EvidenceIdentity
from ampuly.layer3_agent.managed_codex import MODEL_KEY, SDK_VERSION, TOOL_POLICIES, model_route, validate_product_home
from ampuly.layer3_agent.managed_sdk_attempt import ManagedSdkAttempt
from ampuly.layer3_agent.provider_admission import decide_from_home, file_digest
from ampuly.plugins.office_ui.master_managed_admission import SUBJECT


class BootstrapRefused(RuntimeError):
    """A prerequisite of the composed runtime failed; nothing was installed."""


@dataclass(frozen=True)
class ManagedMasterBootstrap:
    """What a deployment decides, and nothing the conversation can change."""

    product_home: Path
    turn_limits: dict[str, float]
    actor: str = "server"

    def validate(self) -> None:
        if not isinstance(self.product_home, Path) or not self.product_home.is_absolute():
            raise BootstrapRefused("bootstrap_product_home_must_be_absolute")
        limits = dict(self.turn_limits or {})
        calls = limits.get("model_call")
        if set(limits) != {"model_call"} or type(calls) is not int or not 1 <= calls <= 100:
            raise BootstrapRefused("bootstrap_turn_limits_must_bound_model_calls")
        if not str(self.actor or "").strip():
            raise BootstrapRefused("bootstrap_actor_required")


# ── identity ───────────────────────────────────────────────────────────────

_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def _cached_file_digest(path: Path) -> str:
    """The binary is large and the identity is computed twice per turn."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    digest = _DIGEST_CACHE.get(key)
    if digest is None:
        digest = file_digest(path)
        _DIGEST_CACHE[key] = digest
    return digest


def _module_file(module_name: str) -> Path:
    import importlib

    module = importlib.import_module(module_name)
    return Path(str(getattr(module, "__file__", "") or ""))


# The modules whose code *is* the composed runtime. A change to any of them
# is a different runtime, and the evidence taken before the change is about
# something else.
_COMPOSITION_MODULES = (
    "ampuly.layer3_agent.managed_codex",
    "ampuly.layer3_agent.managed_sdk_attempt",
    "ampuly.layer3_agent.harness_codex_sdk",
    "ampuly.llm.responses_bridge",
    "ampuly.llm.responses_stream",
    "ampuly.llm.custom_tool_codec",
    "ampuly.llm.managed_model_route",
    "ampuly.llm.model_route_http",
    "ampuly.plugins.office_ui.master_sdk_tools",
    "ampuly.plugins.office_ui.master_managed_entry",
    "ampuly.plugins.office_ui.master_managed_admission",
    "openai_codex.client",
    "litellm.responses.litellm_completion_transformation.transformation",
)


def _selected_model(engine: Any) -> str:
    selector = getattr(getattr(engine, "llm", None), "_select_model", None)
    model = selector(None) if callable(selector) else ""
    if not isinstance(model, str) or not model.strip():
        raise BootstrapRefused("bootstrap_model_route_unavailable")
    return model


def implementation_digest() -> str:
    """sha256 over the bundled binary and every composition module."""
    from codex_cli_bin import bundled_codex_path

    parts: list[str] = [f"codex_bin:{_cached_file_digest(Path(str(bundled_codex_path())))}"]
    for name in _COMPOSITION_MODULES:
        parts.append(f"{name}:{_cached_file_digest(_module_file(name))}")
    if any(part.endswith(":") for part in parts):
        raise BootstrapRefused("bootstrap_composition_file_missing")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def policy_payload(engine: Any, bootstrap: ManagedMasterBootstrap) -> dict[str, Any]:
    """The configuration half of the identity. Secrets and environment values never enter."""
    from ampuly.llm.responses_bridge import (CACHE_HINT_FORWARDED, CACHE_HINT_NOT_FORWARDED,
                                          CLIENT_METADATA_KEYS, VACUOUS_INCLUDE)
    from ampuly.plugins.office_ui.master_tools import MASTER_TOOLS

    model = _selected_model(engine)
    # The route overrides the child receives, with the per-run gateway URL
    # replaced by a placeholder: the URL is allocated at attempt time.
    overrides, _ = model_route(
        SimpleNamespace(model=model, model_wire_api="responses",
                        model_api_base="http://127.0.0.1:1/v1", model_api_key_env=MODEL_KEY),
        env={MODEL_KEY: "placeholder-not-a-credential"},
    )
    return {
        "version": 1,
        "subject": SUBJECT,
        "model": model,
        "product_home": str(bootstrap.product_home),
        "turn_limits": dict(bootstrap.turn_limits),
        "route_overrides": [line for line in overrides if "base_url" not in line],
        "tool_policy": {"name": "business_tools_only", "overrides": list(TOOL_POLICIES["business_tools_only"])},
        "bridge": {
            "client_metadata_keys": sorted(CLIENT_METADATA_KEYS),
            "vacuous_include": list(VACUOUS_INCLUDE),
            "cache_hint_policies": [CACHE_HINT_FORWARDED, CACHE_HINT_NOT_FORWARDED],
        },
        "master_tools": [{"name": t.name, "writes": bool(t.writes), "effects": list(t.effects)} for t in MASTER_TOOLS],
        "human_review": getattr(engine, "interaction_coordinator", None) is not None,
    }


def composed_identity(engine: Any, bootstrap: ManagedMasterBootstrap) -> EvidenceIdentity:
    """One recipe, used by install and by the admission resolver on every turn."""
    bootstrap.validate()
    installed = {name: dist_version(name) for name in ("openai-codex", "openai-codex-cli-bin", "litellm")}
    if installed["openai-codex"] != SDK_VERSION or installed["openai-codex-cli-bin"] != SDK_VERSION:
        raise BootstrapRefused("bootstrap_sdk_version_drift")
    payload = policy_payload(engine, bootstrap)
    return EvidenceIdentity(
        provider=SUBJECT,
        provider_version=f"openai-codex {installed['openai-codex']}; cli-bin {installed['openai-codex-cli-bin']}; litellm {installed['litellm']}",
        implementation_digest=implementation_digest(),
        platform=f"{platform.system()} {platform.machine()} python{sys.version_info.major}.{sys.version_info.minor}",
        configuration_digest=hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


# ── factory ────────────────────────────────────────────────────────────────

def launch_environment(product_home: Path, source: dict[str, str] | None = None) -> dict[str, str]:
    """Launch essentials only, one spelling per name, plus the product's CODEX_HOME."""
    env: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in (source if source is not None else os.environ).items():
        upper = str(key).upper()
        if upper in ExternalAgentAdapter._INHERITED_LAUNCH_ENV and upper not in seen and isinstance(value, str):
            env[key] = value
            seen.add(upper)
    env["CODEX_HOME"] = str(product_home)
    return env


def build_factory(engine: Any, business: Any, bootstrap: ManagedMasterBootstrap):
    """The callable ``run_managed_master_turn`` invokes for one console turn."""
    bootstrap.validate()

    def factory(*, task: Any, scope: Any) -> ManagedSdkAttempt:
        validate_product_home(bootstrap.product_home)
        from ampuly.llm.responses_bridge import ResponsesChatBridge

        owned = copy.deepcopy(task)
        meta = owned.metadata
        if not meta.get("budget_id"):
            budget = business.create_run_budget(
                project_id=scope.project_id, customer_id=scope.customer_id,
                environment_id=scope.environment_id, order_id=str(meta.get("order_id", "") or ""),
                limits=dict(bootstrap.turn_limits), actor=bootstrap.actor,
            )
            meta["budget_id"] = budget["budget_id"]
        return ManagedSdkAttempt(
            registry=getattr(engine, "harness_registry", None), business=business, task=owned, scope=scope,
            bridge=ResponsesChatBridge(engine.llm, model=_selected_model(engine)),
            env=launch_environment(bootstrap.product_home),
            interactions=getattr(engine, "interaction_coordinator", None),
        )

    return factory


# ── install ────────────────────────────────────────────────────────────────

def install(engine: Any, business: Any, bootstrap: ManagedMasterBootstrap) -> EvidenceIdentity:
    """Register the resolver and factory, only when this identity is already admitted."""
    identity = composed_identity(engine, bootstrap)
    home = Path(getattr(engine, "ampuly_home", "")).resolve()
    if home != Path(business.path).parent.resolve():
        raise BootstrapRefused("bootstrap_evidence_root_mismatch")
    decision = decide_from_home(home, provider=SUBJECT, current_identity=identity)
    if not decision.admitted:
        raise BootstrapRefused("bootstrap_not_admitted:" + ",".join(decision.reasons))
    engine._managed_master_identity = lambda: composed_identity(engine, bootstrap)
    engine._managed_master_attempt_factory = build_factory(engine, business, bootstrap)
    return identity


def uninstall(engine: Any) -> None:
    for name in ("_managed_master_identity", "_managed_master_attempt_factory"):
        if name in getattr(engine, "__dict__", {}):
            delattr(engine, name)


def describe(engine: Any, bootstrap: ManagedMasterBootstrap) -> dict[str, Any]:
    """What an operator may see: the identity and the decision, never a secret."""
    identity = composed_identity(engine, bootstrap)
    home = Path(getattr(engine, "ampuly_home", "")).resolve()
    decision = decide_from_home(home, provider=SUBJECT, current_identity=identity)
    return {
        "identity": {
            "provider": identity.provider, "provider_version": identity.provider_version,
            "platform": identity.platform, "implementation_digest": identity.implementation_digest,
            "configuration_digest": identity.configuration_digest,
        },
        "admitted": decision.admitted, "reasons": list(decision.reasons),
        "installed": callable(getattr(engine, "_managed_master_attempt_factory", None)),
    }
