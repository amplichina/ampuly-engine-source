"""The model catalogue: every model the console lists, what it is, and whether it may be chosen (model-routing phase 0).

Each entry is one id as the console and litellm spell it. ``status`` says what the vendor did with it (``active``,
``vendor_routed_alias`` -- the vendor serves the name with another model --, ``retired``); ``availability`` says
whether it may be newly chosen here:

- ``available``;
- ``off_by_default`` -- an overseas vendor (the owner confirms the vendor's terms first, design decision 8);
- ``not_routable`` -- this engine cannot reach it correctly yet (litellm has no route, or the mainland host is not
  applied to the call);
- ``needs_params`` -- it needs phase-1 parameters first (thinking on by default with tool loops that must pass
  ``reasoning_content`` back, a window of up to 1M tokens before the 128k cap).

An existing setting is never rewritten or refused on load: a default the owner chose keeps running (the owner said
DeepSeek's retired names are served by DeepSeek's own routing). Only a *new* choice is checked, and a step's model is
checked when it runs.

``capabilities.vision`` is what reaches the model *through this engine*: litellm's DeepSeek transform flattens list
content to text, so no DeepSeek model sees an image here, whatever the vendor offers. Prices are references seen on
the vendors' pages on 2026-09-22 (per million tokens); they change, the vendor's page is what counts, and the owner
watches spend himself. ``api_base`` is recorded, not applied, in phase 0.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from types import FunctionType
from typing import Any

SEEN = "2026-09-22"

_DEEPSEEK = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
_QWEN = "https://help.aliyun.com/zh/model-studio/model-pricing"
_GLM = "https://docs.bigmodel.cn/cn/guide/start/pricing"
_KIMI = "https://platform.kimi.com/docs/pricing/chat"
_OPENAI = "https://developers.openai.com/api/docs/models/gpt-5.4-mini"
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/models/overview"


def _price(currency: str, source: str, input_: float | None = None, cached: float | None = None, output: float | None = None,
           *, off_peak: dict[str, float] | None = None, note: str = "") -> dict[str, Any]:
    price: dict[str, Any] = {"currency": currency, "unit": "1M tokens", "input": input_, "cached_input": cached,
                             "output": output, "seen": SEEN, "source": source, "note": note}
    if off_peak:
        price["off_peak"] = dict(off_peak)
    return price


_FLASH_CNY = dict(input_=2.0, cached=0.04, output=8.0, off_peak={"input": 1.0, "cached_input": 0.02, "output": 4.0})
_V4PRO_CNY = dict(input_=9.0, cached=0.30, output=27.0, off_peak={"input": 4.5, "cached_input": 0.15, "output": 13.5})


def _entry(model: str, vendor: str, *, api_base: str, hosting: str, trains: str, status: str = "active",
           successor: str = "", successor_mode: str = "", availability: str = "available", reason: str = "",
           tools: bool = True, vision: bool = False, thinking_default: bool = False, passback: bool = False,
           context_window: int | None = None, retires_not_before: str = "", offline_since: str = "",
           price: dict[str, Any] | None = None, note_zh: str = "", note_en: str = "") -> dict[str, Any]:
    return {
        "id": model, "vendor": vendor, "api_base": api_base, "hosting": hosting, "trains_on_inputs": trains,
        "status": status, "successor": successor, "successor_mode": successor_mode,
        "availability": availability, "availability_reason": reason,
        "capabilities": {"tools": tools, "vision": vision, "thinking_default": thinking_default,
                         "reasoning_passback_required": passback},
        "context_window": context_window, "retires_not_before": retires_not_before, "offline_since": offline_since,
        "price_ref": price or _price("", "", note="not recorded"), "note_zh": note_zh, "note_en": note_en,
    }


_DS = dict(vendor="deepseek", api_base="https://api.deepseek.com", hosting="cn_mainland", trains="opt_out")
_QW = dict(vendor="qwen", api_base="https://dashscope.aliyuncs.com/compatible-mode/v1", hosting="cn_mainland", trains="no")
_NEEDS_PARAMS_ZH = "第 1 期补上参数（思考模式、窗口上限）后才能选"
_NEEDS_PARAMS_EN = "selectable once phase 1 adds its parameters (thinking mode, window cap)"

# Keyed by the console's provider id (settings_service.LLM_PROVIDERS); each provider's ``models`` is derived from these,
# in this order -- the first available one is what the settings page's test button pings.
ENTRIES_BY_PROVIDER: dict[str, list[dict[str, Any]]] = {
    "deepseek": [
        _entry("deepseek/deepseek-chat", **_DS, status="vendor_routed_alias", successor="deepseek/deepseek-flash",
               successor_mode="non_thinking", context_window=131072,
               price=_price("CNY", _DEEPSEEK, **_FLASH_CNY, note="billed as the model DeepSeek routes the name to"),
               note_zh="DeepSeek 把这个名字路由到 deepseek-flash（非思考模式）；照常可用", note_en="served by deepseek-flash (non-thinking) per DeepSeek; usable as before"),
        _entry("deepseek/deepseek-reasoner", **_DS, status="vendor_routed_alias", successor="deepseek/deepseek-flash",
               successor_mode="thinking", passback=True, context_window=131072,
               price=_price("CNY", _DEEPSEEK, **_FLASH_CNY, note="billed as the model DeepSeek routes the name to"),
               note_zh="DeepSeek 把这个名字路由到 deepseek-flash（思考模式）；照常可用", note_en="served by deepseek-flash (thinking) per DeepSeek; usable as before"),
        _entry("deepseek/deepseek-flash", **_DS, availability="needs_params", reason="thinking_default_tool_loop",
               thinking_default=True, passback=True, context_window=1_000_000, price=_price("CNY", _DEEPSEEK, **_FLASH_CNY),
               note_zh=_NEEDS_PARAMS_ZH, note_en=_NEEDS_PARAMS_EN),
        _entry("deepseek/deepseek-v4-pro", **_DS, availability="needs_params", reason="thinking_default_tool_loop",
               thinking_default=True, passback=True, context_window=1_000_000, price=_price("CNY", _DEEPSEEK, **_V4PRO_CNY),
               note_zh=_NEEDS_PARAMS_ZH, note_en=_NEEDS_PARAMS_EN),
    ],
    "qwen": [
        _entry("dashscope/qwen-max", **_QW, context_window=30720, note_zh="老一代，窗口只有 3 万 token，不适合带工具的步骤",
               note_en="an older model with a 30k window; not suited to tool steps"),
        _entry("dashscope/qwen-plus", **_QW, context_window=129024, price=_price("CNY", _QWEN, 0.8, 0.16, 2.0, note="up to 128k input")),
        _entry("dashscope/qwen-flash", **_QW, availability="needs_params", reason="window_cap", context_window=997952,
               price=_price("CNY", _QWEN, 0.15, None, 1.5, note="up to 128k input"), note_zh=_NEEDS_PARAMS_ZH, note_en=_NEEDS_PARAMS_EN),
        _entry("dashscope/qwen3.7-plus", **_QW, availability="needs_params", reason="thinking_default", vision=False,
               thinking_default=True, context_window=1_000_000, price=_price("CNY", _QWEN, 2.0, 0.4, 8.0, note="up to 256k input"),
               note_zh=_NEEDS_PARAMS_ZH, note_en=_NEEDS_PARAMS_EN),
        _entry("dashscope/qwen3.8-max", **_QW, availability="needs_params", reason="thinking_default", thinking_default=True,
               price=_price("CNY", _QWEN, 12.0, 1.5, 36.0), note_zh=_NEEDS_PARAMS_ZH, note_en=_NEEDS_PARAMS_EN),
        _entry("dashscope/qwen3.8-flash", **_QW, availability="needs_params", reason="thinking_default", thinking_default=True,
               price=_price("CNY", _QWEN, 0.8, 0.1, 2.7), note_zh=_NEEDS_PARAMS_ZH, note_en=_NEEDS_PARAMS_EN),
    ],
    "glm": [
        _entry("zhipuai/glm-4.6", "zhipu", api_base="https://open.bigmodel.cn/api/paas/v4/", hosting="cn_mainland",
               trains="anonymized", availability="not_routable", reason="no_litellm_route", context_window=200000,
               price=_price("CNY", _GLM, note="not on the pricing page"),
               note_zh="模型库暂时连不上智谱（没有对应的调用路径）", note_en="this engine's model library has no route to Zhipu yet"),
    ],
    "kimi": [
        _entry("moonshot/kimi-k2.6", "moonshot", api_base="https://api.moonshot.cn/v1", hosting="cn_mainland", trains="yes",
               availability="not_routable", reason="base_not_applied", vision=False, context_window=262144,
               price=_price("CNY", _KIMI, 6.5, 1.1, 27.0),
               note_zh="模型库默认连海外地址（.ai），境内地址接上之前不能选", note_en="the library defaults to the overseas host (.ai); not selectable until the mainland host is applied"),
        _entry("moonshot/kimi-k2", "moonshot", api_base="https://api.moonshot.cn/v1", hosting="cn_mainland", trains="yes",
               status="retired", successor="moonshot/kimi-k2.6", offline_since="2026-05-25",
               price=_price("CNY", _KIMI, note="offline"), note_zh="已下线", note_en="offline"),
    ],
    "openai": [
        _entry("openai/gpt-5.4", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, 2.5, 0.25, 15.0), note_zh="境外模型，默认可用", note_en="overseas; available by default"),
        _entry("openai/gpt-5.4-mini", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=400000,
               price=_price("USD", _OPENAI, 0.75, 0.075, 4.5), note_zh="境外模型，默认可用", note_en="overseas; available by default"),
        _entry("openai/gpt-5.5", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外模型，默认可用", note_en="overseas; available by default"),
        _entry("openai/gpt-5.5-pro", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外高阶，默认可用", note_en="overseas higher tier; available by default"),
        _entry("openai/gpt-6-sol", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外 gpt-6，默认可用", note_en="overseas gpt-6; available by default"),
        _entry("openai/gpt-6-luna", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外 gpt-6，默认可用", note_en="overseas gpt-6; available by default"),
        _entry("openai/gpt-6-astra", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外 gpt-6，默认可用", note_en="overseas gpt-6; available by default"),
        _entry("openai/gpt-6.1-sol", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外 gpt-6.1，默认可用", note_en="overseas gpt-6.1; available by default"),
        _entry("openai/gpt-6.1-terra", "openai", api_base="https://api.openai.com/v1", hosting="overseas", trains="no",
               availability="available", context_window=1_050_000,
               price=_price("USD", _OPENAI, note="owner-enabled; price not recorded"), note_zh="境外 gpt-6.1，默认可用", note_en="overseas gpt-6.1; available by default"),
    ],
    "anthropic": [
        _entry("anthropic/claude-sonnet-5", "anthropic", api_base="https://api.anthropic.com", hosting="overseas", trains="no",
               availability="off_by_default", reason="overseas_vendor", context_window=1_000_000,
               price=_price("USD", _ANTHROPIC, 2.0, 0.2, 10.0), note_zh="境外模型，默认不可用", note_en="overseas; off by default"),
        _entry("anthropic/claude-sonnet-4-5", "anthropic", api_base="https://api.anthropic.com", hosting="overseas", trains="no",
               availability="off_by_default", reason="overseas_vendor", successor="anthropic/claude-sonnet-5",
               retires_not_before="2026-09-29", context_window=200000,
               price=_price("USD", _ANTHROPIC, note="not recorded"), note_zh="境外模型，默认不可用；2026-09-29 之后退役",
               note_en="overseas; off by default; retires no sooner than 2026-09-29"),
    ],
}

_REFUSAL_BY_AVAILABILITY = {
    "off_by_default": "model_overseas_off",
    "not_routable": "model_not_routable",
    "needs_params": "model_needs_params",
}


def entries() -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for provider in ENTRIES_BY_PROVIDER.values() for item in provider]


def entry(model: str) -> dict[str, Any] | None:
    chosen = str(model or "").strip()
    for provider in ENTRIES_BY_PROVIDER.values():
        for item in provider:
            if item["id"] == chosen:
                return copy.deepcopy(item)
    return None


def models_of(provider_id: str) -> list[str]:
    return [item["id"] for item in ENTRIES_BY_PROVIDER.get(provider_id, [])]


# Which parameters a ``needs_params`` entry is waiting for. The entry is held not because the model is bad but
# because running it needs a setting nobody could express before phase 1; supply that setting and the hold lifts.
PARAMS_BY_REASON: dict[str, tuple[str, ...]] = {
    "thinking_default_tool_loop": ("thinking",),
    "thinking_default": ("thinking",),
    "window_cap": ("window_cap",),
}


def _params_satisfy(reason: str, params: dict[str, Any]) -> str:
    """``""`` when these parameters lift the hold, otherwise the refusal code that stands instead."""
    wanted = PARAMS_BY_REASON.get(str(reason or ""), ())
    if not wanted:
        return "model_needs_params"
    for name in wanted:
        if name == "thinking":
            if "thinking" not in params:
                return "model_needs_params"
            # Thinking off raises no question: the tool-loop passback only arises when it is on. Thinking on is
            # allowed only once the owner's multi-turn tool trial has been recorded -- otherwise this product
            # would be asserting, on their behalf, that a thing nobody has run works.
            if params.get("thinking") and not str(params.get("tool_loop_trial", "") or "").strip():
                return "model_needs_trial"
        elif name == "window_cap":
            try:
                if int(params.get("window_cap", 0) or 0) <= 0:
                    return "model_needs_params"
            except (TypeError, ValueError):
                return "model_needs_params"
    return ""


def _refusal_decision(item, *, params=None, _availability_codes=None):
    """Share the original refusal decision without copying an entry's private metadata."""
    if item["status"] == "retired":
        return True, "model_retired"
    codes = _REFUSAL_BY_AVAILABILITY if _availability_codes is None else _availability_codes
    code = codes.get(item["availability"])
    if code == "model_needs_params" and params is not None:
        code = _params_satisfy(str(item["availability_reason"] or ""), dict(params))
    return False, code


def refusal(model: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Why ``model`` may not be newly chosen, or ``None``: ``model_unknown``, ``model_retired`` (with its successor),
    ``model_overseas_off``, ``model_not_routable``, ``model_needs_params`` or ``model_needs_trial``.

    ``params`` is what the run's frozen plan supplies for this model. With ``None`` -- which is every caller that
    has no plan in scope -- the answer is exactly what it was before phase 1. With a dict, and only for a
    ``needs_params`` entry, the hold lifts when the parameters that entry was waiting for are all there. Nothing
    else is affected: retired, overseas and not-routable are not things a parameter can answer.
    """
    item = entry(model)
    if item is None:
        return {"code": "model_unknown", "model": str(model or "")}
    retired, code = _refusal_decision(item, params=params)
    if retired:
        return {"code": "model_retired", "model": item["id"], "successor": item["successor"]}
    if code:
        return {"code": code, "model": item["id"], "reason": item["availability_reason"], "successor": item["successor"]}
    return None


def label(model: str) -> dict[str, Any] | None:
    """Display-only facts about ``model`` that are not a refusal: a vendor-routed alias and what serves it."""
    item = entry(model)
    if item is None or item["status"] != "vendor_routed_alias":
        return None
    return {"kind": "vendor_routed_alias", "successor": item["successor"], "successor_mode": item["successor_mode"]}


# This source observation is private, in-memory and not an execution or routing permission.
_PROJECTION_VERSION = "employee_instance_model_selection.v1"
_MAX_PROVIDERS = 500
_MAX_ROWS = 500
_MAX_ROW_KEYS = 64
_MAX_JSON_NODES = 50_000
_MAX_CANONICAL_BYTES = 2 * 1024 * 1024
_MAX_DEPTH = 64
_MAX_IDENTITY_NODES = 10_000
_CAPABILITY_KEYS = ("tools", "vision", "thinking_default", "reasoning_passback_required")
_STATUSES = ("active", "vendor_routed_alias", "retired")
_AVAILABILITIES = ("available", "off_by_default", "not_routable", "needs_params")
_REASONS = ("", "thinking_default_tool_loop", "thinking_default", "window_cap",
            "no_litellm_route", "base_not_applied", "overseas_vendor")
_OBSERVATION_CODES = ("model_catalogue_unavailable", "model_catalogue_changed", "candidate_projection_limit")
_FIXED_MESSAGES = {
    "model_catalogue_unavailable": "模型目录暂不可用。",
    "model_catalogue_changed": "模型目录已变化，请重新读取。",
    "candidate_projection_limit": "模型目录超出本次读取上限。",
}
_ORIGINAL_DECISION = _refusal_decision
_ORIGINAL_DECISION_CODE = _refusal_decision.__code__
_ORIGINAL_ENTRY = entry
_ORIGINAL_ENTRY_CODE = entry.__code__
_ORIGINAL_REFUSAL = refusal
_ORIGINAL_REFUSAL_CODE = refusal.__code__
_ORIGINAL_POLICY = tuple(sorted(_REFUSAL_BY_AVAILABILITY.items()))


class EmployeeModelCatalogueError(ValueError):
    """Only a fixed public code and message; no source values or exception details."""

    def __init__(self, code):
        safe = code if type(code) is str and code in _OBSERVATION_CODES else "model_catalogue_unavailable"
        self.code = safe
        super().__init__(_FIXED_MESSAGES[safe])


@dataclass(frozen=True, slots=True, repr=False)
class EmployeeModelCatalogueObservation:
    projection_version: str
    revision: str
    projection_bytes: bytes
    _identity: tuple


def _observation_fail(code="model_catalogue_unavailable"):
    raise EmployeeModelCatalogueError(code)


def _choice_text(value, *, empty=False):
    if type(value) is not str:
        _observation_fail()
    size = len(value)
    if (not empty and size == 0) or size > 256:
        _observation_fail()
    if value != value.strip() or any(ord(ch) < 32 or 127 <= ord(ch) <= 159 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        _observation_fail()
    if len(value.encode("utf-8")) > 256:
        _observation_fail()
    return value


def _choice_fields(value, wanted, cap):
    """Read exact builtin keys first. Ignored values are never inspected or copied."""
    if type(value) is not dict:
        _observation_fail()
    initial = len(value)
    if initial > cap:
        _observation_fail("candidate_projection_limit")
    output = {}
    count = 0
    for key, item in dict.items(value):
        if count >= cap:
            _observation_fail("candidate_projection_limit")
        if count >= initial:
            _observation_fail("model_catalogue_changed")
        key = _choice_text(key)
        if key in wanted:
            output[key] = item
        count += 1
    if len(value) != initial or count != initial:
        _observation_fail("model_catalogue_changed")
    if len(output) != len(wanted):
        _observation_fail()
    return output


def _choice_policy():
    policy = _REFUSAL_BY_AVAILABILITY
    fields = _choice_fields(policy, tuple(key for key, _ in _ORIGINAL_POLICY), len(_ORIGINAL_POLICY))
    pairs = tuple(sorted((key, _choice_text(value)) for key, value in fields.items()))
    if pairs != _ORIGINAL_POLICY:
        _observation_fail("model_catalogue_changed")
    helper = _refusal_decision
    if type(helper) is not FunctionType or helper is not _ORIGINAL_DECISION or helper.__code__ is not _ORIGINAL_DECISION_CODE:
        _observation_fail("model_catalogue_changed")
    current_entry = entry
    current_refusal = refusal
    if (type(current_entry) is not FunctionType or current_entry is not _ORIGINAL_ENTRY or
            current_entry.__code__ is not _ORIGINAL_ENTRY_CODE or type(current_refusal) is not FunctionType or
            current_refusal is not _ORIGINAL_REFUSAL or current_refusal.__code__ is not _ORIGINAL_REFUSAL_CODE):
        _observation_fail("model_catalogue_changed")
    return policy, helper, pairs, fields


class _ChoiceBudget:
    def __init__(self):
        self.nodes = 0
        self.bytes = 0

    def account(self, value, depth=0):
        if depth > _MAX_DEPTH:
            _observation_fail("candidate_projection_limit")
        self.nodes += 1
        if self.nodes > _MAX_JSON_NODES:
            _observation_fail("candidate_projection_limit")
        if type(value) is str:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            size = len(encoded)
        elif type(value) is bool:
            size = 4 if value else 5
        elif type(value) is list:
            size = 2 + max(0, len(value) - 1)
            self._bytes(size)
            for item in value:
                self.account(item, depth + 1)
            return
        elif type(value) is dict:
            size = 2 + max(0, len(value) - 1) + len(value)
            self._bytes(size)
            for key, item in value.items():
                self.account(key, depth + 1)
                self.account(item, depth + 1)
            return
        else:
            _observation_fail()
        self._bytes(size)

    def _bytes(self, size):
        self.bytes += size
        if self.bytes > _MAX_CANONICAL_BYTES:
            _observation_fail("candidate_projection_limit")


def _choice_pass(budget):
    table = ENTRIES_BY_PROVIDER
    if type(table) is not dict:
        _observation_fail()
    initial = len(table)
    if initial > _MAX_PROVIDERS:
        _observation_fail("candidate_projection_limit")
    policy, helper, pairs, codes = _choice_policy()
    rows = []
    providers = []
    seen = set()
    held = {}
    count = 0
    row_count = 0
    for raw_provider, items in dict.items(table):
        if count >= _MAX_PROVIDERS:
            _observation_fail("candidate_projection_limit")
        if count >= initial:
            _observation_fail("model_catalogue_changed")
        provider = _choice_text(raw_provider)
        if type(items) is not list:
            _observation_fail()
        length = len(items)
        row_count += length
        if row_count > _MAX_ROWS:
            _observation_fail("candidate_projection_limit")
        identities = []
        held_rows = []
        for index in range(length):
            if index >= _MAX_ROWS:
                _observation_fail("candidate_projection_limit")
            if len(items) != length:
                _observation_fail("model_catalogue_changed")
            raw = items[index]
            fields = _choice_fields(raw, ("id", "status", "availability", "availability_reason", "capabilities"), _MAX_ROW_KEYS)
            model = _choice_text(fields["id"])
            status = _choice_text(fields["status"])
            availability = _choice_text(fields["availability"])
            reason = _choice_text(fields["availability_reason"], empty=True)
            if status not in _STATUSES or availability not in _AVAILABILITIES or reason not in _REASONS or model in seen:
                _observation_fail()
            seen.add(model)
            raw_caps = fields["capabilities"]
            caps = _choice_fields(raw_caps, _CAPABILITY_KEYS, len(_CAPABILITY_KEYS))
            if any(type(value) is not bool for value in caps.values()):
                _observation_fail()
            decision_item = {"status": status, "availability": availability, "availability_reason": reason}
            _, code = helper(decision_item, params=None, _availability_codes=codes)
            if code is not None and (type(code) is not str or code not in tuple(value for _, value in pairs) + ("model_retired",)):
                _observation_fail()
            rows.append({"provider": provider, "model": model, "status": status, "availability": availability,
                         "availability_reason": reason, "capabilities": caps, "selectable": code is None,
                         "gaps": [] if code is None else [code]})
            identities.append((id(raw), id(raw_caps), len(raw)))
            held_rows.append((raw, raw_caps, len(raw), model, status, availability, reason, dict(caps)))
        if len(items) != length:
            _observation_fail("model_catalogue_changed")
        providers.append((provider, id(items), tuple(identities)))
        held[provider] = (items, tuple(held_rows))
        count += 1
    if count != initial or len(table) != initial or ENTRIES_BY_PROVIDER is not table:
        _observation_fail("model_catalogue_changed")
    current_policy, current_helper, current_pairs, _ = _choice_policy()
    if current_policy is not policy or current_helper is not helper or current_pairs != pairs:
        _observation_fail("model_catalogue_changed")
    _choice_held(table, initial, held)
    rows.sort(key=lambda item: (item["provider"], item["model"]))
    projection = {"projection_version": _PROJECTION_VERSION, "candidates": rows}
    before = budget.bytes
    budget.account(projection)
    payload = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) != budget.bytes - before:
        _observation_fail()
    final_policy, final_helper, final_pairs, _ = _choice_policy()
    if final_policy is not policy or final_helper is not helper or final_pairs != pairs:
        _observation_fail("model_catalogue_changed")
    token = (id(table), id(policy), id(helper), id(_ORIGINAL_DECISION_CODE), id(_ORIGINAL_ENTRY),
             id(_ORIGINAL_ENTRY_CODE), id(_ORIGINAL_REFUSAL), id(_ORIGINAL_REFUSAL_CODE), pairs, tuple(providers))
    _choice_identity(token)
    return payload, token


def _choice_held(table, initial, held):
    """Bounded end-of-pass identity/value check; no ignored metadata value is examined."""
    count = 0
    for key, items in dict.items(table):
        if count >= _MAX_PROVIDERS:
            _observation_fail("candidate_projection_limit")
        if count >= initial:
            _observation_fail("model_catalogue_changed")
        key = _choice_text(key)
        if key not in held:
            _observation_fail("model_catalogue_changed")
        old_items, old_rows = held[key]
        if items is not old_items or len(items) != len(old_rows):
            _observation_fail("model_catalogue_changed")
        for index in range(len(old_rows)):
            raw, raw_caps, raw_length, model, status, availability, reason, caps = old_rows[index]
            if items[index] is not raw or len(raw) != raw_length:
                _observation_fail("model_catalogue_changed")
            current = _choice_fields(raw, ("id", "status", "availability", "availability_reason", "capabilities"), _MAX_ROW_KEYS)
            values = (_choice_text(current["id"]), _choice_text(current["status"]),
                      _choice_text(current["availability"]), _choice_text(current["availability_reason"], empty=True))
            if values != (model, status, availability, reason) or current["capabilities"] is not raw_caps:
                _observation_fail("model_catalogue_changed")
            current_caps = _choice_fields(raw_caps, _CAPABILITY_KEYS, len(_CAPABILITY_KEYS))
            if any(type(value) is not bool for value in current_caps.values()):
                _observation_fail()
            if current_caps != caps:
                _observation_fail("model_catalogue_changed")
        if len(items) != len(old_rows):
            _observation_fail("model_catalogue_changed")
        count += 1
    if count != initial or len(table) != initial or ENTRIES_BY_PROVIDER is not table:
        _observation_fail("model_catalogue_changed")


def _choice_identity(value):
    """Validate only a fixed shape, with initial tuple lengths before every traversal."""
    nodes = 0

    def node(item):
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_IDENTITY_NODES:
            _observation_fail("candidate_projection_limit")
        return item

    def pointer(item):
        node(item)
        if type(item) is not int or item <= 0 or item.bit_length() > 64:
            _observation_fail()

    node(value)
    if type(value) is not tuple or len(value) != 10:
        _observation_fail()
    for index in range(8):
        pointer(value[index])
    pairs = node(value[8])
    if type(pairs) is not tuple or len(pairs) != len(_ORIGINAL_POLICY):
        _observation_fail()
    for pair in pairs:
        node(pair)
        if type(pair) is not tuple or len(pair) != 2:
            _observation_fail()
        for text in pair:
            _choice_text(node(text))
    if pairs != _ORIGINAL_POLICY:
        _observation_fail()
    providers = node(value[9])
    if type(providers) is not tuple:
        _observation_fail()
    if len(providers) > _MAX_PROVIDERS:
        _observation_fail("candidate_projection_limit")
    rows = 0
    for provider in providers:
        node(provider)
        if type(provider) is not tuple or len(provider) != 3:
            _observation_fail()
        _choice_text(node(provider[0]))
        pointer(provider[1])
        identities = node(provider[2])
        if type(identities) is not tuple:
            _observation_fail()
        rows += len(identities)
        if rows > _MAX_ROWS:
            _observation_fail("candidate_projection_limit")
        for identity in identities:
            node(identity)
            if type(identity) is not tuple or len(identity) != 3:
                _observation_fail()
            pointer(identity[0])
            pointer(identity[1])
            size = node(identity[2])
            if type(size) is not int or not 0 < size <= _MAX_ROW_KEYS:
                _observation_fail()


def _observe_choices():
    budget = _ChoiceBudget()
    first_bytes, first_token = _choice_pass(budget)
    second_bytes, second_token = _choice_pass(budget)
    if first_bytes != second_bytes or first_token != second_token:
        _observation_fail("model_catalogue_changed")
    return EmployeeModelCatalogueObservation(_PROJECTION_VERSION, hashlib.sha256(first_bytes).hexdigest(), first_bytes, first_token)


def observe_employee_model_choices() -> EmployeeModelCatalogueObservation:
    code = None
    try:
        return _observe_choices()
    except EmployeeModelCatalogueError as error:
        code = error.code if type(error) is EmployeeModelCatalogueError else "model_catalogue_unavailable"
    except Exception:
        code = "model_catalogue_unavailable"
    raise EmployeeModelCatalogueError(code)


def recheck_employee_model_choices(observation: EmployeeModelCatalogueObservation) -> None:
    code = None
    try:
        if type(observation) is not EmployeeModelCatalogueObservation:
            _observation_fail()
        if type(observation.projection_version) is not str or observation.projection_version != _PROJECTION_VERSION:
            _observation_fail()
        revision = observation.revision
        if type(revision) is not str or len(revision) != 64 or any(ch not in "0123456789abcdef" for ch in revision):
            _observation_fail()
        payload = observation.projection_bytes
        if type(payload) is not bytes:
            _observation_fail()
        if len(payload) > _MAX_CANONICAL_BYTES:
            _observation_fail("candidate_projection_limit")
        _choice_identity(observation._identity)
        if hashlib.sha256(payload).hexdigest() != revision:
            _observation_fail()
        current = _observe_choices()
        if (current.projection_version != observation.projection_version or current.revision != revision or
                current.projection_bytes != payload or current._identity != observation._identity):
            _observation_fail("model_catalogue_changed")
        return None
    except EmployeeModelCatalogueError as error:
        code = error.code if type(error) is EmployeeModelCatalogueError else "model_catalogue_unavailable"
    except Exception:
        code = "model_catalogue_unavailable"
    raise EmployeeModelCatalogueError(code)
