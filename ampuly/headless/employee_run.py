"""Headless ``talent run`` bootstrap.

Builds an AmpulyEngine, attaches an explicitly supplied employee-host factory, and
calls the direct-task runner. It does not open the console's ui_state database
or the office service factory.
"""
from __future__ import annotations

from typing import Any, Callable


def resolve_headless_project_id(project_id: str | None) -> str:
    """The project the host is built for. An omitted flag is ``default``.

    ``BuiltinReadTools`` treats a missing project id as an unverified tool
    implementation and raises ``BudgetStopSurfaced``. The console normalizes
    that omission to ``default`` before any employee host exists, and the
    command then returns a normal JSON refusal.
    """
    text = project_id.strip() if isinstance(project_id, str) else ""
    return text or "default"


async def run_headless_employee_task(
    *,
    config: Any,
    project_id: str | None,
    instance_id: str,
    instruction: str,
    actor: str,
    host_factory: Callable[[Any], Any],
    direct_runner: Callable[..., Any],
    ampuly_home: Any = None,
    source_handoff_id: str = "",
    rework_of_handoff_id: str = "",
) -> dict[str, Any]:
    """Initialize a core engine and run one employee task without the UI adapter.

    ``host_factory`` and ``direct_runner`` are injected by the CLI (or a test).
    This module does not import ``ampuly.plugins.office_ui``.
    """
    from ampuly.engine import AmpulyEngine

    if not callable(host_factory) or not callable(direct_runner):
        raise TypeError("headless employee run requires a host factory and a direct runner")
    # Resolve before initialize and before the host factory. The factory reads
    # engine.project_id while it builds the read-tool adapter.
    resolved_project = resolve_headless_project_id(project_id)
    engine = AmpulyEngine(
        config=config,
        project_id=resolved_project,
        ampuly_home=ampuly_home,
    )
    try:
        await engine.initialize()
        if getattr(engine, "employee_runtime_host", None) is None:
            engine.set_employee_runtime_host_factory(host_factory)
            engine.attach_employee_runtime_host(host_factory(engine))
        task_project = str(getattr(engine, "project_id", None) or resolved_project).strip() or resolved_project
        result = await direct_runner(
            engine=engine,
            project_id=task_project,
            instance_id=instance_id,
            instruction=instruction,
            actor=actor,
            source_handoff_id=source_handoff_id,
            rework_of_handoff_id=rework_of_handoff_id,
        )
    finally:
        shutdown = getattr(engine, "shutdown", None)
        if callable(shutdown):
            await shutdown()
    if type(result) is not dict:
        raise TypeError("employee direct runner must return a dict")
    return result
