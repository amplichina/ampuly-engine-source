"""Existing master business handlers, declared as app-server dynamic tools.

No new domain write path. Wire shape follows the pinned upstream app-server
DynamicToolSpec/CallResponse; the SDK keeps its transport and request router.
"""
from __future__ import annotations

import copy
import json
from typing import Any


class MasterSdkTools:
    def __init__(self, actions: Any, task: Any, *, include_writes: bool = True):
        from ampuly.plugins.office_ui.master_runtime import build_tool_registry
        from ampuly.plugins.office_ui.master_loop import loop_system_prompt
        self.task = copy.deepcopy(task)
        self.registry = build_tool_registry(actions, include_writes=include_writes)
        self.instructions = loop_system_prompt()
        self.names = frozenset(tool.name for tool in self.registry.list_tools())
        self.calls = 0
        self.blocked = False

    def specs(self) -> list[dict[str, Any]]:
        from openai_codex.generated.v2_all import DynamicToolSpec
        return [DynamicToolSpec.model_validate({
            'type': 'function', 'name': tool.name, 'description': tool.description,
            'inputSchema': copy.deepcopy(tool.parameters),
        }).model_dump(mode='json', by_alias=True, exclude_none=True) for tool in self.registry.list_tools()]

    @staticmethod
    def refused(code: str) -> dict[str, Any]:
        return {'success': False, 'contentItems': [{'type': 'inputText',
            'text': json.dumps({'error': code, 'automatic_retry': False})}]}

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.names or self.blocked or self.calls >= 64:
            return self.refused('business_tool_unavailable')
        self.calls += 1
        result = await self.registry.execute(name, arguments, task=self.task)
        if not result.get('success'):
            # An execution exception is not proof that an external write did
            # not happen. The durable handler owns reconciliation, not an SDK
            # retry. Keep transport exception messages out of the model input.
            self.blocked = True
            return self.refused('business_tool_failed_verify_before_retry')
        payload = result.get('result')
        refused = isinstance(payload, dict) and (
            payload.get('ok') is False or 'refused' in payload or 'error' in payload)
        text = json.dumps({'source': 'superones_business_tool', 'result': payload},
                          ensure_ascii=False, allow_nan=False)
        if len(text.encode('utf-8')) > 65536:
            self.blocked = True
            return self.refused('business_tool_output_too_large')
        return {'success': not refused, 'contentItems': [{'type': 'inputText', 'text': text}]}
