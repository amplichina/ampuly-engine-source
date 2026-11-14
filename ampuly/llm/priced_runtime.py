"""Host-bound CNY tariffs for the actual LLMProvider transport boundary.

Rates and hard token ceilings are deployment-reviewed billing contracts. They
are never inferred from task metadata, LiteLLM USD estimates or customer text.
The ledger records rated usage under that contract, not a supplier invoice.
Unknown usage retains the maximum hold. No schema migration is required.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import threading
import uuid
import weakref
from urllib.parse import urlsplit

from ampuly.llm.budget import ModelBudgetStop
from ampuly.core.model_visual_tariff import (
    CNYVisualInputConfig, checked_visual_contract, checked_visual_content,
)

SCALE = 1_000_000
MAX_UNITS = 2**53 - 1
_BINDINGS = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()


def _stop(message):
    raise ModelBudgetStop(message)


def _integer(value, maximum=MAX_UNITS, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= maximum:
        _stop("Invalid server pricing contract")
    return value


def _text(value):
    if (type(value) is not str or not 0 < len(value) <= 256 or value != value.strip()
            or any(ord(c) < 32 for c in value)):
        _stop("Invalid server pricing identity")
    return value


def _ceil(value):
    return -(-value.numerator // value.denominator)


def _amount(units):
    exact = Fraction(units, SCALE)
    value = float(exact)
    if Fraction.from_float(value) < exact:
        value = math.nextafter(value, math.inf)
    return value


@dataclass(frozen=True)
class ServerTextTariff:
    """One exact model/account contract; no endpoint/model wildcards.

    input_token_limit is the reviewed supplier's complete billed-input hard
    limit (including tools/framing), not a tokenizer estimate. Output/reasoning
    must share max_tokens. Unsupported modalities and extra paid features are
    refused. Rates are integer micro-CNY per million billed tokens. CNY rates
    may incorporate a fixed reviewed exchange rate; no live FX is guessed.
    """
    policy_id: str
    revision: str
    model: str
    endpoint: str
    billing_binding_id: str
    billing_epoch: str
    input_token_limit: int
    output_token_limit: int
    input_micro_cny_per_million: int
    output_micro_cny_per_million: int
    fixed_fee_micro_cny: int = 0
    served_models: tuple[str, ...] = ()
    visual_input: CNYVisualInputConfig | None = None

    def __post_init__(self):
        for key in ("policy_id", "revision", "model", "endpoint", "billing_binding_id", "billing_epoch"):
            _text(getattr(self, key))
        endpoint = urlsplit(self.endpoint)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            _stop("A priced provider requires an explicit reviewed API endpoint")
        if type(self.served_models) is not tuple or len(self.served_models) > 16:
            _stop("Invalid served-model pricing aliases")
        for model in self.served_models:
            _text(model)
        _integer(self.input_token_limit, 10_000_000, positive=True)
        _integer(self.output_token_limit, 1_000_000, positive=True)
        _integer(self.input_micro_cny_per_million, 10**15)
        _integer(self.output_micro_cny_per_million, 10**15)
        _integer(self.fixed_fee_micro_cny)
        _integer(self.charge(self.input_token_limit, self.output_token_limit), positive=True)
        try:
            object.__setattr__(self, "visual_input", checked_visual_contract(self.visual_input))
        except ValueError:
            _stop("Invalid reviewed visual billing contract")

    def charge(self, inputs, outputs):
        return _ceil(Fraction(inputs * self.input_micro_cny_per_million
                              + outputs * self.output_micro_cny_per_million, SCALE)) + self.fixed_fee_micro_cny


@dataclass(frozen=True)
class _Binding:
    tariff: ServerTextTariff
    credential_digest: str
    generation: str


def _credential_digest(value):
    if value is not None and type(value) is not str:
        _stop("Invalid provider credential binding")
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def bind_model_pricing(provider, tariff: ServerTextTariff) -> None:
    """Called by trusted server composition, never exposed as a wire action.

    Rebinding/revocation invalidates already reserved requests. Sibling model
    providers do not inherit a tariff for another model/account implicitly.
    """
    from ampuly.llm.provider import LLMProvider
    if not isinstance(provider, LLMProvider) or type(tariff) is not ServerTextTariff:
        _stop("Server pricing binding requires a provider and reviewed tariff")
    endpoint = provider._api_base or "default"
    if endpoint != tariff.endpoint or provider.config.default_model != tariff.model:
        _stop("Pricing contract does not match the provider route")
    credential = provider._call_api_key()
    if type(credential) is not str or not credential.strip():
        _stop("A priced provider requires an explicit billing credential")
    binding = _Binding(tariff, _credential_digest(credential), uuid.uuid4().hex)
    with _LOCK:
        _BINDINGS[provider] = binding


def revoke_model_pricing(provider) -> None:
    with _LOCK:
        _BINDINGS.pop(provider, None)


def _binding(provider):
    with _LOCK:
        value = _BINDINGS.get(provider)
    if type(value) is not _Binding:
        _stop("No trusted CNY tariff is bound to this provider")
    from ampuly.llm.server_tariff_binding import verify_server_text_tariff
    verify_server_text_tariff(provider, value.tariff)
    return value


def inherit_matching_model_pricing(source, target) -> bool:
    """Copy only an already reviewed exact model/account into a fixed sibling."""
    with _LOCK:
        exists = source in _BINDINGS
    if not exists:
        return False
    value = _binding(source)
    if (target.config.default_model != value.tariff.model
            or (target._api_base or "default") != value.tariff.endpoint
            or _credential_digest(target._call_api_key()) != value.credential_digest):
        return False
    bind_model_pricing(target, value.tariff)
    return True


@dataclass(frozen=True)
class _EmployeeCostScope:
    run_id: str
    step_task_id: str
    attempt: int
    maximum_units: int
    series_key: str


def _employee_cost_scope(scope):
    from ampuly.layer3_agent.employee_runtime import current_employee_turn
    turn = current_employee_turn()
    if turn is None:
        return None
    identity = turn.basis.identity
    if (scope is None or identity.project_id != scope.project_id
            or identity.order_id != scope.order_id):
        _stop("Employee monetary scope does not match its order budget")
    pin = turn.basis.pin()
    value = pin["effective_limits"].get("max_cost_cny")
    if type(value) is not str or not 1 <= len(value) <= 64:
        _stop("Employee monetary ceiling is invalid")
    try:
        ceiling = Fraction(value) * SCALE
        units = ceiling.numerator // ceiling.denominator
    except (ValueError, ZeroDivisionError):
        _stop("Employee monetary ceiling is invalid")
    if not 0 <= units <= MAX_UNITS or type(identity.attempt) is not int or identity.attempt < 1:
        _stop("Employee monetary ceiling is invalid")
    series = [identity.project_id, identity.order_id, identity.step_task_id,
              _text(pin["instance_id"]), _text(turn.basis.pins_fingerprint)]
    series_key = hashlib.sha256(json.dumps(series, separators=(",", ":")).encode()).hexdigest()
    return _EmployeeCostScope(_text(identity.run_id), _text(identity.step_task_id), identity.attempt, units, series_key)


def _freeze_call(call, binding):
    if type(call) is not dict:
        _stop("Prepared model request is invalid")
    try:
        visual = checked_visual_contract(binding.tariff.visual_input)
    except ValueError:
        _stop("Invalid reviewed visual billing contract")
    try:
        raw = json.dumps(call, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > (visual.max_request_bytes if visual is not None else 1024 * 1024):
            _stop("Prepared model request is too large")
        frozen = json.loads(raw)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _stop("Prepared model request cannot be priced")
    allowed = {"model", "messages", "temperature", "max_tokens", "num_retries", "timeout",
               "api_key", "api_base", "stream", "stream_options", "tools", "tool_choice", "response_format"}
    if set(frozen) - allowed:
        _stop("Prepared model request uses an unpriced feature")
    tariff = binding.tariff
    if frozen.get("model") != tariff.model or frozen.get("api_base", "default") != tariff.endpoint:
        _stop("Prepared model request changed its priced route")
    if _credential_digest(frozen.get("api_key")) != binding.credential_digest:
        _stop("Prepared model request changed its billing identity")
    _integer(frozen.get("max_tokens"), tariff.output_token_limit, positive=True)
    if type(frozen.get("num_retries")) is not int or frozen["num_retries"] != 0:
        _stop("Priced transport retries must be disabled")
    messages = frozen.get("messages")
    if type(messages) is not list or not 1 <= len(messages) <= 1024:
        _stop("Priced text messages are invalid")
    image_count = 0
    for message in messages:
        if type(message) is not dict or type(message.get("role")) is not str:
            _stop("Priced text messages are invalid")
        if (message["role"] not in {"system", "developer", "user", "assistant", "tool", "function"}
                or set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "function_call"}):
            _stop("Message contains an unpriced feature")
        if message.get("content") is not None and type(message.get("content")) is not str:
            if visual is None:
                _stop("Multimodal inputs require their own reviewed tariff")
            try:
                image_count += checked_visual_content(message, visual)
            except ValueError:
                _stop("Prepared model request has unsupported visual input")
            if image_count > visual.max_images_per_call:
                _stop("Prepared model request exceeds reviewed image count")
    if "tools" in frozen:
        tools = frozen["tools"]
        if type(tools) is not list or len(tools) > 128:
            _stop("Priced function tools are invalid")
        for tool in tools:
            if (type(tool) is not dict or set(tool) != {"type", "function"}
                    or tool["type"] != "function" or type(tool["function"]) is not dict
                    or set(tool["function"]) - {"name", "description", "parameters", "strict"}
                    or type(tool["function"].get("name")) is not str):
                _stop("Built-in paid tools require their own reviewed tariff")
    if "stream_options" in frozen and frozen["stream_options"] != {"include_usage": True}:
        _stop("Unpriced stream option")
    if "response_format" in frozen:
        response_format = frozen["response_format"]
        if (type(response_format) is not dict
                or response_format.get("type") not in {"text", "json_object", "json_schema"}
                or set(response_format) - {"type", "json_schema"}):
            _stop("Unpriced response format")
    return frozen


class PricedRuntimeGroup:
    money_priced = True

    def __init__(self, business, scope, provider, call, retrying):
        self.business, self.scope, self.provider = business, scope, provider
        self.employee = _employee_cost_scope(scope)
        self.binding = _binding(provider)
        self.prepared_call = _freeze_call(call, self.binding)
        self.prepared_fingerprint = hashlib.sha256(json.dumps(self.prepared_call, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        tariff = self.binding.tariff
        self.maximum_units = tariff.charge(tariff.input_token_limit, self.prepared_call["max_tokens"])
        _integer(self.maximum_units, positive=True)
        public_call = {key: value for key, value in self.prepared_call.items() if key != "api_key"}
        self.call_id = uuid.uuid4().hex
        self.quote = dict(policy_id=tariff.policy_id, revision=tariff.revision, model=tariff.model,
                          billing_binding_id=tariff.billing_binding_id, billing_epoch=tariff.billing_epoch,
                          maximum_micro_cny=self.maximum_units, call_id=self.call_id,
                          input_token_limit=tariff.input_token_limit,
                          output_token_limit=self.prepared_call["max_tokens"],
                          input_micro_cny_per_million=tariff.input_micro_cny_per_million,
                          output_micro_cny_per_million=tariff.output_micro_cny_per_million,
                          fixed_fee_micro_cny=tariff.fixed_fee_micro_cny,
                          request_sha256=hashlib.sha256(json.dumps(public_call, sort_keys=True,
                              ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
        if self.employee is not None:
            self.quote["employee_run_id"] = self.employee.run_id
            self.quote["employee_step_task_id"] = self.employee.step_task_id
            self.quote["employee_attempt"] = self.employee.attempt
            self.quote["employee_maximum_micro_cny"] = self.employee.maximum_units
            self.quote["employee_cost_series"] = self.employee.series_key
        self.note = "priced-runtime.v1:" + json.dumps(self.quote, sort_keys=True, separators=(",", ":"))
        self.reservations = []
        self.claimed = False
        self.usage = None
        self.usage_invalid = False
        with business._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            limits = self._budget(conn)
            maximum_amount = _amount(self.maximum_units)
            # Integer micro-CNY accounting is conservative across the legacy
            # REAL ledger and applies in the same write transaction.
            history = conn.execute("SELECT amount,settled_amount,state FROM budget_reservations WHERE budget_id=? AND kind='cost_cny' LIMIT 10001", (scope.budget_id,)).fetchall()
            if len(history) > 10000:
                _stop("Budget history is too large")
            used = 0
            for item in history:
                if item["state"] != "released":
                    value = item["settled_amount"] if item["state"] == "settled" else item["amount"]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        _stop("Budget history is invalid")
                    used += _ceil(Fraction(str(value)) * SCALE)
            extra = _ceil(Fraction(str(maximum_amount)) * SCALE)
            if "cost_cny" in limits:
                cap = Fraction(str(limits["cost_cny"])) * SCALE
                if used + extra > cap.numerator // cap.denominator:
                    _stop("Monetary budget exhausted before model dispatch")
            self._employee_usage(conn, extra=extra)
            kinds = [("model_call", 1)] + ([("retry", 1)] if retrying else []) + [("cost_cny", maximum_amount)]
            for kind, amount in kinds:
                rid, existing = business._reserve_budget_owned_on_connection(conn, scope.budget_id,
                    kind=kind, amount=amount, actor="runtime", idempotency_key=f"priced-{self.call_id}-{kind}",
                    expected_main=business.path.absolute(), note=self.note)
                if existing:
                    _stop("Retained reservations are not fresh dispatch permissions")
                self.reservations.append((rid, kind, amount))

    def _budget(self, conn):
        row = conn.execute("SELECT * FROM run_budgets WHERE budget_id=?", (self.scope.budget_id,)).fetchone()
        if row is None or any(str(row[key] or "") != getattr(self.scope, key)
                              for key in ("project_id", "customer_id", "environment_id", "order_id")):
            _stop("Model budget scope mismatch")
        if row["state"] != "open" or row["health"] != "ok":
            _stop("Model budget is closed or held for verification")
        limits = self.business._budget_limits(row)
        if "cost_cny" not in limits and self.employee is None:
            _stop("Monetary budget disappeared before dispatch")
        return limits

    def _employee_usage(self, conn, *, extra=0):
        if self.employee is None:
            return
        if _employee_cost_scope(self.scope) != self.employee:
            _stop("Employee monetary ceiling changed before dispatch")
        # Scope to the order but include replaced order budgets: a new budget
        # is not permission to reset an existing employee attempt's spending.
        rows = conn.execute("SELECT r.amount,r.settled_amount,r.state,r.note FROM budget_reservations r "
            "JOIN run_budgets b ON r.budget_id=b.budget_id WHERE r.kind='cost_cny' "
            "AND b.project_id=? AND b.customer_id=? AND b.environment_id=? AND b.order_id=? LIMIT 10001",
            (self.scope.project_id, self.scope.customer_id, self.scope.environment_id, self.scope.order_id)).fetchall()
        if len(rows) > 10000:
            _stop("Employee budget history is too large")
        total = 0
        for row in rows:
            note = row["note"]
            if type(note) is not str or len(note) > 4096:
                _stop("Employee budget attribution is invalid")
            marker = "priced-runtime.v1:"
            if marker not in note:
                continue
            try:
                attribution = json.loads(note.split(marker, 1)[1])
                if type(attribution) is not dict:
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                _stop("Employee budget attribution is invalid")
            if attribution.get("employee_cost_series") != self.employee.series_key:
                continue
            if attribution.get("employee_step_task_id") != self.employee.step_task_id:
                _stop("Employee budget identity is inconsistent")
            if row["state"] == "released":
                continue
            value = row["settled_amount"] if row["state"] == "settled" else row["amount"]
            if (row["state"] not in {"held", "settled", "unknown"}
                    or type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                _stop("Employee budget history is invalid")
            total += _ceil(Fraction(str(value)) * SCALE)
        if total + extra > self.employee.maximum_units:
            _stop("Employee monetary ceiling exhausted before model dispatch")

    def validate_call(self, actual):
        if _binding(self.provider) is not self.binding:
            _stop("Pricing binding changed before model dispatch")
        try:
            fingerprint = hashlib.sha256(json.dumps(actual, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, RecursionError, UnicodeError):
            _stop("Prepared model request changed after reservation")
        if fingerprint != self.prepared_fingerprint:
            _stop("Prepared model request changed after reservation")

    def claim(self):
        if self.claimed or _binding(self.provider) is not self.binding:
            _stop("Pricing binding changed before model dispatch")
        if (_credential_digest(self.provider._call_api_key()) != self.binding.credential_digest
                or (self.provider._api_base or "default") != self.binding.tariff.endpoint):
            _stop("Provider billing identity changed before model dispatch")
        with self.business._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            limits = self._budget(conn)
            self._employee_usage(conn)
            rows = conn.execute("SELECT kind,amount,settled_amount,state FROM budget_reservations WHERE budget_id=? LIMIT 10001", (self.scope.budget_id,)).fetchall()
            _, _, used = self.business._budget_usage(rows)
            available = self.business._budget_available(limits, used)
            if any(value is not None and value < 0 for value in available.values()):
                _stop("Budget ceiling was lowered before model dispatch")
            for rid, _, _ in self.reservations:
                cursor = conn.execute("UPDATE budget_reservations SET note=? WHERE reservation_id=? AND budget_id=? AND state='held' AND note=?",
                                      ("dispatch:" + self.note, rid, self.scope.budget_id, self.note))
                if cursor.rowcount != 1:
                    _stop("Priced request already claimed or changed")
        self.claimed = True

    def observe(self, *, model, prompt_tokens, completion_tokens):
        tariff = self.binding.tariff
        if self.usage_invalid:
            return
        if (model not in (tariff.served_models or (tariff.model,)) or type(prompt_tokens) is not int or type(completion_tokens) is not int
                or not 0 <= prompt_tokens <= tariff.input_token_limit
                or not 0 <= completion_tokens <= self.prepared_call["max_tokens"]):
            self.usage = None
            self.usage_invalid = True
            return
        candidate = (prompt_tokens, completion_tokens)
        if self.usage is not None and any(a < b for a, b in zip(candidate, self.usage)):
            self.usage = None
            self.usage_invalid = True
            return
        self.usage = candidate

    def _finish(self, state, *, reason="", actual=None):
        with self.business._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for rid, kind, amount in self.reservations:
                row = conn.execute("SELECT state,settled_amount FROM budget_reservations WHERE reservation_id=? AND budget_id=?", (rid, self.scope.budget_id)).fetchone()
                if row is None:
                    _stop("Priced reservation disappeared")
                charged = actual if kind == "cost_cny" else amount
                if row["state"] == state:
                    if state == "settled" and row["settled_amount"] != charged:
                        _stop("Conflicting priced settlement")
                    continue
                if row["state"] != "held":
                    _stop("Priced reservation changed before settlement")
                self.business._transition_reservation(conn, rid, expected="held", to_state=state,
                    settled_amount=charged if state == "settled" else None,
                    note=(reason + ":" + self.note)[:4096])
            if state == "unknown":
                conn.execute("UPDATE run_budgets SET health='pending_verification',state_reason='Priced model charge unknown' WHERE budget_id=?", (self.scope.budget_id,))

    def settle(self):
        if not self.claimed or self.usage is None:
            self.unknown("model usage is not attributable to the priced request")
            _stop("Model charge uncertain; maximum amount held for verification")
        units = self.binding.tariff.charge(*self.usage)
        if units > self.maximum_units:
            self.unknown("usage exceeded the reviewed pricing bound")
            _stop("Model charge exceeded its bound; verification required")
        self._finish("settled", reason="rated-usage-cny-not-supplier-invoice", actual=_amount(units))

    def release(self, reason=""):
        self._finish("released", reason=reason)

    def unknown(self, reason=""):
        self._finish("unknown", reason=reason)
