"""PULY: the console's judgment provider (4.7 Q04-C), answered by TypeSafe's JEV model.

A judgment is a question about a state whose answer is typed, not written: yes or no (``noul``), one of a list
(``choice``), or a level on a rubric (``score``), each with the probabilities it came from. Nothing is generated, so a
judgment costs its input and next to no output -- why it is worth having for recognising, sorting and dispatching at
volume.

PULY is the product name for this integration. JEV answers through TypeSafe's HTTP API;
the receipt identifies the provider, model and responding version.

Integration requirements:
- The key comes from the environment (``JEV_API_KEY``, set in 配置 → 模型与密钥) and goes into one header. It is never
  logged, returned or quoted in an error.
- Each request is a model call on the order's allowance when one is bound (``request_budget``): reserved before it is
  sent, settled when an answer or a definite refusal comes back, held for verification when how it ended is unknown --
  and then raised, as for every model call (a budget stop is the owner's to act on, not an answer).
- Retried only when the service said it did not take the request (429 rate limited, 529 overloaded): twice at most,
  each retry reserved as a retry. A timeout, a dropped connection or a server error is not retried: it may have been
  answered and billed.
- Nothing here decides for anybody. ``decided`` gives an answer only when one came back and cleared the caller's own
  threshold (there is no house threshold: it is set on the caller's data); refused, busy, unknown and below-threshold
  are all ``None``, and the caller asks a person. A failure is never read as "no objection".
- The request is checked before it is sent, against the service's own limits (at most 255 options, 2 to 10 rubric
  levels); a state too large is refused, never cut.

The state is sent to TypeSafe's service. What a caller puts in it leaves this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from typing import Any, Awaitable, Callable, Mapping

import httpx
from loguru import logger

from ampuly.llm.budget import CallNotCharged, mark_dispatched, model_retry_scope, request_budget

PULY_NAME = "PULY"
JEV_BACKEND = "JEV · TypeSafe"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_KEY_ENV = "JEV_API_KEY"
JEV_MODEL = "jev-latest"
# The id the settings page tests the connection under (a judgment model is not a chat model: litellm never sees it).
PULY_TEST_MODEL = "puly/jev-latest"

QUESTION_TYPES = ("noul", "choice", "score")
MAX_QUESTIONS = 32
MAX_CHOICE_OPTIONS = 255
SCORE_LEVELS = (2, 10)
MAX_STATE_BYTES = 64 * 1024
MAX_BODY_BYTES = 256 * 1024
MAX_QUESTION_ID = 64
MAX_RETRIES = 2
MAX_RETRY_WAIT_S = 10.0
DEFAULT_TIMEOUT_S = 20.0

# The service answered "not taken, try later": nothing was judged, so asking again is safe.
_RETRYABLE = frozenset({429, 529})
# The service answered "no" about the request itself: nothing was judged and asking again would get the same.
_REFUSED = frozenset({401, 403})
_REJECTED = frozenset({400, 404, 413, 422})

OUTCOMES = ("answered", "not_configured", "refused", "rejected", "busy", "unreachable", "failed", "unknown")


class PulyRequestError(ValueError):
    """The request is not one the service can answer. Raised before anything is sent or reserved."""


class _NotSent(CallNotCharged):
    """The request never left this machine: a definite "not sent" -- the hold is given back."""


class _AnsweredNo(CallNotCharged):
    """TypeSafe answered with a status that means the work was not done (busy, refused, rejected): the hold is given
    back, and the response is kept for the receipt and the retry rule."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _Uncertain(RuntimeError):
    """A response that does not say whether the question was answered (a server error): held, never retried."""


def _key() -> str:
    from ampuly.plugins.office_ui.settings_service import configured_key

    return configured_key(JEV_KEY_ENV)


def configured() -> bool:
    return bool(_key())


# ── The request ─────────────────────────────────────────────────────────────


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (dict, list)) and bool(value)


def check_request(state: Any, questions: Mapping[str, Any]) -> tuple[Any, dict[str, dict[str, Any]]]:
    """The state and questions as they will be sent, or ``PulyRequestError`` saying what is wrong with them."""
    if not isinstance(state, (str, dict, list)):
        raise PulyRequestError("state must be text, an object or a list")
    if not _nonempty(state):
        raise PulyRequestError("state is empty: there is nothing to judge")
    try:
        size = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PulyRequestError(f"state cannot be sent as JSON: {type(exc).__name__}") from None
    if size > MAX_STATE_BYTES:
        raise PulyRequestError(f"state is {size} bytes; at most {MAX_STATE_BYTES} are sent, and it is not cut")
    if not isinstance(questions, Mapping) or not questions:
        raise PulyRequestError("no questions")
    if len(questions) > MAX_QUESTIONS:
        raise PulyRequestError(f"{len(questions)} questions; at most {MAX_QUESTIONS} in one request")
    checked: dict[str, dict[str, Any]] = {}
    for raw_id, raw in questions.items():
        qid = str(raw_id or "").strip()
        if not qid or len(qid) > MAX_QUESTION_ID:
            raise PulyRequestError(f"question id {raw_id!r} is empty or longer than {MAX_QUESTION_ID}")
        if not isinstance(raw, Mapping):
            raise PulyRequestError(f"question {qid}: not an object")
        kind = str(raw.get("type", "") or "")
        if kind not in QUESTION_TYPES:
            raise PulyRequestError(f"question {qid}: type {kind!r} is not one of {', '.join(QUESTION_TYPES)}")
        instructions = raw.get("instructions")
        if not _nonempty(instructions):
            raise PulyRequestError(f"question {qid}: no instructions")
        question: dict[str, Any] = {"type": kind, "instructions": instructions}
        criteria = raw.get("criteria")
        if kind == "noul":
            if criteria is not None:
                if not isinstance(criteria, Mapping) or not criteria or set(map(str, criteria)) - {"true", "false"}:
                    raise PulyRequestError(f"question {qid}: yes/no criteria take only the keys true and false")
                question["criteria"] = {str(key): value for key, value in criteria.items()}
        elif kind == "choice":
            if not isinstance(criteria, Mapping) or len(criteria) < 2:
                raise PulyRequestError(f"question {qid}: a choice needs at least two options")
            if len(criteria) > MAX_CHOICE_OPTIONS:
                raise PulyRequestError(f"question {qid}: {len(criteria)} options; the service takes {MAX_CHOICE_OPTIONS}")
            options = {str(key).strip(): value for key, value in criteria.items()}
            if "" in options or len(options) != len(criteria):
                raise PulyRequestError(f"question {qid}: option names must be distinct and not empty")
            question["criteria"] = options
        else:
            low, high = SCORE_LEVELS
            if not isinstance(criteria, list) or not low <= len(criteria) <= high:
                raise PulyRequestError(f"question {qid}: a score needs {low} to {high} rubric levels")
            if not all(_nonempty(level) for level in criteria):
                raise PulyRequestError(f"question {qid}: a rubric level is empty")
            question["criteria"] = list(criteria)
        if qid in checked:
            raise PulyRequestError(f"question id {qid} is used twice")
        checked[qid] = question
    encode_body(state, checked)
    return state, checked


def encode_body(state: Any, questions: Mapping[str, Any]) -> bytes:
    """The request exactly as it is sent -- strict JSON (no NaN, nothing that is not JSON) -- or PulyRequestError.

    Encoded here, before anything is reserved: a body that cannot be encoded never reaches the client, where the
    failure would read as "sent, outcome unknown" and hold the order's allowance for a request that never left.
    """
    try:
        body = json.dumps({"state": state, "model": JEV_MODEL, "questions": questions}, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PulyRequestError(f"the request cannot be sent as JSON: {type(exc).__name__}") from None
    if len(body) > MAX_BODY_BYTES:
        raise PulyRequestError(f"the request is {len(body)} bytes; at most {MAX_BODY_BYTES} are sent")
    return body


# ── The answer ──────────────────────────────────────────────────────────────


class _Malformed(ValueError):
    pass


def _probability(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Malformed(f"{what} is not a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise _Malformed(f"{what} is outside 0..1")
    return number


def _distribution(value: Any, what: str) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _Malformed(f"{what} is not a map")
    return {str(key): _probability(item, f"{what}[{key}]") for key, item in value.items()}


def _read_answers(payload: Any, questions: Mapping[str, Mapping[str, Any]],
                  thresholds: Mapping[str, float]) -> dict[str, dict[str, Any]]:
    answers = payload.get("answers") if isinstance(payload, Mapping) else None
    if not isinstance(answers, Mapping):
        raise _Malformed("the response has no answers")
    read: dict[str, dict[str, Any]] = {}
    for qid, question in questions.items():
        raw = answers.get(qid)
        kind = question["type"]
        if not isinstance(raw, Mapping) or raw.get("type") != kind:
            raise _Malformed(f"no {kind} answer for {qid}")
        legend: dict[str, Any] = {}
        if kind == "noul":
            p = _probability(raw.get("noul"), f"{qid}.noul")
            value: Any = p
            # Only what the service said. A yes/no answer is not coherent under negation (TypeSafe's own example: refund
            # 0.72 and not_refund 0.47 on one ticket), so a "false" probability of 1-p would be a number JEV never gave.
            probabilities = {"true": p}
            confidence = None
            # The service gives a yes/no answer no confidence of its own. The gate's measure is our rule, said as one:
            # how far p is from the middle, on the side it falls (the receipt names the basis).
            measure, basis = max(p, 1.0 - p), "max(p, 1-p)"
        elif kind == "choice":
            value = raw.get("choice")
            if not isinstance(value, str) or value not in question["criteria"]:
                raise _Malformed(f"{qid}: the chosen option is not one of those asked")
            probabilities = _distribution(raw.get("probabilities"), f"{qid}.probabilities")
            confidence = _probability(raw.get("confidence"), f"{qid}.confidence")
            measure, basis = confidence, "confidence"
        else:
            score = raw.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                raise _Malformed(f"{qid}: the score is not a number")
            value = float(score)
            if not 0.0 <= value <= float(len(question["criteria"])):
                raise _Malformed(f"{qid}: the score {value} is outside the rubric's {len(question['criteria'])} levels")
            probabilities = _distribution(raw.get("probabilities"), f"{qid}.probabilities")
            legend = dict(raw.get("legend") or {}) if isinstance(raw.get("legend"), Mapping) else {}
            confidence = _probability(raw.get("confidence"), f"{qid}.confidence")
            measure, basis = confidence, "confidence"
        threshold = thresholds.get(qid)
        abstained = threshold is not None and measure < threshold
        read[qid] = {
            "type": kind,
            "value": value,
            "probabilities": probabilities,
            "confidence": confidence,
            "measure": measure,
            "measure_basis": basis,
            "threshold": threshold,
            "abstained": abstained,
            **({"legend": legend} if kind == "score" else {}),
        }
    return read


def decided(receipt: Mapping[str, Any], question_id: str) -> Any:
    """The answer to act on, or ``None`` -- and ``None`` means a person decides.

    Yes/no gives ``True``/``False``; a choice, the option; a score, the number. Only an answered receipt whose answer
    cleared the caller's threshold gives anything: refused, busy, unreachable, failed, unknown and abstained do not.
    """
    if receipt.get("outcome") != "answered":
        return None
    answer = dict(receipt.get("answers", {}) or {}).get(question_id)
    if not isinstance(answer, Mapping) or answer.get("abstained"):
        return None
    if answer.get("type") == "noul":
        p = float(answer["value"])
        return None if p == 0.5 else p > 0.5
    return answer.get("value")


# ── The call ────────────────────────────────────────────────────────────────


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    header = str(response.headers.get("retry-after", "") or "").strip()
    try:
        wait = float(header)
    except ValueError:
        wait = float(2 ** attempt)
    return max(0.0, min(wait, MAX_RETRY_WAIT_S))


def _said(response: httpx.Response, key: str) -> str:
    """What the service said about a refused request, short, with the key taken out should it ever be echoed."""
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, Mapping):
        body = body.get("error") or body.get("detail") or body.get("message") or body
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    if key:
        text = text.replace(key, "•••")
    return " ".join(text.split())[:200]


async def judge(
    state: Any,
    questions: Mapping[str, Any],
    *,
    purpose: str,
    thresholds: Mapping[str, float] | None = None,
    meter: Callable[..., Awaitable[Any]] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    """Ask PULY; return the receipt. ``purpose`` names the ask in the log and the receipt (never the state).

    Raises ``PulyRequestError`` for a request that cannot be sent, and a budget stop (``ModelBudgetStop``) when an
    order's allowance refuses the call or holds it; every other outcome is in the receipt.
    """
    state, checked = check_request(state, questions)
    limits: dict[str, float] = {}
    for qid, value in dict(thresholds or {}).items():
        if qid not in checked:
            raise PulyRequestError(f"threshold for {qid!r}, which is not asked")
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0):
            raise PulyRequestError(f"threshold for {qid} must be a number from 0 to 1")
        limits[qid] = float(value)
    receipt: dict[str, Any] = {
        "provider": PULY_NAME,
        "backend": JEV_BACKEND,
        "purpose": str(purpose or ""),
        "model_requested": JEV_MODEL,
        "model": "",
        "outcome": "",
        "answers": {},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "attempts": 0,
        "http_status": None,
        "latency_ms": 0,
        "error": "",
    }
    key = _key()
    if not key:
        receipt.update(outcome="not_configured", error=f"{JEV_KEY_ENV} is not set: 配置 → 模型与密钥")
        return _logged(receipt)
    if not key.isascii():
        receipt.update(outcome="refused", error=f"{JEV_KEY_ENV} holds characters no API key has; paste it again")
        return _logged(receipt)
    content = encode_body(state, checked)
    started = time.monotonic()
    response: httpx.Response | None = None
    try:
        async with _client(key, transport, timeout) as client:
            for attempt in range(MAX_RETRIES + 1):
                receipt["attempts"] = attempt + 1
                response = None
                # Only an answer is charged (review R10): a request that never left this machine, or one TypeSafe
                # answered "no" to without doing the work, gives its hold back instead of settling it.
                try:
                    with model_retry_scope(True) if attempt else contextlib.nullcontext():
                        async with request_budget():
                            try:
                                mark_dispatched()
                                answered = await client.post(JEV_ENDPOINT, content=content)
                            except _NOT_SENT as exc:
                                raise _NotSent(type(exc).__name__) from exc
                            if answered.status_code >= 500 and answered.status_code not in _RETRYABLE:
                                raise _Uncertain(f"TypeSafe answered HTTP {answered.status_code}")
                            if answered.status_code in _RETRYABLE or 400 <= answered.status_code < 500:
                                raise _AnsweredNo(answered)
                            response = answered
                except _NotSent as exc:
                    # Never left this machine: a definite "not sent", nothing to verify.
                    receipt.update(outcome="unreachable", error=f"could not reach TypeSafe: {exc}")
                    break
                except _AnsweredNo as no:
                    response = no.response
                receipt["http_status"] = response.status_code
                if response.status_code in _RETRYABLE and attempt < MAX_RETRIES:
                    await sleep(_retry_wait(response, attempt))
                    continue
                break
    except (httpx.HTTPError, _Uncertain) as exc:
        # Unbound only: under an order's allowance the budget has already held the call and raised its own stop.
        receipt.update(outcome="unknown",
                       error=f"how the request ended is unknown ({type(exc).__name__}); it may have been answered "
                             "and billed, so it was not sent again")
    receipt["latency_ms"] = int((time.monotonic() - started) * 1000)
    if receipt["outcome"]:
        return _logged(receipt)
    assert response is not None
    status = response.status_code
    if status in _RETRYABLE:
        receipt.update(outcome="busy", error=f"TypeSafe is {'rate limiting' if status == 429 else 'overloaded'} "
                                              f"(HTTP {status}) after {receipt['attempts']} attempts")
    elif status in _REFUSED:
        receipt.update(outcome="refused", error=f"the key was not accepted (HTTP {status}): {_said(response, key)}")
    elif status in _REJECTED or 400 <= status < 500:
        receipt.update(outcome="rejected", error=f"the request was rejected (HTTP {status}): {_said(response, key)}")
    elif status != 200:
        receipt.update(outcome="failed", error=f"unexpected HTTP {status}")
    else:
        try:
            payload = response.json()
            receipt["answers"] = _read_answers(payload, checked, limits)
        except (ValueError, _Malformed) as exc:
            receipt.update(outcome="failed", error=f"the answer could not be read: {exc}"[:200])
        else:
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            receipt["usage"] = {
                "input_tokens": _count(usage.get("input_tokens")),
                "output_tokens": _count(usage.get("output_tokens")),
            }
            receipt["model"] = str(payload.get("model", "") or "")
            receipt["outcome"] = "answered"
            await _meter(meter, receipt)
    return _logged(receipt)


# Raised before a byte of the request left this machine: a definite "not sent".
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)


def _client(key: str, transport: httpx.AsyncBaseTransport | None, timeout: float) -> httpx.AsyncClient:
    """The client, carrying the key. Built here so that no line of ``judge`` a traceback may print holds the key."""
    return httpx.AsyncClient(
        transport=transport, timeout=timeout,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def _meter(meter: Callable[..., Awaitable[Any]] | None, receipt: dict[str, Any]) -> None:
    """File the tokens under the task that asked. No price is guessed: TypeSafe's is not published here."""
    if meter is None:
        return
    usage = receipt["usage"]
    try:
        await meter(model=f"typesafe/{receipt['model'] or JEV_MODEL}", tokens_in=usage["input_tokens"],
                    tokens_out=usage["output_tokens"], cost=0.0)
    except Exception as exc:  # the judgment happened; a meter that failed is said, not turned into "unknown"
        receipt["meter_error"] = type(exc).__name__
        logger.warning(f"PULY {receipt['purpose']}: usage not recorded ({type(exc).__name__})")


def _logged(receipt: dict[str, Any]) -> dict[str, Any]:
    logger.info(
        f"PULY {receipt['purpose'] or '-'}: {receipt['outcome']} via {receipt['model'] or receipt['model_requested']} "
        f"in {receipt['latency_ms']}ms, {receipt['attempts']} attempt(s)"
    )
    return receipt


async def check_connection(*, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """One small yes/no judgment, for the settings page's test button: does the key work, and which JEV answered.

    Always answers the button: anything unexpected comes back as its type name, never as a traceback.
    """
    try:
        receipt = await judge(
            "PULY connection test. This message is a connection test.",
            {"is_test": {"type": "noul", "instructions": "Is this message a connection test?"}},
            purpose="settings_connection_test",
            timeout=timeout,
            transport=transport,
        )
    except Exception as exc:
        return {"ok": False, "latency_ms": 0, "model": "", "error": f"the test did not complete ({type(exc).__name__})"}
    ok = receipt["outcome"] == "answered"
    return {
        "ok": ok,
        "latency_ms": receipt["latency_ms"],
        "model": receipt["model"],
        "error": "" if ok else receipt["error"] or receipt["outcome"],
    }
