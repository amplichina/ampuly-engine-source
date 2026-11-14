"""Single policy model for Ampuly Native tool permissions.

External-agent harnesses intentionally do not consume this policy.  The
resolver is scoped by the root UI/CLI session so every native company role and
native subagent observes the same live setting without copying mutable policy
state into each child task.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
from typing import Any, Literal, get_args

from ampuly.i18n_runtime import is_zh
from ampuly.core.models import (
    PermissionResolution,
    PermissionScope,
    RiskLevel,
    RuntimePermissionDecision,
)


NativeApprovalLevel = Literal["read-only", "auto", "full-access"]
NativePermissionEffect = Literal[
    "local_read",
    "workspace_write",
    "process_execute",
    "network_access",
    "external_side_effect",
    "runtime_internal",
    "unknown",
]

NATIVE_APPROVAL_LEVELS = frozenset(get_args(NativeApprovalLevel))
NATIVE_PERMISSION_EFFECTS = frozenset(get_args(NativePermissionEffect))


def parse_native_approval_level(value: Any) -> NativeApprovalLevel:
    """Parse an operator-supplied level without silently changing intent."""

    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "readonly": "read-only",
        "read": "read-only",
        "workspace-write": "auto",
        "full": "full-access",
        "unrestricted": "full-access",
        "danger-full-access": "full-access",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in NATIVE_APPROVAL_LEVELS:
        raise ValueError(
            "native approval level must be read-only, auto, or full-access"
        )
    return normalized  # type: ignore[return-value]


def normalize_native_approval_level(
    value: Any,
    *,
    default: NativeApprovalLevel = "auto",
) -> NativeApprovalLevel:
    try:
        return parse_native_approval_level(value)
    except ValueError:
        return default


def clamp_native_approval_for_content_trust(
    level: Any,
    content_trust: Any,
) -> NativeApprovalLevel:
    """Keep untrusted inbound content from widening native execution rights.

    Content trust and tool approval remain separate controls: trusted console
    turns retain the configured level, while channel-bound external content is
    capped at read-only so writes, processes, network access, and external
    effects still require an explicit native permission decision.
    """

    normalized = normalize_native_approval_level(level)
    trust = str(content_trust or "").strip().lower()
    if trust == "untrusted_external":
        return "read-only"
    return normalized


def migrate_legacy_native_approval_level(values: Any) -> NativeApprovalLevel:
    """Map one legacy ``autonomy`` mapping to the canonical three levels."""

    if not isinstance(values, dict):
        return "auto"
    explicit = values.get("native_approval_level")
    if explicit not in (None, ""):
        return parse_native_approval_level(explicit)
    if values.get("enabled") is False:
        return "full-access"
    if values.get("allow_native_tool_auto_approval") is False:
        return "read-only"
    max_risk = str(values.get("max_auto_approve_risk", "") or "").strip().lower()
    threshold = values.get("approval_confidence_threshold")
    first_use = values.get("tool_first_use_approval")
    try:
        threshold_value = float(threshold) if threshold is not None else 1.0
    except (TypeError, ValueError):
        threshold_value = 1.0
    if max_risk in {"high", "critical"} and threshold_value <= 0 and first_use is False:
        return "full-access"
    return "auto"


# The product browser tools (ampuly/layer4_tools/browser.py create_browser_tools), each routed through the
# Computer Use Gateway. A delegation authorizes these and nothing else named like them (an MCP tool in the
# `browser_` namespace is not one of them); tests pin the set against the tool face.
PRODUCT_BROWSER_TOOLS = frozenset({
    "browser_navigate", "browser_snapshot", "browser_observe", "browser_click", "browser_type", "browser_submit_enter", "browser_take_screenshot", "browser_wait_for",
    "browser_scroll", "browser_select_option", "browser_navigate_back", "browser_close", "browser_switch_tab",
    "browser_vision_act", "browser_request_human", "browser_decide_act",
})


def browser_scope_delegated(tool: Any, task: Any) -> bool:
    """X05 / C01: whether this call is a browser tool of an order the owner delegated to named hosts.

    The delegation (`master_delegate_browser`, or an order dispatched for a
    website) is the owner naming the hosts this order may reach; the browser
    tools refuse any other host before the page moves, a submit is proposed
    for approval on its own (X06), and a person can take the page at any
    time. Asking the generic network question again would only make the
    owner repeat what the delegation said. Only a delegation recorded by the
    console counts -- a domain list alone is scope, not authorization.
    """
    name = str(getattr(tool, "name", "") or "")
    # Both the name and the category: a registry entry can be replaced under a product name, and a
    # product-named tool from anywhere else (category "mcp") never passed through the Gateway.
    if name not in PRODUCT_BROWSER_TOOLS or str(getattr(tool, "category", "") or "") != "browser":
        return False
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    domains = [str(d) for d in list(metadata.get("browser_allowed_domains") or []) if str(d or "").strip()]
    return bool(domains) and bool(str(metadata.get("browser_delegated_by", "") or "").strip())


def permission_effects_for_tool(tool: Any) -> frozenset[str]:
    """Return declared effects; undeclared dynamic tools remain unknown."""

    raw = getattr(tool, "permission_effects", None)
    if raw is None:
        return frozenset({"unknown"})
    effects = {
        str(item or "").strip().lower()
        for item in raw
        if str(item or "").strip()
    }
    return frozenset(effects or {"unknown"})


def native_sandbox_profile(level: Any) -> dict[str, Any]:
    normalized = normalize_native_approval_level(level)
    if normalized == "full-access":
        return {
            "enabled": False,
            "mode": "off",
            "wrapper": "none",
            "fail_if_unavailable": False,
            "allow_direct_fallback": True,
            "allow_network": True,
            "native_approval_level": normalized,
        }
    return {
        "enabled": True,
        "mode": "read-only" if normalized == "read-only" else "workspace-write",
        "wrapper": "auto",
        "fail_if_unavailable": True,
        "allow_direct_fallback": False,
        "allow_network": False,
        "native_approval_level": normalized,
    }


def tasks_in_native_permission_scope(
    tasks: list[Any],
    *,
    root_session_id: str,
    scope_id: str,
) -> list[Any]:
    """Resolve one root session and all of its persisted native descendants."""

    root_session = str(root_session_id or "").strip()
    normalized_scope = str(scope_id or root_session).strip()
    session_ids = {value for value in (root_session, normalized_scope) if value}
    selected_ids: set[str] = set()
    selected: list[Any] = []
    changed = True
    while changed:
        changed = False
        for task in tasks:
            task_id = str(getattr(task, "id", "") or "").strip()
            selection_key = task_id or f"object:{id(task)}"
            if selection_key in selected_ids:
                continue
            metadata = dict(getattr(task, "metadata", {}) or {})
            task_session = str(getattr(task, "session_id", "") or "").strip()
            parent_session = str(
                getattr(task, "parent_session_id", "") or ""
            ).strip()
            persisted_scope = str(
                metadata.get("native_permission_scope_id", "") or ""
            ).strip()
            declared_root = str(
                metadata.get("root_session_id", "")
                or metadata.get("company_runtime_root_session_id", "")
                or metadata.get("parent_session_id", "")
                or ""
            ).strip()
            if not (
                (normalized_scope and persisted_scope == normalized_scope)
                or (task_session and task_session in session_ids)
                or (parent_session and parent_session in session_ids)
                or (declared_root and declared_root in session_ids)
            ):
                continue
            selected.append(task)
            selected_ids.add(selection_key)
            if task_session and task_session not in session_ids:
                session_ids.add(task_session)
                changed = True
    return selected


@dataclass(frozen=True)
class NativePermissionContext:
    level: NativeApprovalLevel
    scope_id: str


class NativePermissionPolicyResolver:
    """Resolve default/session policy and make deterministic tool decisions."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._session_levels: dict[str, NativeApprovalLevel] = {}

    def set_config(self, config: Any) -> None:
        self._config = config

    @property
    def default_level(self) -> NativeApprovalLevel:
        return normalize_native_approval_level(
            getattr(self._config, "native_approval_level", "auto")
        )

    def set_session_level(self, scope_id: str, level: Any) -> NativeApprovalLevel:
        normalized = normalize_native_approval_level(level, default=self.default_level)
        key = str(scope_id or "").strip()
        if key:
            self._session_levels[key] = normalized
        return normalized

    def context_for_task(self, task: Any = None) -> NativePermissionContext:
        metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
        scope_id = str(
            metadata.get("native_permission_scope_id", "")
            or metadata.get("root_session_id", "")
            or metadata.get("company_runtime_root_session_id", "")
            or metadata.get("parent_session_id", "")
            or getattr(task, "session_id", "")
            or getattr(task, "id", "")
            or ""
        ).strip()
        persisted = metadata.get("native_approval_level")
        cached = self._session_levels.get(scope_id)
        if persisted not in (None, ""):
            level = normalize_native_approval_level(persisted, default=self.default_level)
            if cached is not None:
                # Either source can have been downgraded since the other was
                # loaded. A stale high value must never undo that restriction.
                # Explicit elevation updates both through the UI/CLI service.
                rank = {"read-only": 0, "auto": 1, "full-access": 2}
                level = min((level, cached), key=rank.__getitem__)
            elif scope_id:
                self._session_levels[scope_id] = level
        elif cached is not None:
            level = cached
        else:
            level = self.default_level
        return NativePermissionContext(level=level, scope_id=scope_id)

    def register_task(self, task: Any) -> NativePermissionContext:
        context = self.context_for_task(task)
        metadata = dict(getattr(task, "metadata", {}) or {})
        metadata["native_approval_level"] = context.level
        if context.scope_id:
            metadata["native_permission_scope_id"] = context.scope_id
            self._session_levels[context.scope_id] = context.level
        task.metadata = metadata
        return context

    def sandbox_for_task(self, task: Any = None) -> dict[str, Any]:
        return native_sandbox_profile(self.context_for_task(task).level)

    def evaluate(
        self,
        tool: Any,
        *,
        task: Any = None,
        outside_workspace: bool = False,
        safe_read_only_process: bool = False,
        network_hint: bool = False,
        outside_workspace_path: str = "",
    ) -> RuntimePermissionDecision:
        context = self.context_for_task(task)
        level = context.level
        effects = permission_effects_for_tool(tool)
        metadata = {
            "native_approval_level": level,
            "native_permission_scope_id": context.scope_id,
            "permission_effects": sorted(effects),
            "sandbox": native_sandbox_profile(level),
        }
        if level == "full-access":
            return RuntimePermissionDecision(
                PermissionResolution.ALLOW,
                PermissionScope.ONCE,
                RiskLevel.LOW,
                "Native full access permits registered tools without a tool approval prompt.",
                "native_permission_policy",
                metadata,
            )

        if "unknown" in effects:
            return self._ask(
                level,
                metadata,
                (
                    "该工具未声明它会产生哪些系统影响，无法判断风险。"
                    if is_zh()
                    else "Tool has no declared native permission effects."
                ),
                sandbox_override=native_sandbox_profile("full-access"),
            )
        if "process_execute" in effects and not self._sandbox_available():
            return self._ask(
                level,
                metadata,
                (
                    "本机无法建立进程围栏，该命令需要一次性人工批准。"
                    if is_zh()
                    else "The required native sandbox is unavailable; this process needs one-time approval."
                ),
                sandbox_override=native_sandbox_profile("full-access"),
            )
        if outside_workspace:
            return self._ask(
                level,
                metadata,
                (
                    "该工具要访问工作区之外的路径。"
                    if is_zh()
                    else "Tool accesses a path outside the current workspace."
                ),
                sandbox_override=self._grant_sandbox(
                    level,
                    effects,
                    outside_workspace_path=outside_workspace_path,
                ),
            )
        if level == "auto" and browser_scope_delegated(tool, task) and not outside_workspace:
            # The owner already said which hosts this order's browser may reach; a
            # read-only session still asks, because that restriction is the person's.
            return RuntimePermissionDecision(
                PermissionResolution.ALLOW,
                PermissionScope.ONCE,
                RiskLevel.LOW,
                (
                    "浏览器范围已由 owner 委派到指定站点；站点之外的访问由浏览器工具本身拒绝，提交另行审批。"
                    if is_zh()
                    else "The owner delegated this order's browser to named hosts; other hosts are refused by the browser tools and a submit is approved on its own."
                ),
                "native_permission_policy",
                {**metadata, "browser_delegated": True,
                 "sandbox": self._grant_sandbox(level, effects, network=True, outside_workspace_path=outside_workspace_path)},
            )
        if network_hint or "external_side_effect" in effects or "network_access" in effects:
            return self._ask(
                level,
                metadata,
                (
                    "该工具需要联网，或会产生对外的副作用。"
                    if is_zh()
                    else "Tool requires network access or an external side effect."
                ),
                sandbox_override=self._grant_sandbox(
                    level,
                    effects,
                    network=True,
                    outside_workspace_path=outside_workspace_path,
                ),
            )

        if level == "read-only":
            # Shell/Python definitions describe their maximum capability.  A
            # command that has been structurally proven read-only does not
            # exercise the tool's workspace-write capability, so evaluate
            # the effects of this call rather than the broad tool envelope.
            effective_effects = set(effects)
            if safe_read_only_process:
                effective_effects.discard("workspace_write")
            disallowed = effective_effects & {
                "workspace_write",
                "external_side_effect",
                "network_access",
            }
            if disallowed:
                return self._ask(
                    level,
                    metadata,
                    (
                    "当前为只读模式，产生副作用需要批准。"
                    if is_zh()
                    else "Read-only mode requires approval for side effects."
                ),
                    sandbox_override=self._grant_sandbox(
                        level,
                        effective_effects,
                        outside_workspace_path=outside_workspace_path,
                    ),
                )
            if "process_execute" in effects and not safe_read_only_process:
                return self._ask(
                    level,
                    metadata,
                    (
                    "当前为只读模式，执行该进程需要批准。"
                    if is_zh()
                    else "Read-only mode requires approval for this process execution."
                ),
                    sandbox_override=self._grant_sandbox(level, effects),
                )

        return RuntimePermissionDecision(
            PermissionResolution.ALLOW,
            PermissionScope.ONCE,
            RiskLevel.LOW,
            f"Native {level} policy allows this tool call.",
            "native_permission_policy",
            metadata,
        )

    @staticmethod
    def _sandbox_available() -> bool:
        # Answered by the execution layer, which is the one that has to apply
        # the confinement: a sandbox claimed here but not applied there would
        # waive the approval prompt and confine nothing. Imported lazily
        # because the execution layer reads this module's sandbox profiles.
        from ampuly.layer4_tools.execution_context import sandbox_wrapper_available

        return sandbox_wrapper_available()

    @staticmethod
    def _ask(
        level: NativeApprovalLevel,
        metadata: dict[str, Any],
        rationale: str,
        *,
        sandbox_override: dict[str, Any],
    ) -> RuntimePermissionDecision:
        elevated = dict(metadata)
        elevated["sandbox_override"] = dict(sandbox_override)
        elevated["sandbox_override"]["grant_scope"] = "once"
        return RuntimePermissionDecision(
            PermissionResolution.ASK,
            PermissionScope.ONCE,
            RiskLevel.MEDIUM,
            rationale,
            "native_permission_policy",
            elevated,
        )

    def approved_call_metadata(
        self,
        tool: Any,
        *,
        task: Any = None,
        outside_workspace_path: str = "",
        network_hint: bool = False,
    ) -> dict[str, Any]:
        """Capability envelope for a call covered by a prior human grant."""

        context = self.context_for_task(task)
        effects = permission_effects_for_tool(tool)
        metadata: dict[str, Any] = {
            "native_approval_level": context.level,
            "native_permission_scope_id": context.scope_id,
            "permission_effects": sorted(effects),
            "sandbox": native_sandbox_profile(context.level),
        }
        override = self._grant_sandbox(
            context.level,
            effects,
            network=network_hint,
            outside_workspace_path=outside_workspace_path,
        )
        if override != metadata["sandbox"]:
            override["grant_scope"] = "persisted"
            metadata["sandbox_override"] = override
        return metadata

    def _grant_sandbox(
        self,
        level: NativeApprovalLevel,
        effects: Any,
        *,
        network: bool = False,
        outside_workspace_path: str = "",
    ) -> dict[str, Any]:
        if level == "full-access":
            return native_sandbox_profile("full-access")
        effect_set = set(effects or ())
        if "unknown" in effect_set:
            return native_sandbox_profile("full-access")
        if "process_execute" in effect_set and not self._sandbox_available():
            return native_sandbox_profile("full-access")
        profile = native_sandbox_profile(level)
        if "workspace_write" in effect_set:
            profile["mode"] = "workspace-write"
        if network or "network_access" in effect_set or "external_side_effect" in effect_set:
            profile["allow_network"] = True
        raw_path = str(outside_workspace_path or "").strip()
        if raw_path:
            try:
                candidate = Path(raw_path).expanduser().resolve(strict=False)
                grant_path = candidate if candidate.is_dir() else candidate.parent
                profile["additional_write_paths"] = [str(grant_path)]
            except Exception:
                pass
        return profile
