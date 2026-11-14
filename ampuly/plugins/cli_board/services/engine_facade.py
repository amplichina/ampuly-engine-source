"""Engine lifecycle management for the CLI board."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from ampuly.core.config import AmpulyConfig, get_ampuly_home
from ampuly.engine import AmpulyEngine

ProgressCallback = Callable[[str], Awaitable[None] | Awaitable[Any]]
EventCallback = Callable[[Any], Awaitable[None]]


class EngineFacade:
    """Owns a single lazily-initialized AmpulyEngine for the CLI board process."""

    def __init__(self, project_id: str | None = None) -> None:
        self.project_id = project_id
        self._config: AmpulyConfig | None = None
        self._engine: AmpulyEngine | None = None
        self._progress_callback: Callable[..., Awaitable[None]] | None = None
        self._event_callback: EventCallback | None = None
        self._init_lock = asyncio.Lock()

    @property
    def engine(self) -> AmpulyEngine | None:
        return self._engine

    @property
    def store(self):  # noqa: ANN201 - preserve simple property for callers
        return self._engine.store if self._engine else None

    @property
    def ampuly_home(self) -> Path:
        return get_ampuly_home()

    def configure_callbacks(
        self,
        *,
        progress_callback: Callable[..., Awaitable[None]] | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self._progress_callback = progress_callback
        self._event_callback = event_callback
        if self._engine is not None and progress_callback is not None:
            self._engine.on_progress = progress_callback

    async def ensure_ready(self) -> AmpulyEngine:
        if self._engine is not None:
            return self._engine

        async with self._init_lock:
            if self._engine is not None:
                return self._engine

            from ampuly.plugins.office_ui.order_liveness import BusinessOrderLiveness

            config_dir = get_ampuly_home() / "config"
            self._config = AmpulyConfig.load(config_dir) if config_dir.exists() else AmpulyConfig()
            engine = AmpulyEngine(
                config=self._config,
                project_id=self.project_id,
                on_progress=self._progress_callback,
                # runtime contract: the console's orders are the kernel's order port here too, so a
                # board opened on the same home withholds the cards of an order that has ended, as the console does.
                order_liveness=BusinessOrderLiveness(get_ampuly_home()),
            )
            await engine.initialize()
            if self._event_callback is not None:
                engine.event_bus.subscribe_all(self._event_callback)
            self._engine = engine
            return engine

    async def shutdown(self) -> None:
        if self._engine is None:
            return
        engine = self._engine
        self._engine = None
        await engine.shutdown()

