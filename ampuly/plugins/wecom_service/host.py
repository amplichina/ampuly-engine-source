"""What the 企业微信客服 extension needs from the console, declared once (``ConsoleHost``).

Everything the extension asks of the console goes through this protocol; the console implements it with its own
adapter (``ampuly.plugins.office_ui.extension_host.HandlerConsoleHost``), the one place that reads the websocket
handler's members. Nothing in this package reaches into the handler itself.

An entry point of this package that takes "the console" takes a ``ConsoleHost``, or the console's handler (or a test's
stand-in for it), which ``as_console_host`` puts behind the console's adapter.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol


class ConsoleHost(Protocol):
    """The console, as this extension uses it."""

    # True on every host: how ``as_console_host`` tells a host from a handler.
    is_console_host: bool

    @property
    def business(self) -> Any:
        """The business store."""
        ...

    def packages(self) -> list[Any]:
        """The workflow packages the console knows."""
        ...

    def console_api(self) -> Any:
        """The console API: ``create_order``, ``record_observation``, what ``acceptance_round`` takes."""
        ...

    async def request_engine(self, project_id: str) -> tuple[Any, str]:
        """(engine, project id) for a request naming ``project_id``."""
        ...

    async def project_engine(self, project_id: str) -> Any:
        """The engine of ``project_id`` (its model and its accounting store)."""
        ...

    def normalize_project_id(self, value: Any) -> str: ...

    def gate_configured(self) -> bool:
        """Whether the Feishu gate group is set up."""
        ...

    def gate_service(self) -> Any:
        """The Feishu gate service (``feishu_gate.GateService``)."""
        ...

    def ensure_feishu_listener(self) -> None:
        """Start the group's listener if the console has one and it does not run."""
        ...

    def frozen_project_check(self) -> Callable[[list[str]], Any] | None:
        """Which of these projects their owner archived or destroyed, or None when the console cannot tell."""
        ...

    def outbound_refusal(self) -> Any:
        """The console's outbound check for a notifier (``notifier_from_env(outbound_allowed=...)``), or None."""
        ...

    def note_duty(self, entry: dict[str, Any]) -> None:
        """One entry in the duty log the project page reads."""
        ...

    @property
    def shutting_down(self) -> bool: ...

    def extension_state(self, name: str) -> dict[str, Any]:
        """This extension's per-console place for what it holds while the console runs."""
        ...

    def is_owner(self, ws: Any) -> bool: ...

    def actor_login(self, ws: Any) -> str: ...

    async def send_ack(self, ws: Any, **fields: Any) -> None: ...


def as_console_host(console: Any) -> ConsoleHost:
    """``console`` as a ``ConsoleHost``: itself when it is one, else the console's adapter around it
    (``extension_host.console_host``)."""
    from ampuly.plugins.office_ui.extension_host import console_host

    return console_host(console)


def state_of(console: Any) -> dict[str, Any]:
    """This extension's per-console state (``ConsoleHost.extension_state``)."""
    from ampuly.plugins.wecom_service import NAME

    return as_console_host(console).extension_state(NAME)
