"""Bounded transport codec, never an executor or an authorization decision.

Grammar from openai/codex 8e6a44b428e31f91b21edc97904fcdf4f0931ade,
codex-rs/core/assets/tools/apply_patch.lark (Apache-2.0).
See ampuly/layer3_agent/licenses/codex/{LICENSE,NOTICE}.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

from ampuly.llm.responses_bridge import ProtocolBridgeRefused

# Kept verbatim; Python Lark does not allow upstream's zero-width terminals.
PATCH_GRAMMAR = '''start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF

%import common.LF
'''
ENV_PATCH_GRAMMAR = PATCH_GRAMMAR.replace(
    'start: begin_patch hunk+ end_patch',
    'start: begin_patch environment_id? hunk+ end_patch\nenvironment_id: "*** Environment ID: " filename LF',
)


@lru_cache(maxsize=2)
def _parser(grammar):
    if grammar not in (PATCH_GRAMMAR, ENV_PATCH_GRAMMAR):
        raise ProtocolBridgeRefused("unreviewed custom grammar")
    try:
        installed = version("lark")
    except PackageNotFoundError:
        raise ProtocolBridgeRefused("custom tool parser is not installed") from None
    if installed != "1.3.1":
        raise ProtocolBridgeRefused("custom grammar parser version changed")
    from lark import Lark
    return Lark(grammar.replace('/(.*)/', '/(.+)/?'), parser="lalr")


def validate_patch(value, grammar):
    if not isinstance(value, str) or len(value) > 64_000 or value.count('\n') > 2000:
        raise ProtocolBridgeRefused("custom input exceeds the supported bound")
    try:
        if len(value.encode('utf-8')) > 64_000:
            raise ProtocolBridgeRefused("custom input exceeds the supported bound")
        _parser(grammar).parse(value)
    except (UnicodeError, ValueError):
        raise ProtocolBridgeRefused("invalid custom patch input") from None
    except ImportError:
        raise ProtocolBridgeRefused("custom tool parser is not installed") from None
    except Exception as exc:
        from lark.exceptions import LarkError
        if isinstance(exc, LarkError):
            raise ProtocolBridgeRefused("custom input does not match the reviewed grammar") from None
        raise
    return value


def prepare_request(request):
    """Preserve raw input inside a strict JSON envelope for Chat functions."""
    try:
        if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) > 512_000:
            raise ValueError
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolBridgeRefused("bounded finite JSON request required") from None
    if not isinstance(request, dict):
        raise ProtocolBridgeRefused("request must be an object")
    result = copy.deepcopy(request)
    tools = result.get('tools', [])
    if not isinstance(tools, list) or len(tools) > 64:
        raise ProtocolBridgeRefused("bounded tool list required")
    grammars = {}
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
            raise ProtocolBridgeRefused("invalid tool definition")
        name = tool['name']
        if name in names:
            raise ProtocolBridgeRefused("duplicate tool name")
        names.add(name)
        if tool.get('type') != 'custom':
            continue
        fmt = tool.get('format')
        if (name != 'apply_patch' or set(tool) - {'type', 'name', 'description', 'format'}
                or not isinstance(fmt, dict) or set(fmt) != {'type', 'syntax', 'definition'}
                or fmt.get('type') != 'grammar' or fmt.get('syntax') != 'lark'
                or not isinstance(fmt.get('definition'), str)):
            raise ProtocolBridgeRefused("unsupported custom tool contract")
        grammar = fmt['definition'].replace('\r\n', '\n').strip() + '\n'
        if grammar not in (PATCH_GRAMMAR, ENV_PATCH_GRAMMAR):
            raise ProtocolBridgeRefused("unreviewed custom grammar")
        _parser(grammar)  # Fail before model dispatch, not after paid generation.
        description = tool.get('description', '')
        if not isinstance(description, str):
            raise ProtocolBridgeRefused("custom description must be text")
        grammars[name] = grammar
        tool.clear()
        tool.update(type='function', name=name, description=description +
                    '\nTransport contract: return JSON {"input": "raw patch text"}. '
                    'The input must conform exactly to this grammar:\n' + grammar,
                    parameters={'type': 'object', 'properties': {'input': {'type': 'string'}},
                                'required': ['input'], 'additionalProperties': False}, strict=True)
    choice = result.get('tool_choice')
    if isinstance(choice, dict) and choice.get('type') == 'custom':
        if (set(choice) != {'type', 'name'} or not isinstance(choice.get('name'), str)
                or choice['name'] not in grammars):
            raise ProtocolBridgeRefused("custom choice must name an offered custom tool")
        choice['type'] = 'function'
    elif isinstance(choice, dict) and isinstance(choice.get('name'), str) and choice['name'] in grammars:
        raise ProtocolBridgeRefused("custom tool requires a custom choice")
    items = result.get('input')
    if isinstance(items, list):
        if len(items) > 256:
            raise ProtocolBridgeRefused("bounded history required")
        custom_ids = set()
        for item in items:
            if not isinstance(item, dict):
                raise ProtocolBridgeRefused("history item must be an object")
            kind = item.get('type')
            if kind == 'custom_tool_call':
                name = item.get('name')
                cid = item.get('call_id')
                if (not isinstance(name, str) or name not in grammars or not isinstance(cid, str) or not cid
                        or cid in custom_ids or item.get('namespace') is not None
                        or item.get('caller') is not None):
                    raise ProtocolBridgeRefused("unsupported custom history call")
                value = validate_patch(item.get('input'), grammars[name])
                item.clear()
                item.update(type='function_call', name=name, call_id=cid,
                            arguments=json.dumps({'input': value}, ensure_ascii=False))
                custom_ids.add(cid)
            elif kind == 'custom_tool_call_output':
                cid = item.get('call_id')
                if not isinstance(cid, str) or cid not in custom_ids:
                    raise ProtocolBridgeRefused("custom output must match a custom call")
                item['type'] = 'function_call_output'
                custom_ids.remove(cid)
            elif ((kind == 'function_call' and isinstance(item.get('name'), str) and item['name'] in grammars)
                  or (kind == 'function_call_output' and isinstance(item.get('call_id'), str) and item['call_id'] in custom_ids)):
                raise ProtocolBridgeRefused("function/custom history type mismatch")
    return result, grammars


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def restore_response(response, grammars):
    from openai.types.responses import ResponseCustomToolCall
    for index, item in enumerate(response.get('output', [])):
        if item.get('type') != 'function_call' or item.get('name') not in grammars:
            continue
        raw = item.get('arguments')
        if not isinstance(raw, str) or len(raw) > 512_000:
            raise ProtocolBridgeRefused("custom transport envelope exceeds its bound")
        try:
            envelope = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise ProtocolBridgeRefused("invalid custom transport envelope") from None
        if not isinstance(envelope, dict) or set(envelope) != {'input'}:
            raise ProtocolBridgeRefused("custom envelope must contain only input")
        value = validate_patch(envelope['input'], grammars[item['name']])
        response['output'][index] = ResponseCustomToolCall(
            id=item['id'], call_id=item['call_id'], name=item['name'],
            input=value, type='custom_tool_call',
        ).model_dump(exclude_none=True)
    return response
