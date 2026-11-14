"""One prepared desktop image request, one final transport hand-off.

This is an in-memory list subtype, not a new provider, transport or storage.
The original provider serializes it as an ordinary JSON array. Its private
ticket is neither a message field nor a LiteLLM argument. A prepared request
cannot be reused, silently rerouted, or authorized by a default endpoint.
"""
from __future__ import annotations

import hashlib
import json

from ampuly.llm.budget import CallNotCharged, RouteRefused


class DesktopFrameDispatchRefused(RouteRefused, CallNotCharged):
    """A local disclosure refusal, including the final pre-transport check.

CallNotCharged matters if a refusal is raised inside _acompletion, after the
outer provider has marked dispatch but before LiteLLM is invoked. No image
request reached a transport; the original budget must release, not mark an
unknown paid call. We never use this exception for transport failures.
"""


def _refuse(code):
    raise DesktopFrameDispatchRefused(
        '桌面图像外发许可无法核实；本次请求没有发送。', code=code)


def _hash(messages):
    try:
        raw = json.dumps(messages, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(raw) > 8 * 1024 * 1024:
            _refuse('desktop_frame_message_too_large')
        return hashlib.sha256(raw).digest()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _refuse('desktop_frame_messages_invalid')


def _has_images(messages):
    for message in messages if isinstance(messages, list) else ():
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list) and any(isinstance(part, dict) and
                part.get('type') in {'image_url', 'input_image', 'image'} for part in content):
            return True
    return False


def explicit_endpoint(provider):
    from ampuly.computer_use.desktop_frames import FrameRefused, route_identity
    value = getattr(provider, '_api_base', None)
    if type(value) is not str or not value:
        raise FrameRefused('desktop_frame_explicit_endpoint_required')
    # Do not read LiteLLM/OpenAI process globals or environment variables. An
    # explicit call api_base is mandatory for visual disclosure, so later
    # changes to their implicit defaults cannot choose the recipient.
    route_identity('explicit/endpoint', value)
    return value


class FrameOutboundMessages(list):
    def __init__(self, messages, provider, task, vault, receipt):
        super().__init__(messages)
        self._provider, self._task = provider, task
        self._vault, self._receipt = vault, receipt
        self._digest, self._phase = _hash(self), 'ready'


def bind_frame_messages(provider, task, vault, messages, receipt):
    from ampuly.computer_use.desktop_frames import _PROVIDER, _TASK
    if _PROVIDER.get() is not provider or _TASK.get() is not task:
        _refuse('desktop_frame_provider_changed')
    explicit_endpoint(provider)
    return FrameOutboundMessages(messages, provider, task, vault, receipt)


def _validated(provider, call, phase):
    from ampuly.computer_use.desktop_frames import FrameRefused, _PROVIDER, _TASK
    messages = call.get('messages')
    if not isinstance(messages, FrameOutboundMessages):
        # A plain-list copy loses the private receipt. It is not accepted as a
        # replacement for a desktop image request in the current task scope.
        from ampuly.plugins.office_ui.desktop_connector import _step
        task = _TASK.get()
        if task is not None and _step(task) is not None and _has_images(messages):
            _refuse('desktop_frame_ticket_missing')
        return None
    if (messages._phase != phase or messages._provider is not provider
            or _PROVIDER.get() is not provider or _TASK.get() is not messages._task
            or messages._digest != _hash(messages)):
        _refuse('desktop_frame_request_changed_or_reused')
    try:
        base = explicit_endpoint(provider)
        if call.get('api_base') != base:
            _refuse('desktop_frame_final_endpoint_changed')
        if call.get('num_retries') != 0 or 'max_retries' in call:
            _refuse('desktop_frame_transport_retry_forbidden')
        messages._vault.recheck(messages._task, messages._receipt,
                                call.get('model'), call.get('api_base'))
    except FrameRefused as exc:
        _refuse(exc.reason)
    return messages


def before_frame_dispatch(provider, call):
    """After ledger/model checks, before original mark_dispatched()."""
    try:
        messages = _validated(provider, call, 'ready')
    except DesktopFrameDispatchRefused:
        messages = call.get('messages')
        if isinstance(messages, FrameOutboundMessages):
            messages._phase = 'refused'
        raise
    if messages is not None:
        messages._phase = 'checked'


def consume_frame_dispatch(provider, call):
    """Last local check, immediately before original LiteLLM transport."""
    try:
        messages = _validated(provider, call, 'checked')
    except DesktopFrameDispatchRefused:
        messages = call.get('messages')
        if isinstance(messages, FrameOutboundMessages):
            messages._phase = 'refused'
        raise
    if messages is not None:
        # Even a received transport rejection must obtain a fresh observation
        # and a new prepared request; never resend these private image bytes.
        messages._phase = 'consumed'
