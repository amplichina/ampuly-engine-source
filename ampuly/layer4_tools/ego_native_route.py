"""Server-selected Ego routing for existing browser tool declarations.

This leaf neither creates an Ego space nor authorizes an action. The selector
and dispatcher must come from the server composition root; a model argument is
never a backend selector. Unsupported bound calls fail closed. The original
browser callable remains available only when the selector explicitly returns
None for the trusted runtime task.
"""
from __future__ import annotations

import copy
import functools
import inspect
from typing import Any, Awaitable, Callable, Protocol, Sequence

from ampuly.layer4_tools.registry import ToolDefinition


EGO_NATIVE_TOOL_NAMES = frozenset({
    "browser_navigate", "browser_observe", "browser_type",
    "browser_select_option", "browser_scroll", "browser_click",
})


class EgoNativeRouteRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class EgoNativeDispatcher(Protocol):
    def __call__(
        self, tool_name: str, arguments: dict[str, Any], *, task: Any, binding: Any,
        on_progress: Any = None,
    ) -> Awaitable[Any]: ...


BindingSelector = Callable[[Any], Any | None]


def _route_callable(tool: ToolDefinition, select_binding: BindingSelector,
                    dispatch: EgoNativeDispatcher):
    original = tool.func
    tool_name = tool.name
    signature = inspect.signature(original)
    if "task" not in signature.parameters:
        raise ValueError("browser_route_requires_explicit_runtime_task")

    @functools.wraps(original)
    async def routed(*args: Any, **kwargs: Any) -> Any:
        # Match the original signature before selecting a backend. Registry
        # injection has already replaced model-supplied reserved arguments.
        call = signature.bind(*args, **kwargs)
        call.apply_defaults()
        task = call.arguments.get("task")
        try:
            binding = select_binding(task)
        except Exception:
            raise EgoNativeRouteRefused("ego_host_selection_failed") from None
        if inspect.isawaitable(binding):
            if inspect.iscoroutine(binding):
                binding.close()
            raise EgoNativeRouteRefused("ego_host_selector_must_be_synchronous")
        if binding is None:
            return await original(*args, **kwargs)
        if tool_name not in EGO_NATIVE_TOOL_NAMES:
            raise EgoNativeRouteRefused("ego_tool_unsupported")
        arguments = dict(call.arguments)
        arguments.pop("task", None)
        on_progress = arguments.pop("on_progress", None)
        # Deliberately no catch/fallback: failures, cancellation and unknown
        # outcomes belong to this selected host and its original Effect ledger.
        result = dispatch(tool_name, arguments, task=task, binding=binding,
                          on_progress=on_progress)
        if not inspect.isawaitable(result):
            raise EgoNativeRouteRefused("ego_host_dispatch_must_be_async")
        return await result

    # ToolRegistry uses inspect.signature to inject trusted task/on_progress
    # and reject unknown model arguments. Preserve it explicitly, including
    # parameter kinds, defaults, annotations and the return annotation.
    routed.__signature__ = signature
    return routed


def route_ego_browser_tools(
    tools: Sequence[ToolDefinition], *, select_binding: BindingSelector,
    dispatch: EgoNativeDispatcher,
) -> list[ToolDefinition]:
    """Detach browser declarations without changing capability metadata.

    Every browser tool is wrapped, including unsupported operations, so an Ego
    task cannot escape to Playwright by choosing another browser tool name.
    Other tool categories and the input declarations remain untouched.
    """
    if not callable(select_binding) or not callable(dispatch):
        raise TypeError("server_binding_selector_and_dispatcher_required")
    result: list[ToolDefinition] = []
    for tool in tools:
        if not isinstance(tool, ToolDefinition):
            raise TypeError("tool_definition_required")
        if tool.category != "browser":
            result.append(tool)
            continue
        detached = copy.copy(tool)
        detached.func = _route_callable(tool, select_binding, dispatch)
        result.append(detached)
    return result
