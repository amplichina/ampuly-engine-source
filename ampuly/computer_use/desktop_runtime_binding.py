"""Project/task-scoped composition of the existing Windows desktop Helper.

Construction and tool registration do not start a process or read a screen.
Only a current explicitly enabled channel and a projected desktop task may
reach the existing fixed-vocabulary worker. No model-supplied process command.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
from threading import Lock

from ampuly.computer_use import desktop_bridge
from ampuly.computer_use.desktop_helper import HelperCapabilityBroker, SecureInputBroker
from ampuly.computer_use.desktop_indicator import DesktopOperationIndicator, IndicatorUnavailable
from ampuly.computer_use.desktop_runtime_lifecycle import DesktopLifecycleRefused, DesktopRuntimeLifecycle
from ampuly.layer4_tools.registry import ToolDefinition

_DESKTOP_OWNER = Lock()
MAX_BINDINGS = 64


class DesktopBindingRefused(PermissionError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _text(value, maximum=128):
    return type(value) is str and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


class DesktopRuntimeBinding:
    def __init__(self, engine, read_switch, *, factory=None, windows=None):
        project = getattr(engine, 'project_id', None)
        if not _text(project) or not callable(read_switch):
            raise DesktopBindingRefused('desktop_binding_unavailable')
        self.engine, self.project_id, self.read_switch = engine, project, read_switch
        self.windows = os.name == 'nt' if windows is None else windows is True
        self._factory = factory
        from ampuly.computer_use.desktop_ui_frames import DesktopUiFrames
        self.ui_frames = DesktopUiFrames(self)
        self._lifecycle = DesktopRuntimeLifecycle(project, max_contexts=MAX_BINDINGS,
            on_owner_closing=self.ui_frames.revoke_attempt)
        self._contexts = self._lifecycle.contexts
        self.indicator = DesktopOperationIndicator(self)
        from ampuly.computer_use.desktop_frame_binding import DesktopFramesBinding
        self.frames = DesktopFramesBinding(self)

    def enabled(self):
        from ampuly.plugins.office_ui.desktop_connector import CHANNEL_SWITCH
        if self._lifecycle.admission_closed or getattr(self.engine, 'project_id', None) != self.project_id:
            return False
        try:
            value = self.read_switch(CHANNEL_SWITCH)
            return type(value) is dict and value.get('explicit') is True and value.get('enabled') is True
        except Exception:
            return False

    def helper_available(self):
        return (not self._lifecycle.admission_closed and self.windows
                and getattr(self.engine, 'project_id', None) == self.project_id)

    def _scope(self, task):
        from ampuly.plugins.office_ui.desktop_connector import _step, clean_app, never_app
        if getattr(task, 'project_id', None) != self.project_id:
            raise DesktopBindingRefused('desktop_project_mismatch')
        metadata = getattr(task, 'metadata', None)
        if type(metadata) is not dict:
            raise DesktopBindingRefused('desktop_task_scope_invalid')
        step = _step(task)
        if step is None:
            raise DesktopBindingRefused('desktop_not_in_this_step')
        summary, node_id = step
        order_id, task_id = metadata.get('order_id'), getattr(task, 'id', None)
        apps = summary.get('allowed_apps')
        direct = node_id == 'direct'
        if ((not direct and not _text(order_id)) or not _text(task_id) or not _text(node_id)
                or type(apps) is not list or not 1 <= len(apps) <= 8
                or any(type(app) is not str or clean_app(app) != app or never_app(app) for app in apps)
                or len({desktop_bridge.app_key(app) for app in apps}) != len(apps)):
            raise DesktopBindingRefused('desktop_task_scope_invalid')
        raw = json.dumps([self.project_id, order_id, task_id, node_id, summary],
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if len(raw.encode('utf-8')) > 32768:
            raise DesktopBindingRefused('desktop_task_scope_invalid')
        return hashlib.sha256(raw.encode('utf-8')).hexdigest(), tuple(apps)

    @contextmanager
    def scope(self, task):
        # A single real desktop cannot be driven by concurrent project turns.
        # Refuse contention; do not queue an old coordinate for later use.
        if not _DESKTOP_OWNER.acquire(blocking=False):
            raise DesktopBindingRefused('desktop_busy')
        try:
            if not self.enabled():
                raise DesktopBindingRefused('desktop_channel_closed')
            if not self.helper_available():
                raise DesktopBindingRefused('desktop_helper_unavailable')
            key, apps = self._scope(task)
            from ampuly.plugins.office_ui.desktop_connector import _step
            step = _step(task)
            node_id = step[1] if step else ""

            def create_context():
                broker, texts = HelperCapabilityBroker(), SecureInputBroker()
                holder = {}

                def before_input():
                    return self._os_input_allowed(holder.get("helper"))

                if self._factory is None:
                    from ampuly.computer_use.windows_desktop_process import WindowsDesktopHelperProcess
                    helper = WindowsDesktopHelperProcess(broker, texts, allowed_apps=apps, before_input=before_input)
                else:
                    helper = self._factory(broker, texts, apps)
                holder["helper"] = helper
                if any(not callable(getattr(helper, name, None)) for name in ('observe', 'execute', 'kill')):
                    raise DesktopBindingRefused('desktop_helper_unavailable')
                return helper, broker, texts

            # The bridge checks the current switch again at every operation.
            with self._lifecycle.borrow(task, key, create_context) as context:
                self._borrowed_task = task
                self._borrowed_node = node_id
                try:
                    with desktop_bridge.helper_scope(*context, channel_check=self.enabled):
                        yield context[0]
                finally:
                    self._borrowed_task = None
                    self._borrowed_node = None
        finally:
            _DESKTOP_OWNER.release()

    def _note_foreign_foreground(self, helper) -> None:
        window_id = getattr(helper, "window_id", "") if helper is not None else ""
        task, node = getattr(self, "_borrowed_task", None), getattr(self, "_borrowed_node", None)
        if not window_id or task is None or type(node) is not str or not node:
            return
        from ampuly.computer_use.windows_desktop_native import foreign_foreground
        if foreign_foreground(window_id) is True:
            desktop_bridge.note_foreign_foreground(task, node)

    def _os_input_allowed(self, helper) -> bool:
        """Last check before a real helper is allowed to reach the OS."""
        self._note_foreign_foreground(helper)
        if not self.enabled() or not self._lifecycle.input_allowed():
            return False
        task, node = getattr(self, "_borrowed_task", None), getattr(self, "_borrowed_node", None)
        if self._factory is None and (task is None or not self.indicator.input_allowed(task)):
            return False
        if task is None or not desktop_bridge.agent_may_input(task, node if type(node) is str else None):
            return False
        if self.frames.input_allowed(task) is not True:
            return False
        return True

    def tools(self):
        from ampuly.plugins.office_ui.desktop_connector import create_desktop_tools, OPEN_EFFECTS, GATED_EFFECTS
        manager = self
        class ScopedDesktopTool(ToolDefinition):
            @property
            def permission_effects(self):
                # Evaluated by the ORIGINAL permission adapter before func is
                # called; do not inherit the process-global closed gate here.
                return OPEN_EFFECTS[self.name] if manager.enabled() and manager.helper_available() else GATED_EFFECTS
            @permission_effects.setter
            def permission_effects(self, value): pass
        result = []
        def wrap(function):
            async def invoke(*args, task=None, **kwargs):
                if not manager.enabled():
                    from ampuly.plugins.office_ui.desktop_connector import stop_request
                    return stop_request(desktop_bridge.STOP_CHANNEL_CLOSED)
                if not manager.helper_available():
                    from ampuly.plugins.office_ui.desktop_connector import stop_request
                    return stop_request(desktop_bridge.STOP_NO_HELPER)
                try:
                    with manager.scope(task):
                        if manager._factory is None:
                            manager.indicator.before_operation(task, function.__name__)
                        result = await function(*args, task=task, **kwargs)
                        if manager._factory is None:
                            manager.indicator.after_operation(task)
                        return result
                except (DesktopBindingRefused, DesktopLifecycleRefused) as exc:
                    return {'ok': False, 'refused': exc.code,
                            'note': '桌面运行范围无法核实或当前被占用，请负责人核对后继续。'}
                except IndicatorUnavailable as exc:
                    return {'ok': False, 'refused': str(exc),
                            'note': '桌面操作提示不可用或你已停止操作；不会在隐藏提示的情况下继续输入。'}
            return invoke
        for original in create_desktop_tools():
            values = {name: getattr(original, name) for name in ('name', 'description', 'parameters',
                'category', 'requires_confirmation', 'concurrency_safe', 'read_only', 'runtime_managed',
                'max_result_chars', 'persist_large_results', 'self_bounded_output', 'preview_chars',
                'company_effect_kind', 'permission_effects', 'return_schema')}
            values['func'] = wrap(original.func)
            result.append(ScopedDesktopTool(**values))
        return result


def bind_desktop_runtime(engine, read_switch):
    """Attach per-engine tools without binding or launching a global helper."""
    previous = vars(engine).get('_ampuly_desktop_runtime_binding')
    if previous is None:
        previous = DesktopRuntimeBinding(engine, read_switch)
        engine._ampuly_desktop_runtime_binding = previous
    elif type(previous) is not DesktopRuntimeBinding or previous.engine is not engine:
        raise DesktopBindingRefused('desktop_binding_conflict')
    else:
        previous.read_switch = read_switch
    for tool in previous.tools():
        engine.tool_registry.register(tool)
    desktop_bridge.bind_project_gate(previous.project_id, previous)
    return previous


@asynccontextmanager
async def desktop_execution_scope(engine, task, attempt_token):
    """Bind the exact Engine-owned attempt; construction alone starts nothing."""
    binding = vars(engine).get('_ampuly_desktop_runtime_binding')
    if binding is None:
        yield
        return
    if type(binding) is not DesktopRuntimeBinding or binding.engine is not engine:
        raise DesktopBindingRefused('desktop_binding_conflict')
    try:
        async with binding._lifecycle.owner(task, attempt_token):
            yield
    finally:
        # Keep the notice until original in-flight operations drain and their
        # Helper is killed. An incomplete cleanup retains its exact notice.
        if not binding._lifecycle.has_attempt(attempt_token):
            binding.indicator.revoke_attempt(attempt_token)


def begin_desktop_shutdown(engine):
    """Close admission immediately; return the exact object to drain later."""
    binding = vars(engine).get('_ampuly_desktop_runtime_binding')
    if binding is None:
        return None
    if type(binding) is not DesktopRuntimeBinding or binding.engine is not engine:
        raise DesktopBindingRefused('desktop_binding_conflict')
    binding._lifecycle.begin_shutdown()
    binding.ui_frames.close_all()
    return binding


async def finish_desktop_shutdown(binding):
    if binding is None:
        return
    await binding._lifecycle.close()
    binding.indicator.close()
    desktop_bridge.unbind_project_gate(binding.project_id, binding)
