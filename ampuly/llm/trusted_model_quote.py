"""Private bounded quote preparation, NEVER spending or dispatch permission.

There is currently NO reviewed production supplier implementation. Production
bind/prepare/recheck fail closed. The separate technical_verification lane has
one compiled synthetic text adapter, not a supplier price/cap/bill assertion.
No Provider, budget scope, transport, database, configuration or clock is opened.
Future Provider wiring must supply its actual prepared call and server binding;
passing a constructor/caller dict here is not proof of that producer or authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import math
from typing import Any


MAX_REQUEST_BYTES = 1024 * 1024
MAX_JSON_NODES = 50000
MAX_JSON_DEPTH = 64
MAX_SLOTS = 32
MAX_INTEGER = 2**63 - 1
MAX_CHARGE_UNITS = 2**53 - 1
UNIT_SCALE = 1000000
_INVALID = "invalid_source"
_UNAVAILABLE = "unavailable"
_UNSUPPORTED = "unsupported"
_EXPIRED = "quote_expired"
_CODES = (_INVALID, _UNAVAILABLE, _UNSUPPORTED, _EXPIRED)
_STOP = object()
_SLOT_CONSTRUCTOR = object()

# Empty by review, not configurable. Adding a real implementation is another
# reviewed SOURCE change, not a client adapter_id/boolean/price declaration.
_REVIEWED_PRODUCTION_ADAPTERS: tuple[object, ...] = ()
_TECHNICAL_PROVIDER = "technical-verification"
_TECHNICAL_MODEL = "technical-verification/text-chat-v1"
_TECHNICAL_ENDPOINT = "technical_verification.text_chat"
_TECHNICAL_BASE = "technical-verification://text-chat"
_TECHNICAL_SOURCE = "technical-verification.quote-source.v1"
_TECHNICAL_REVISION = "1"
_TECHNICAL_EVIDENCE = "synthetic-framing-tariff.v1-not-supplier-evidence"
_TECHNICAL_MAX_OUTPUT = 32768
_TECHNICAL_VALID_SECONDS = 300
# Synthetic capability definition: 8 request framing + 4/message + UTF8 text
# and role bytes; output respects this synthetic protocol's max_tokens.
# Synthetic micro-CNY rates are input 1/3 and output 2/3 per token. No fees,
# caching or reasoning exist in this synthetic protocol; no supplier claim.
_TECHNICAL_FRAME = 8
_TECHNICAL_MESSAGE_FRAME = 4
_TECHNICAL_RATE_DENOMINATOR = 3
_TECHNICAL_INPUT_NUMERATOR = 1
_TECHNICAL_OUTPUT_NUMERATOR = 2
_SLOTS: dict[int, _TargetQuoteSlot] = {}


class TrustedQuotePrepareError(ValueError):
    """Fixed code/message only; no body, credential, adapter or private cause."""

    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _CODES else _UNAVAILABLE
        super().__init__(self.code)


class _QuoteFault(Exception):
    pass


def _fault(code: str = _INVALID) -> None:
    raise _QuoteFault(code)


def _boundary(method):
    @wraps(method)
    def call(*args, **kwargs):
        failure = None
        try:
            return method(*args, **kwargs)
        except _QuoteFault as exc:
            failure = exc.args[0] if (len(exc.args) == 1 and type(exc.args[0]) is str
                                     and exc.args[0] in _CODES) else _UNAVAILABLE
        except TrustedQuotePrepareError as exc:
            failure = exc.code if type(exc) is TrustedQuotePrepareError else _UNAVAILABLE
        except Exception:
            failure = _UNAVAILABLE
        # Outside handlers. External active-except/traceback locals are not
        # globally scrubbed. BaseException is never caught or substituted.
        raise TrustedQuotePrepareError(failure) from None
    return call


def _text(value: Any) -> str:
    if (type(value) is not str or not value or len(value) > 512
            or value.strip() != value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        _fault()
    invalid = False
    try:
        invalid = len(value.encode("utf-8")) > 512
    except UnicodeError:
        invalid = True
    if invalid:
        _fault()
    return value


def _clock(value: Any) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        _fault()
    return value


class _TargetQuoteSlot:
    __slots__ = ("_target", "_binding", "_generation")

    def __init__(self, target: object, *, _constructor: object):
        if _constructor is not _SLOT_CONSTRUCTOR:
            _fault()
        self._target = target
        self._binding = None
        self._generation = 0

    def __repr__(self) -> str:
        return "<TargetQuoteSlot>"


class _TechnicalVerificationTextAdapter:
    __slots__ = ()

    @property
    def production_eligible(self) -> bool:
        return False

    def _billable_bounds(self, call: dict[str, Any]) -> dict[str, int]:
        return _technical_bounds(call)

    def _max_charge_units(self, bounds: dict[str, int]) -> int:
        return _technical_charge(bounds)


_TECHNICAL_ADAPTER = _TechnicalVerificationTextAdapter()


@dataclass(frozen=True, slots=True, repr=False)
class _TechnicalRegistration:
    target: object
    adapter: _TechnicalVerificationTextAdapter
    billing_binding_id: str
    billing_epoch: str
    generation: int


@dataclass(frozen=True, slots=True, repr=False)
class PreparedPricedCall:
    """Selected quote observation; constructors are NOT producer/permission proof."""

    selected: Any
    quote_payload: bytes
    quote_sha256: str
    prepared_fingerprint: str
    _sealed_call_bytes: bytes
    _registration: _TechnicalRegistration
    _slot_generation: int

    @property
    def production_eligible(self) -> bool:
        # Compiled capability, not a constructor/client boolean. No reviewed
        # production concrete type is implemented in this source version.
        return False

    @property
    def is_authorized(self) -> bool:
        return False


def _slot(slot: Any, target: object) -> _TargetQuoteSlot:
    if type(slot) is not _TargetQuoteSlot:
        _fault()
    if (slot._target is not target or _SLOTS.get(id(target)) is not slot
            or type(slot._generation) is not int
            or not 0 <= slot._generation <= MAX_INTEGER):
        _fault()
    return slot


def _next_generation(slot: _TargetQuoteSlot) -> int:
    if slot._generation >= MAX_INTEGER:
        _fault(_UNAVAILABLE)
    return slot._generation + 1


def _registration(slot: _TargetQuoteSlot, target: object,
                  billing_binding_id: Any, billing_epoch: Any) -> _TechnicalRegistration:
    billing = _text(billing_binding_id)
    epoch = _text(billing_epoch)
    binding = slot._binding
    if type(binding) is not _TechnicalRegistration:
        _fault(_UNAVAILABLE)
    _text(binding.billing_binding_id)
    _text(binding.billing_epoch)
    if (binding.target is not target or binding.adapter is not _TECHNICAL_ADAPTER
            or type(binding.adapter) is not _TechnicalVerificationTextAdapter
            or type(binding.generation) is not int or not 1 <= binding.generation <= MAX_INTEGER
            or binding.generation != slot._generation
            or binding.billing_binding_id != billing or binding.billing_epoch != epoch):
        _fault()
    return binding


@_boundary
def _new_quote_slot(target: object) -> _TargetQuoteSlot:
    """Bounded identity-only slot; does not establish target/provider authority."""
    if target is None:
        _fault()
    current = _SLOTS.get(id(target))
    if current is not None:
        return _slot(current, target)
    if len(_SLOTS) >= MAX_SLOTS:
        _fault(_UNAVAILABLE)
    slot = _TargetQuoteSlot(target, _constructor=_SLOT_CONSTRUCTOR)
    _SLOTS[id(target)] = slot
    return slot


@_boundary
def _bind_reviewed_adapter(slot: Any, *, target: object, billing_binding_id: str,
                           billing_epoch: str, adapter_id: str) -> None:
    _slot(slot, target)
    _text(billing_binding_id)
    _text(billing_epoch)
    _text(adapter_id)
    # No runtime registration escape hatch, including the technical id.
    # Even a changed private table cannot install an arbitrary caller type.
    if _REVIEWED_PRODUCTION_ADAPTERS != ():
        _fault(_UNAVAILABLE)
    _fault(_UNAVAILABLE)


@_boundary
def _prepare_registered_quote(slot: Any, *, target: object, billing_binding_id: str,
                              billing_epoch: str, prepared_kwargs: Any, quote_id: str,
                              checked_at: datetime) -> PreparedPricedCall:
    _slot(slot, target)
    _text(billing_binding_id)
    _text(billing_epoch)
    _text(quote_id)
    _clock(checked_at)
    # No reviewed supplier: never infer a production capability from a
    # technical binding, body fields, carrier constructor or supported flag.
    _fault(_UNAVAILABLE)


@_boundary
def _recheck_prepared_quote(slot: Any, prepared: Any, *, target: object,
                            billing_binding_id: str, billing_epoch: str,
                            checked_at: datetime) -> None:
    _slot(slot, target)
    _text(billing_binding_id)
    _text(billing_epoch)
    _clock(checked_at)
    _fault(_UNAVAILABLE)


@_boundary
def _bind_technical_verification_adapter(slot: Any, *, target: object,
                                         billing_binding_id: str, billing_epoch: str) -> None:
    slot = _slot(slot, target)
    billing = _text(billing_binding_id)
    epoch = _text(billing_epoch)
    old = slot._binding
    if old is not None:
        if type(old) is not _TechnicalRegistration:
            _fault()
        _registration(slot, target, old.billing_binding_id, old.billing_epoch)
        if old.billing_epoch == epoch and old.billing_binding_id != billing:
            _fault()
        if old.billing_epoch == epoch and old.billing_binding_id == billing:
            _registration(slot, target, billing, epoch)
            return None
    generation = _next_generation(slot)
    binding = _TechnicalRegistration(target, _TECHNICAL_ADAPTER, billing, epoch, generation)
    slot._binding = binding
    slot._generation = generation
    return None


@_boundary
def _revoke_quote_slot(slot: Any, *, target: object) -> None:
    slot = _slot(slot, target)
    generation = _next_generation(slot)
    slot._binding = None
    slot._generation = generation
    return None


class _JSONBudget:
    __slots__ = ("nodes", "bytes", "active")

    def __init__(self):
        self.nodes = 0
        self.bytes = 0
        self.active: set[int] = set()

    def string(self, value: str) -> None:
        if len(value) > MAX_REQUEST_BYTES:
            _fault()
        invalid = False
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            invalid = True
            size = 0
        if invalid:
            _fault()
        self.bytes += size
        if self.bytes > MAX_REQUEST_BYTES:
            _fault()

    def clone(self, value: Any, depth: int = 0):
        self.nodes += 1
        if self.nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            _fault()
        kind = type(value)
        if kind is str:
            self.string(value)
            return value
        if value is None or kind is bool:
            return value
        if kind is int:
            if not -MAX_INTEGER <= value <= MAX_INTEGER:
                _fault()
            return value
        if kind is float:
            if not math.isfinite(value):
                _fault()
            return value
        if kind not in (dict, list):
            _fault()
        identity = id(value)
        if identity in self.active:
            _fault()
        length = len(value)
        if length > MAX_JSON_NODES - self.nodes:
            _fault()
        self.active.add(identity)
        try:
            if kind is list:
                result = []
                for index in range(length):
                    if len(value) != length:
                        _fault()
                    result.append(self.clone(value[index], depth + 1))
                if len(value) != length:
                    _fault()
                return result
            result = {}
            items = iter(dict.items(value))
            for _ in range(length):
                if len(value) != length:
                    _fault()
                pair = next(items, _STOP)
                if pair is _STOP:
                    _fault()
                key, item = pair
                if type(key) is not str:
                    _fault()
                self.nodes += 1
                if self.nodes > MAX_JSON_NODES:
                    _fault()
                self.string(key)
                result[key] = self.clone(item, depth + 1)
            if len(value) != length or next(items, _STOP) is not _STOP:
                _fault()
            return result
        finally:
            self.active.remove(identity)


def _canonical(value: Any, *, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(raw) <= maximum:
        _fault()
    return raw


def _technical_call(prepared_kwargs: Any) -> tuple[dict[str, Any], bytes]:
    if type(prepared_kwargs) is not dict:
        _fault()
    call = _JSONBudget().clone(prepared_kwargs)
    required = {"model", "messages", "temperature", "max_tokens", "num_retries"}
    optional = {"timeout", "api_key", "api_base"}
    if not required <= call.keys() or call.keys() - required - optional:
        _fault(_UNSUPPORTED)
    if type(call["model"]) is not str or call["model"] != _TECHNICAL_MODEL:
        _fault(_UNSUPPORTED)
    if (type(call["max_tokens"]) is not int
            or not 1 <= call["max_tokens"] <= _TECHNICAL_MAX_OUTPUT
            or type(call["num_retries"]) is not int or call["num_retries"] != 0):
        _fault(_UNSUPPORTED)
    temperature = call["temperature"]
    if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
        _fault(_UNSUPPORTED)
    if "timeout" in call:
        timeout = call["timeout"]
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            _fault(_UNSUPPORTED)
    if "api_base" in call and (type(call["api_base"]) is not str
                                or call["api_base"] != _TECHNICAL_BASE):
        _fault(_UNSUPPORTED)
    if "api_key" in call and (type(call["api_key"]) is not str or not call["api_key"]):
        _fault()
    messages = call["messages"]
    if type(messages) is not list or not 1 <= len(messages) <= 256:
        _fault(_UNSUPPORTED)
    length = len(messages)
    for index in range(length):
        message = messages[index]
        if (type(message) is not dict or set(message) != {"role", "content"}
                or type(message["role"]) is not str
                or message["role"] not in ("system", "user", "assistant")
                or type(message["content"]) is not str):
            _fault(_UNSUPPORTED)
    return call, _canonical(call)


def _codec():
    # Reuse ONLY the original B51 pure selected/quote/validity codec. No DB
    # helper is called. Root's loader maps this exact module to selected code;
    # real package import is not tested or called by this author.
    from ..plugins.office_ui.priced_model_reservation import (
        SelectedPreparedCall, PricedReservationError, _quote, _valid, _time,
    )
    return SelectedPreparedCall, PricedReservationError, _quote, _valid, _time


def _fingerprint(call: dict[str, Any], binding: _TechnicalRegistration) -> str:
    # Credentials are excluded even from public digests. The server opaque
    # billing identity/epoch must independently identify account rotation.
    public = {key: value for key, value in call.items() if key != "api_key"}
    raw = _canonical([
        "priced_model_prepared.v1", _TECHNICAL_SOURCE, _TECHNICAL_REVISION,
        _TECHNICAL_PROVIDER, _TECHNICAL_ENDPOINT,
        binding.billing_binding_id, binding.billing_epoch, public,
    ])
    return hashlib.sha256(raw).hexdigest()


def _technical_bounds(call: dict[str, Any]) -> dict[str, int]:
    messages = call["messages"]
    inputs = _TECHNICAL_FRAME
    length = len(messages)
    for index in range(length):
        message = messages[index]
        inputs += (_TECHNICAL_MESSAGE_FRAME + len(message["role"].encode("utf-8"))
                   + len(message["content"].encode("utf-8")))
    if not 1 <= inputs <= MAX_INTEGER:
        _fault()
    return {
        "input_tokens": inputs, "output_tokens": call["max_tokens"],
        "reasoning_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "extra_fee_units": 0,
    }


def _technical_charge(bounds: dict[str, int]) -> int:
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
              "cache_write_tokens", "extra_fee_units")
    if type(bounds) is not dict or len(bounds) != len(fields):
        _fault()
    checked = {}
    items = iter(dict.items(bounds))
    for _ in range(len(fields)):
        if len(bounds) != len(fields):
            _fault()
        pair = next(items, _STOP)
        if pair is _STOP:
            _fault()
        key, value = pair
        if type(key) is not str or key not in fields or type(value) is not int or not 0 <= value <= MAX_INTEGER:
            _fault()
        checked[key] = value
    if len(bounds) != len(fields) or next(items, _STOP) is not _STOP:
        _fault()
    if (checked.keys() != set(fields) or checked["input_tokens"] == 0
            or not 1 <= checked["output_tokens"] <= _TECHNICAL_MAX_OUTPUT
            or any(checked[field] != 0 for field in fields[2:])):
        _fault(_UNSUPPORTED)
    numerator = (checked["input_tokens"] * _TECHNICAL_INPUT_NUMERATOR
                 + checked["output_tokens"] * _TECHNICAL_OUTPUT_NUMERATOR)
    charge = (numerator + _TECHNICAL_RATE_DENOMINATOR - 1) // _TECHNICAL_RATE_DENOMINATOR
    if not 1 <= charge <= MAX_CHARGE_UNITS:
        _fault(_UNSUPPORTED)
    return charge


@_boundary
def _prepare_technical_verification_quote(
    slot: Any, *, target: object, billing_binding_id: str, billing_epoch: str,
    prepared_kwargs: Any, quote_id: str, checked_at: datetime,
) -> PreparedPricedCall:
    slot = _slot(slot, target)
    binding = _registration(slot, target, billing_binding_id, billing_epoch)
    quote_id = _text(quote_id)
    now = _clock(checked_at)
    call, sealed = _technical_call(prepared_kwargs)
    fingerprint = _fingerprint(call, binding)
    bounds = binding.adapter._billable_bounds(call)
    charge = binding.adapter._max_charge_units(bounds)
    selected_type, original_error, original_quote, original_valid, original_time = _codec()
    selected = selected_type(
        _TECHNICAL_SOURCE, _TECHNICAL_REVISION, _TECHNICAL_PROVIDER,
        binding.billing_binding_id, _TECHNICAL_MODEL, _TECHNICAL_ENDPOINT, fingerprint,
    )
    quote = {
        "quote_version": "model_pricing_quote.v1", "quote_id": quote_id,
        "source_id": _TECHNICAL_SOURCE, "source_revision": _TECHNICAL_REVISION,
        "provider_id": _TECHNICAL_PROVIDER, "billing_binding_id": binding.billing_binding_id,
        "requested_model": _TECHNICAL_MODEL, "endpoint_class": _TECHNICAL_ENDPOINT,
        "prepared_fingerprint": fingerprint, "currency": "CNY", "unit_scale": UNIT_SCALE,
        "max_charge_units": charge, "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(seconds=_TECHNICAL_VALID_SECONDS)).isoformat(),
        "evidence_id": _TECHNICAL_EVIDENCE, "billable_bounds": bounds,
        "billed_models": [_TECHNICAL_MODEL],
    }
    raw = _canonical(quote, maximum=64 * 1024)
    digest = hashlib.sha256(raw).hexdigest()
    decoded = original_quote(raw, digest, selected)
    original_valid(decoded, now)
    # Recheck registration identity after codec calls. No await/clock read;
    # this is not an OS/thread lock or a production source/dispatch proof.
    if _registration(slot, target, billing_binding_id, billing_epoch) is not binding:
        _fault()
    return PreparedPricedCall(selected, raw, digest, fingerprint, sealed, binding, slot._generation)


@_boundary
def _recheck_technical_verification_quote(
    slot: Any, prepared: Any, *, target: object, billing_binding_id: str,
    billing_epoch: str, checked_at: datetime,
) -> None:
    slot = _slot(slot, target)
    binding = _registration(slot, target, billing_binding_id, billing_epoch)
    now = _clock(checked_at)
    if (type(prepared) is not PreparedPricedCall or prepared._registration is not binding
            or type(prepared._slot_generation) is not int
            or prepared._slot_generation != slot._generation
            or type(prepared._sealed_call_bytes) is not bytes
            or not 0 < len(prepared._sealed_call_bytes) <= MAX_REQUEST_BYTES
            or type(prepared.prepared_fingerprint) is not str
            or len(prepared.prepared_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in prepared.prepared_fingerprint)):
        _fault()
    call = json.loads(prepared._sealed_call_bytes)
    call, sealed = _technical_call(call)
    if sealed != prepared._sealed_call_bytes:
        _fault()
    fingerprint = _fingerprint(call, binding)
    if prepared.prepared_fingerprint != fingerprint:
        _fault()
    selected_type, original_error, original_quote, original_valid, original_time = _codec()
    if type(prepared.selected) is not selected_type:
        _fault()
    quote = original_quote(prepared.quote_payload, prepared.quote_sha256, prepared.selected)
    expected_bounds = binding.adapter._billable_bounds(call)
    if (quote["prepared_fingerprint"] != fingerprint
            or quote["source_id"] != _TECHNICAL_SOURCE
            or quote["source_revision"] != _TECHNICAL_REVISION
            or quote["provider_id"] != _TECHNICAL_PROVIDER
            or quote["requested_model"] != _TECHNICAL_MODEL
            or quote["endpoint_class"] != _TECHNICAL_ENDPOINT
            or quote["billing_binding_id"] != binding.billing_binding_id
            or quote["evidence_id"] != _TECHNICAL_EVIDENCE
            or quote["billed_models"] != [_TECHNICAL_MODEL]
            or quote["billable_bounds"] != expected_bounds
            or quote["max_charge_units"] != binding.adapter._max_charge_units(expected_bounds)
            or original_time(quote["valid_until"]) - original_time(quote["valid_from"])
            != timedelta(seconds=_TECHNICAL_VALID_SECONDS)):
        _fault()
    expires = False
    try:
        original_valid(quote, now)
    except original_error as exc:
        if type(exc) is original_error and type(exc.code) is str and exc.code == _EXPIRED:
            expires = True
        else:
            _fault(_UNAVAILABLE)
    if expires:
        _fault(_EXPIRED)
    if _registration(slot, target, billing_binding_id, billing_epoch) is not binding:
        _fault()
    return None
