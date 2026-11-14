"""External agent adapter registry — manages available external agents."""

from __future__ import annotations

from loguru import logger

from ampuly.core.config import AgentsConfig
from ampuly.core.execution_agents import EXTERNAL_EXECUTION_AGENTS
from ampuly.layer3_agent.adapters.base import ExternalAgentAdapter
from ampuly.layer3_agent.adapters.claude_code import ClaudeCodeAdapter
from ampuly.layer3_agent.adapters.cursor_adapter import CursorAdapter
from ampuly.layer3_agent.adapters.codex_adapter import CodexAdapter
from ampuly.layer3_agent.adapters.opencode_adapter import OpenCodeAdapter
from ampuly.layer3_agent.adapters.jiuwen_adapter import JiuwenAdapter, JiuwenSwarmAdapter


ADAPTER_CLASSES: dict[str, type[ExternalAgentAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "cursor": CursorAdapter,
    "codex": CodexAdapter,
    "opencode": OpenCodeAdapter,
    "jiuwen": JiuwenAdapter,
    "jiuwenswarm": JiuwenSwarmAdapter,
}

if frozenset(ADAPTER_CLASSES) != EXTERNAL_EXECUTION_AGENTS:
    missing = sorted(EXTERNAL_EXECUTION_AGENTS.difference(ADAPTER_CLASSES))
    unexpected = sorted(set(ADAPTER_CLASSES).difference(EXTERNAL_EXECUTION_AGENTS))
    raise RuntimeError(
        "External adapter registry does not match the canonical execution-agent "
        f"catalog (missing={missing}, unexpected={unexpected})"
    )


class AdapterRegistry:
    """Manages external agent adapters and preferred order."""

    def __init__(self, config: AgentsConfig) -> None:
        self.config = config
        self._adapters: dict[str, ExternalAgentAdapter] = {}
        self._available: dict[str, bool] = {}

    async def initialize(self) -> None:
        """Discover and initialize available external agents."""
        self._adapters = {}
        self._available = {}
        for agent_type, adapter_cls in ADAPTER_CLASSES.items():
            agent_config = self.config.agents.get(agent_type)
            adapter = adapter_cls(config=agent_config)
            self._adapters[agent_type] = adapter
            if agent_config and not agent_config.enabled:
                self._available[agent_type] = False
                logger.info(f"External agent {agent_type}: disabled in config")
                continue

            available = await adapter.is_available()
            self._available[agent_type] = available
            status = "available" if available else "not found"
            logger.info(f"External agent {agent_type}: {status}")

    def get(self, agent_type: str) -> ExternalAgentAdapter | None:
        if agent_type in self._adapters and self._available.get(agent_type):
            return self._adapters[agent_type]
        return None

    def get_preferred(self) -> ExternalAgentAdapter | None:
        """Get the first available adapter from the preferred order."""
        for agent_type in self.config.preferred_order:
            adapter = self.get(agent_type)
            if adapter:
                return adapter
        return None

    def get_ordered_available(self) -> list[tuple[str, ExternalAgentAdapter]]:
        ordered: list[tuple[str, ExternalAgentAdapter]] = []
        seen: set[str] = set()

        for agent_type in self.config.preferred_order:
            adapter = self.get(agent_type)
            if adapter:
                ordered.append((agent_type, adapter))
                seen.add(agent_type)

        for agent_type, adapter in self._adapters.items():
            if agent_type not in seen and self._available.get(agent_type):
                ordered.append((agent_type, adapter))

        return ordered

    def list_available(self) -> list[str]:
        return [k for k, v in self._available.items() if v]

    def list_all(self) -> dict[str, bool]:
        return dict(self._available)

    def describe_all(self) -> list[dict[str, object]]:
        from ampuly.layer3_agent.provider_conformance import evaluate_provider

        profiles: list[dict[str, object]] = []
        for agent_type, adapter in self._adapters.items():
            profile = adapter.describe()
            profile["available"] = self._available.get(agent_type, False)
            # How it is driven and how its tools are reached are two separate
            # facts, and this is the Provider's own statement about both --
            # a declaration, never evidence that either works.
            try:
                profile["transport"] = adapter.transport_profile()
            except Exception:
                profile["transport"] = {"control_transport": "unknown", "tool_transport": "unknown", "declared": False}
            # Conformance is a read-only, fail-closed posture signal. It is
            # deliberately attached to the existing provider profile so the
            # UI, CLI and future Harness selector cannot mistake availability
            # for production eligibility.
            try:
                profile["conformance"] = evaluate_provider(adapter).to_payload()
            except Exception as exc:
                profile["conformance"] = {
                    "provider": agent_type,
                    "production_ready": False,
                    "blockers": ["conformance_evaluation_failed"],
                    "checks": [],
                    "error": type(exc).__name__,
                }
            profiles.append(profile)
        return profiles

    def describe_available(self) -> list[dict[str, object]]:
        return [p for p in self.describe_all() if p.get("available")]
