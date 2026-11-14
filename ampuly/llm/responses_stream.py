"""Buffered SSE projection, not a claim of token-by-token model streaming.

Uses the official OpenAI event schemas. No background producer, session cache,
network request or tool execution lives here. The caller authenticates requests
and awaits one budgeted completion before projecting its immutable result.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

from ampuly.llm.responses_bridge import BridgedResponse, ProtocolBridgeRefused


def buffered_events(result: BridgedResponse) -> Iterator[dict[str, Any]]:
    """Project text/function results; refuse unknown shapes before first event."""
    from openai.types import responses as wire

    body = copy.deepcopy(result.response)
    if body.get("status") not in {"completed", "incomplete"}:
        raise ProtocolBridgeRefused("only terminal results have a buffered event projection")
    for item in body.get("output", []):
        kind = item.get("type")
        if kind == "message":
            if any(part.get("type") != "output_text" for part in item.get("content", [])):
                raise ProtocolBridgeRefused("buffered stream supports text content only")
        elif kind not in {"function_call", "custom_tool_call"}:
            raise ProtocolBridgeRefused("unsupported output type must not disappear from stream")
    # Unlike the permissive constructors, model_validate checks required fields.
    body = wire.Response.model_validate(body).model_dump(mode="json")
    sequence = 0

    def event(event_class, event_type, **payload):
        nonlocal sequence
        value = event_class.model_validate({"type": event_type, "sequence_number": sequence, **payload})
        sequence += 1
        return value.model_dump(mode="json", exclude_none=True)

    initial = {**body, "output": [], "status": "in_progress", "usage": None,
               "incomplete_details": None, "completed_at": None}
    yield event(wire.ResponseCreatedEvent, "response.created", response=initial)
    yield event(wire.ResponseInProgressEvent, "response.in_progress", response=initial)
    for output_index, item in enumerate(body["output"]):
        fields = {"output_index": output_index, "item_id": item["id"]}
        added = {**item, "status": "in_progress"}
        if item["type"] == "message":
            added["content"] = []
        elif item["type"] == "custom_tool_call":
            added.pop("status", None)
            added["input"] = ""
        else:
            added["arguments"] = ""
        yield event(wire.ResponseOutputItemAddedEvent, "response.output_item.added",
                    output_index=output_index, item=added)
        if item["type"] == "message":
            for content_index, part in enumerate(item["content"]):
                text_fields = {**fields, "content_index": content_index}
                yield event(wire.ResponseContentPartAddedEvent, "response.content_part.added",
                            **text_fields, part={**part, "text": "", "annotations": []})
                if part["text"]:
                    yield event(wire.ResponseTextDeltaEvent, "response.output_text.delta",
                                **text_fields, delta=part["text"], logprobs=[])
                yield event(wire.ResponseTextDoneEvent, "response.output_text.done",
                            **text_fields, text=part["text"], logprobs=[])
                yield event(wire.ResponseContentPartDoneEvent, "response.content_part.done",
                            **text_fields, part=part)
        elif item["type"] == "custom_tool_call":
            if item["input"]:
                yield event(wire.ResponseCustomToolCallInputDeltaEvent, "response.custom_tool_call_input.delta",
                            **fields, delta=item["input"])
            yield event(wire.ResponseCustomToolCallInputDoneEvent, "response.custom_tool_call_input.done",
                        **fields, input=item["input"])
        else:
            if item["arguments"]:
                yield event(wire.ResponseFunctionCallArgumentsDeltaEvent, "response.function_call_arguments.delta",
                            **fields, delta=item["arguments"])
            yield event(wire.ResponseFunctionCallArgumentsDoneEvent, "response.function_call_arguments.done",
                        **fields, name=item["name"], arguments=item["arguments"])
        yield event(wire.ResponseOutputItemDoneEvent, "response.output_item.done",
                    output_index=output_index, item=item)
    terminal_class = wire.ResponseCompletedEvent if body["status"] == "completed" else wire.ResponseIncompleteEvent
    yield event(terminal_class, f"response.{body['status']}", response=body)


def buffered_sse(result: BridgedResponse) -> Iterator[bytes]:
    """Each chunk is one complete SSE record; text cannot inject another record."""
    for event in buffered_events(result):
        data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        yield f"event: {event['type']}\ndata: {data}\n\n".encode("utf-8")
