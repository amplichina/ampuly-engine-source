"""Factory for shared Office services."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Awaitable, Callable

import aiosqlite

from ampuly.core.config import AmpulyConfig, get_ampuly_home
from ampuly.engine import AmpulyEngine
from ampuly.plugins.office_ui.agent_store import AgentStore
from ampuly.plugins.office_ui.chat_store import ChatStore
from ampuly.plugins.office_ui.event_adapter import EventAdapter

from . import OfficeServices
from .context import ModeState, OfficeServiceContext


class OfficeServiceFactory:
    """Async context manager that owns engine and UI-state persistence."""

    def __init__(
        self,
        *,
        config: AmpulyConfig | None = None,
        project_id: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_runtime_event: Callable[[Any], Awaitable[None]] | None = None,
        on_escalation: Callable[..., Awaitable[str | None]] | None = None,
    ) -> None:
        self.config = config
        self.project_id = project_id
        self.on_progress = on_progress
        self.on_runtime_event = on_runtime_event
        self.on_escalation = on_escalation
        self.db: aiosqlite.Connection | None = None
        self.engine: AmpulyEngine | None = None
        self.services: OfficeServices | None = None

    async def __aenter__(self) -> OfficeServices:
        if self.config is None:
            config_dir = get_ampuly_home() / "config"
            self.config = AmpulyConfig.load(config_dir) if config_dir.exists() else AmpulyConfig()
        from ampuly.plugins.office_ui.order_liveness import BusinessOrderLiveness

        self.engine = AmpulyEngine(
            config=self.config,
            project_id=self.project_id,
            on_progress=self.on_progress,
            on_runtime_event=self.on_runtime_event,
            on_escalation=self.on_escalation,
            # runtime contract: the console's orders are the kernel's order port.
            order_liveness=BusinessOrderLiveness(get_ampuly_home()),
        )
        self.db = await aiosqlite.connect(str(self.engine.ampuly_home / "ui_state.db"))
        # Wait for a concurrent writer (e.g. a running office-UI server)
        # instead of failing after sqlite's 5s default with 'database is locked'.
        await self.db.execute("PRAGMA busy_timeout=30000")
        agent_store = AgentStore(self.db)
        await agent_store.initialize()
        chat_store = ChatStore(self.db)
        await chat_store.initialize()
        event_adapter = EventAdapter()
        await self.engine.initialize()
        mode_state = ModeState(
            exec_mode=await agent_store.get_server_state("exec_mode", "task"),
            company_profile=await agent_store.get_server_state("company_profile", "corporate"),
            task_preferred_agent=await agent_store.get_server_state("task_preferred_agent", "native"),
        )
        context = OfficeServiceContext(
            engine=self.engine,
            agent_store=agent_store,
            chat_store=chat_store,
            event_adapter=event_adapter,
            mode_state=mode_state,
        )
        self.services = OfficeServices(context)
        return self.services

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.engine is not None:
            await self.engine.shutdown()
        if self.db is not None:
            await self.db.close()
