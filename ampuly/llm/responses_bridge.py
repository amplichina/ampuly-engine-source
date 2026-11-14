"""Stateless Responses/function adapter over the existing Chat model boundary.

Reuse: LiteLLM 1.82.1's message, tool-schema and response conversion. Deliberately
avoid its global tool-call cache and session recovery. This component neither
hosts an HTTP endpoint nor runs tools; its caller must authenticate/bind scope.
Supports complete text/function histories plus explicitly reviewed patch grammar
through a transport codec; other Responses features remain fail-closed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any


class ProtocolBridgeRefused(ValueError):
    """Unsupported or incomplete input, refused before requesting model work."""


# Pinned to the commit the bundled 0.147.0 binary is built from
# (tag rust-v0.147.0 -> be6e8eac029b183056b7e4402879f15d2c85f61b),
# core/src/responses_metadata.rs::client_metadata(), HTTP path: nine keys.
# The earlier reference commit 8e6a44b4 (1288 commits newer than the binary)
# adds 'root_turn_id'; the binary in the product never sends it, so it is
# not accepted. See docs/V42-DEFAULT-REQUEST-MATRIX-2026-09-12.zh.md.
CLIENT_METADATA_KEYS = frozenset({
    'x-codex-installation-id', 'session_id', 'thread_id', 'x-codex-window-id',
    'turn_id', 'x-openai-subagent', 'x-codex-parent-thread-id',
    'parent_turn_id', 'x-codex-turn-metadata',
})
# The one `include` value the binary always sends (client.rs, unconditional).
# It asks the server to return encrypted reasoning items so a stateless client
# can replay them. This bridge produces no reasoning items at all, so the
# request is satisfied vacuously: nothing is omitted from the response, and a
# history that carries a reasoning item is still refused below. Any other
# include value would change what the response must contain, and is refused.
VACUOUS_INCLUDE = ('reasoning.encrypted_content',)


def client_metadata_receipt(request: dict[str, Any]) -> dict[str, Any]:
    """Consume diagnostic metadata locally; never grant authority or echo values."""
    metadata = request.get('client_metadata')
    if metadata is None:
        return {}
    if (not isinstance(metadata, dict) or set(metadata) - CLIENT_METADATA_KEYS
            or any(not isinstance(value, str) or len(value) > 32768 for value in metadata.values())):
        raise ProtocolBridgeRefused('unsupported client diagnostic metadata')
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    except UnicodeError:
        raise ProtocolBridgeRefused('invalid client diagnostic encoding') from None
    if len(encoded) > 32768:
        raise ProtocolBridgeRefused('client diagnostic metadata exceeds bound')
    if not metadata:
        return {}
    return {'client_metadata': {'policy': 'local_digest_only', 'field_count': len(metadata),
                                'sha256': hashlib.sha256(encoded).hexdigest()}}


def _converter():
    if version("litellm") != "1.82.1":
        raise ProtocolBridgeRefused("protocol converter version changed; rerun conformance")
    from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig
    return LiteLLMCompletionResponsesConfig


def _function_name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ProtocolBridgeRefused("invalid function name")
    return value


def normalize_wire_defaults(request: dict[str, Any]) -> dict[str, Any]:
    """Known no-op wire fields only; active unsupported settings remain errors.

    Shared with durable deduplication, so explicit defaults do not buy a retry.
    These fields come from the ResponsesApiRequest serializer at the commit the
    bundled binary is built from (be6e8eac, tag rust-v0.147.0). What that binary
    sends for an unknown model slug is written down, field by field, in
    docs/V42-DEFAULT-REQUEST-MATRIX-2026-09-12.zh.md; `reasoning: {}` is its
    normal shape once the product config turns summaries off.
    """
    if not isinstance(request, dict):
        raise ProtocolBridgeRefused("request must be an object")
    value = copy.deepcopy(request)
    metadata_receipt = client_metadata_receipt(value)
    if not metadata_receipt:
        value.pop('client_metadata', None)
    reasoning = value.pop("reasoning", None)
    if reasoning is not None:
        if not isinstance(reasoning, dict) or set(reasoning) - {"effort", "summary"}:
            raise ProtocolBridgeRefused("unsupported reasoning fields")
        if reasoning.get("summary") is not None:
            raise ProtocolBridgeRefused("reasoning summaries are not supported by this bridge")
        effort = reasoning.get("effort")
        if effort is not None:
            if not isinstance(effort, str) or effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise ProtocolBridgeRefused("unsupported reasoning effort")
            value["reasoning"] = {"effort": effort}
    cache_key = value.pop("prompt_cache_key", None)
    if cache_key is not None:
        if (not isinstance(cache_key, str) or not 1 <= len(cache_key) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in cache_key)):
            raise ProtocolBridgeRefused("invalid prompt cache key")
        value["prompt_cache_key"] = cache_key
    for key in ("stream_options", "service_tier",
                "text", "access_programs"):
        if key in value:
            if value[key] is not None:
                raise ProtocolBridgeRefused("active wire option is not supported by this bridge")
            del value[key]
    if "include" in value:
        include = value["include"]
        if not isinstance(include, list) or any(item not in VACUOUS_INCLUDE for item in include):
            raise ProtocolBridgeRefused("additional response fields are not supported")
        del value["include"]
    if value.get("tools") is None or value.get("tools") == []:
        value.pop("tools", None)
    return value


# What happened to the binary's prompt_cache_key. The key is a performance
# hint by API definition (and already outside the request's identity): a
# model that does not declare the parameter gets the request without it,
# and the receipt says so. Reasoning effort is not treated this way -- it
# changes what the model does, so an undeclared model still refuses it.
CACHE_HINT_FORWARDED = "forwarded_opaque"
CACHE_HINT_NOT_FORWARDED = "not_forwarded_model_undeclared"


def _prepared_chat_request(request: dict[str, Any], *, model: str) -> tuple[dict[str, Any], str | None]:
    from ampuly.llm.custom_tool_codec import prepare_request
    prepared, _ = prepare_request(normalize_wire_defaults(request))
    return _function_chat_request(prepared, model=model)


def chat_request(request: dict[str, Any], *, model: str) -> dict[str, Any]:
    return _prepared_chat_request(request, model=model)[0]


def _tool_output_text(output: Any) -> str:
    """The binary sends a string, or content items; text items are joined, media is refused."""
    if isinstance(output, str):
        return output
    if isinstance(output, list) and output and all(
            isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str)
            for part in output):
        return "\n".join(part["text"] for part in output)
    raise ProtocolBridgeRefused("tool output must be text; media or encrypted tool output is not carried by this bridge")


def _function_chat_request(request: dict[str, Any], *, model: str) -> tuple[dict[str, Any], str | None]:
    """No caller credentials, model switching, missing history or global cache."""
    permitted = {"model", "input", "instructions", "tools", "tool_choice", "temperature", "top_p", "max_output_tokens", "parallel_tool_calls", "store", "stream", "reasoning", "prompt_cache_key", "client_metadata"}
    if not isinstance(request, dict) or set(request) - permitted:
        raise ProtocolBridgeRefused("unsupported request fields; full stateless history is required")
    if request.get("model", model) != model:
        raise ProtocolBridgeRefused("request model differs from the server-bound model")
    effort = (request.get("reasoning") or {}).get("effort")
    cache_key = request.get("prompt_cache_key")
    supported: Any = None
    if effort is not None or cache_key is not None:
        # LiteLLM's global drop_params=True overrides a per-call False in
        # 1.82.1. Check the local provider declaration before that lossy path.
        from litellm.litellm_core_utils.get_supported_openai_params import get_supported_openai_params
        try:
            supported = get_supported_openai_params(model=model)
        except Exception:
            supported = None
        if effort is not None and (not isinstance(supported, list) or "reasoning_effort" not in supported):
            raise ProtocolBridgeRefused("bound model does not declare reasoning effort support")
    cache_hint = None
    if cache_key is not None:
        cache_hint = (CACHE_HINT_FORWARDED if isinstance(supported, list) and "prompt_cache_key" in supported
                      else CACHE_HINT_NOT_FORWARDED)
    if any(request.get(key) is not None and request.get(key) is not False for key in ("store", "stream")):
        raise ProtocolBridgeRefused("stored sessions and streaming are not implemented by this component")
    if request.get("parallel_tool_calls") is not None and type(request["parallel_tool_calls"]) is not bool:
        raise ProtocolBridgeRefused("parallel_tool_calls must be a boolean")
    try:
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ProtocolBridgeRefused("request must be finite JSON") from None
    if len(encoded.encode()) > 512_000:
        raise ProtocolBridgeRefused("request exceeds the bounded bridge payload")
    for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1), ("max_output_tokens", 1, 32768)):
        value = request.get(key)
        if value is not None and (type(value) not in (int, float) or not low <= value <= high
                                  or (key == "max_output_tokens" and type(value) is not int)):
            raise ProtocolBridgeRefused("model parameter is outside the supported bound")
    tools = request.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 64:
        raise ProtocolBridgeRefused("tool list exceeds the supported shape")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ProtocolBridgeRefused("only function tools are supported; custom tools need an explicit adapter")
        name = _function_name(tool.get("name"))
        if name in names or not isinstance(tool.get("parameters", {}), dict):
            raise ProtocolBridgeRefused("duplicate tool or invalid parameters")
        names.add(name)
    choice = request.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") != "function" or choice.get("name") not in names:
            raise ProtocolBridgeRefused("tool choice does not name an offered function")
    elif choice not in (None, "auto", "none", "required"):
        raise ProtocolBridgeRefused("unsupported tool choice")
    if choice == "required" and not names:
        raise ProtocolBridgeRefused("required tool choice needs offered tools")
    items = request.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items or len(items) > 256:
        raise ProtocolBridgeRefused("bounded, nonempty full input history required")
    instructions = request.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ProtocolBridgeRefused("instructions must be text")
    convert = _converter()
    # Use the library for parameter/schema conversion, but never pass a tool
    # output into its cache-aware input converter.
    translated = convert.transform_responses_api_request_to_chat_completion_request(
        model=model, input="", responses_api_request=copy.deepcopy(
            {key: value for key, value in request.items() if key != 'client_metadata'}), stream=False,
    )
    if effort is not None and translated.get("reasoning_effort") != effort:
        raise ProtocolBridgeRefused("converter did not preserve reasoning effort")
    translated.pop("prompt_cache_key", None)
    if cache_hint == CACHE_HINT_FORWARDED:
        # 1.82.1's Responses converter omits this standard Chat field.
        # Scope rewriting happens in complete(), never in the request body.
        translated["prompt_cache_key"] = request["prompt_cache_key"]
    if isinstance(choice, dict):
        # 1.82.1 maps Responses' {type:function,name:...} to merely "required",
        # losing the selected tool. Preserve the precise standard Chat shape.
        translated["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append(convert.transform_instructions_to_system_message(instructions))
    pending: set[str] = set()
    observed: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ProtocolBridgeRefused("each input item must be an object")
        kind = item.get("type", "message")
        if kind == "function_call":
            if pending and messages and messages[-1].get("role") == "tool":
                raise ProtocolBridgeRefused("parallel tool outputs must finish before the next assistant call")
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in observed or len(call_id) > 256:
                raise ProtocolBridgeRefused("function call requires a unique bounded call id")
            _function_name(item.get("name"))
            if not isinstance(item.get("arguments"), str):
                raise ProtocolBridgeRefused("function arguments must preserve their wire string")
            converted = convert._transform_responses_api_function_call_to_chat_completion_message(copy.deepcopy(item))[0]
            for tool_call in converted["tool_calls"]:
                tool_call.pop("index", None)
            # Consecutive parallel calls belong to one assistant message.
            if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                messages[-1]["tool_calls"].extend(converted["tool_calls"])
            else:
                messages.append(converted)
            observed.add(call_id)
            pending.add(call_id)
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ProtocolBridgeRefused("tool output must answer a pending call in this request, exactly once")
            # This standard message mapping is intentionally local: LiteLLM's
            # corresponding function consults a process-wide bare-call-id cache.
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _tool_output_text(item.get("output"))})
            pending.remove(call_id)
        elif kind == "message":
            if pending or item.get("role") not in {"system", "developer", "user", "assistant"}:
                raise ProtocolBridgeRefused("message role invalid or tool results are missing")
            content = item.get("content")
            if isinstance(content, list):
                if not content or any(not isinstance(part, dict) or part.get("type") not in {"input_text", "output_text"} or not isinstance(part.get("text"), str) for part in content):
                    raise ProtocolBridgeRefused("this component supports text content only")
            elif not isinstance(content, str):
                raise ProtocolBridgeRefused("text message content required")
            messages.extend(convert._transform_responses_api_input_item_to_chat_completion_message(copy.deepcopy(item)))
        else:
            raise ProtocolBridgeRefused("unsupported input item; history will not be silently dropped")
    if pending:
        raise ProtocolBridgeRefused("model request contains unanswered tool calls")
    translated["messages"] = messages
    return translated, cache_hint


@dataclass(frozen=True)
class BridgedResponse:
    response: dict[str, Any]
    accounting: dict[str, Any]


def responses_result(result: dict[str, Any], *, request: dict[str, Any]) -> BridgedResponse:
    from ampuly.llm.custom_tool_codec import prepare_request, restore_response
    normalized = normalize_wire_defaults(request)
    receipt = client_metadata_receipt(normalized)
    normalized.pop('client_metadata', None)
    prepared, grammars = prepare_request(normalized)
    converted = _function_responses_result(result, request=prepared)
    return BridgedResponse(restore_response(converted.response, grammars), {**converted.accounting, **receipt})


def _function_responses_result(result: dict[str, Any], *, request: dict[str, Any]) -> BridgedResponse:
    """Reuse response conversion without writing tool definitions to global state."""
    convert = _converter()
    finish = result.get("finish_reason")
    if finish not in {"stop", "tool_calls", "length", "content_filter"}:
        raise ProtocolBridgeRefused("no supported terminal model result; completion cannot be assumed")
    calls = result.get("tool_calls") or []
    if not isinstance(calls, list) or (finish == "tool_calls" and not calls):
        raise ProtocolBridgeRefused("tool-call result lacks its calls")
    names = {tool["name"] for tool in request.get("tools", [])}
    output_calls = []
    ids = set()
    from openai.types.responses import ResponseFunctionToolCall
    choice = request.get("tool_choice")
    if calls and choice == "none":
        raise ProtocolBridgeRefused("model returned calls despite disabled tools")
    serialized_dropped = 0
    if len(calls) > 1 and request.get("parallel_tool_calls") is False:
        # The binary asked for one call at a time (its fallback ModelInfo says
        # parallel_tool_calls=false) and the Chat provider returned several:
        # DeepSeek via LiteLLM does exactly this on ordinary instructions (V2,
        # 2026-09-12), so refusing here failed the turn. Serialising is what
        # the contract means: the first call goes back now; the others are not
        # returned and never enter the binary's history, so the next request
        # rebuilds a chat history in which they did not happen and the model
        # re-plans from the first call's output. Recorded, not silent.
        serialized_dropped = len(calls) - 1
        calls = calls[:1]
    if not calls and finish == "stop" and (choice == "required" or isinstance(choice, dict)):
        raise ProtocolBridgeRefused("required tool call was not returned")
    for call in calls:
        if not isinstance(call, dict):
            raise ProtocolBridgeRefused("model returned an invalid tool call")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256 or call_id in ids or call.get("function") not in names:
            raise ProtocolBridgeRefused("model returned duplicate or unoffered tool calls")
        if isinstance(choice, dict) and call.get("function") != choice.get("name"):
            raise ProtocolBridgeRefused("model ignored the specified tool choice")
        ids.add(call_id)
        raw = call.get("arguments_raw")
        arguments = raw if isinstance(raw, str) else json.dumps(call.get("arguments", {}), ensure_ascii=False)
        output_calls.append(ResponseFunctionToolCall(
            id=f"fc_{uuid.uuid4().hex}", call_id=call_id, name=call["function"], arguments=arguments,
            type="function_call", status="completed",
        ).model_dump(exclude_none=True))
    wire = {
        "id": f"resp_{uuid.uuid4().hex}", "model": result.get("model"),
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": result.get("content") or ""}}],
        "usage": result.get("usage") if result.get("usage_known") else None,
    }
    response = convert.transform_chat_completion_response_to_responses_api_response(
        request_input=request["input"], responses_api_request=request, chat_completion_response=wire,
    ).model_dump(exclude_none=True)
    response["object"] = "response"
    if not result.get("content"):
        response["output"] = []
    response["output"].extend(output_calls)
    if not result.get("usage_known"):
        response["usage"] = None
    elif isinstance(response.get("usage"), dict):
        # The converter omits the detail objects the official Response
        # schema requires; without them the stream projection refuses the
        # result. Details are zero here because this bridge has no cache
        # or reasoning tokens to report, and the total is the parts'
        # sum when the provider did not give one.
        usage = response["usage"]
        usage.setdefault("input_tokens_details", {"cached_tokens": 0, "cache_write_tokens": 0})
        usage.setdefault("output_tokens_details", {"reasoning_tokens": 0})
        if not usage.get("total_tokens"):
            usage["total_tokens"] = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    if finish in {"length", "content_filter"}:
        response["incomplete_details"] = {"reason": "max_output_tokens" if finish == "length" else "content_filter"}
    accounting = {
        "model": result.get("model"), "usage_known": bool(result.get("usage_known")),
        "cost_known": bool(result.get("cost_known")),
        "cost": result.get("cost") if result.get("cost_known") else None,
    }
    if serialized_dropped:
        accounting["serialized_calls_dropped"] = serialized_dropped
    return BridgedResponse(response, accounting)


class ResponsesChatBridge:
    """One model request, no tool execution, retries, fallback or hidden sessions."""
    def __init__(self, provider: Any, *, model: str, timeout: float = 120) -> None:
        if not model or not 0 < timeout <= 300:
            raise ValueError("a server-bound model and bounded timeout are required")
        self.provider = provider
        self.model = model
        self.timeout = timeout

    async def complete(self, request: dict[str, Any], *, before_dispatch: Any = None,
                       cache_namespace: str | None = None) -> BridgedResponse:
        request = copy.deepcopy(request)
        data, cache_hint = _prepared_chat_request(request, model=self.model)
        if cache_hint == CACHE_HINT_NOT_FORWARDED:
            # Not forwarded, and not echoed: the response converter must not
            # see a key the model never received.
            request.pop("prompt_cache_key", None)
        if data.get("prompt_cache_key") is not None:
            if not isinstance(cache_namespace, str) or not re.fullmatch(r"[0-9a-f]{64}", cache_namespace):
                raise ProtocolBridgeRefused("prompt cache requires a server-bound namespace")
            opaque_key = hashlib.sha256(json.dumps(
                ["superones-cache-v1", cache_namespace, data["prompt_cache_key"]],
                ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest()
            data["prompt_cache_key"] = opaque_key
            # A response converter must never echo the caller's raw identifier.
            request["prompt_cache_key"] = opaque_key
        # Call the existing budgeted boundary, never raw litellm.acompletion.
        if self.provider._select_model(None) != self.model:
            raise ProtocolBridgeRefused("configured provider route changed; rebind before calling")
        kwargs = {key: data[key] for key in ("tool_choice", "top_p", "parallel_tool_calls", "temperature", "max_tokens") if key in data}
        if data.get("reasoning_effort") is not None:
            kwargs["reasoning_effort"] = data["reasoning_effort"]
        if data.get("prompt_cache_key") is not None:
            kwargs["prompt_cache_key"] = data["prompt_cache_key"]
        result = await asyncio.wait_for(self.provider.chat(
            messages=data["messages"], tools=data.get("tools"), num_retries=0,
            before_dispatch=before_dispatch, **kwargs,
        ), timeout=self.timeout)
        if result.get("model") != self.model:
            raise ProtocolBridgeRefused("provider returned a different model route")
        bridged = responses_result(result, request=request)
        if cache_hint is None:
            return bridged
        return BridgedResponse(bridged.response, {**bridged.accounting, "cache_hint": {"policy": cache_hint}})
