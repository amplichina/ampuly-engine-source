"""A model that never calls anyone, for building against without an account.

Most of this system cannot be exercised offline today: every turn needs a real
model, so a run costs money, varies between attempts, and cannot be replayed.
That makes the parts *around* the model -- pauses, approvals, budgets,
recovery -- expensive to develop and impossible to assert on precisely.

This provider answers from a script instead. The important part is *where* the
script stops: it produces the same wire payloads litellm would return, and
then production code parses them. Tool-call argument parsing, usage
accounting, tool-history sanitisation, stream-event conversion and every state
transition downstream are the real ones. A replay that skipped to the final
answer would prove nothing about them.

Determinism is by request, not by order: the reply is chosen from a
fingerprint of the model and the conversation, so the same input gives the
same answer no matter when it is asked, and a re-run of one step does not
consume someone else's line.

It refuses to exist outside test mode, and it never reads a credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ampuly.core.config import LLMConfig
from ampuly.llm.provider import LLMProvider

# The one switch that allows a scripted model. Set per process, like the
# channel mute: a production console cannot turn it on from a settings page.
TEST_MODE_ENV = "AMPULY_TEST_MODE"
# What request_fingerprint emits today; older recordings must be regenerated.
FINGERPRINT_FORMAT = re.compile(r"^v2:[0-9a-f]{64}$")
# Models under this prefix are scripted by definition; naming one in a
# production configuration is refused rather than silently answered.
TEST_MODEL_PREFIX = "deterministic/"


class TestModeRefused(RuntimeError):
    """Raised when a scripted model is asked for outside test mode."""


def test_mode_enabled() -> bool:
    return (os.environ.get(TEST_MODE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_test_model(model: str) -> bool:
    return str(model or "").strip().startswith(TEST_MODEL_PREFIX)


@dataclass(frozen=True)
class ScriptedTurn:
    """One scripted reply, in the shape a provider would have returned.

    ``text`` and ``tool_calls`` are what the model "said"; ``error`` makes the
    call fail the way a provider failure does, so the retry and failure paths
    can be exercised without breaking anything real.
    """

    text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    error: str = ""
    # How the failure arrives. "timeout" raises what a transport timeout
    # raises, because the retry path treats it differently from an
    # ordinary provider error and that difference is what needs testing.
    error_kind: str = ""
    # Seconds the call takes before answering, so a configured request
    # timeout can actually fire against a script.
    delay_seconds: float = 0.0
    prompt_tokens: int = 12
    completion_tokens: int = 8
    # Streaming is delivered in these slices so a consumer sees several chunks.
    chunk_size: int = 24


@dataclass
class DeterministicScript:
    """What the scripted model says, keyed by what it was asked.

    ``default`` answers anything unmatched, which keeps a test from silently
    depending on an exact prompt. ``by_marker`` matches when a marker string
    appears anywhere in the conversation -- the readable way to say "when the
    worker asks about the logo brief, answer this".
    """

    default: ScriptedTurn = field(default_factory=lambda: ScriptedTurn(text="ok"))
    by_marker: dict[str, ScriptedTurn] = field(default_factory=dict)
    # Fingerprint -> turn, for replaying one exact recorded conversation.
    by_fingerprint: dict[str, ScriptedTurn] = field(default_factory=dict)
    # What the same request gets the second time it is asked. Without
    # this a script cannot tell "timed out, then answered" from "answered
    # twice", and cannot show a duplicate call at all.
    on_repeat: ScriptedTurn | None = None
    strict: bool = False

    def __post_init__(self) -> None:
        # A recorded key in an older format can never match a request again,
        # and with strict off the call would fall through to the default and
        # answer "ok" to a conversation that was meant to drive tool calls.
        stale = [str(k) for k in self.by_fingerprint if not FINGERPRINT_FORMAT.match(str(k))]
        if stale:
            raise ValueError(
                "fixture fingerprints are not the current v2 format and would never match; "
                "regenerate the fixture rather than replaying it silently: " + ", ".join(stale[:3])
            )

    def resolve(self, fingerprint: str, conversation: str) -> ScriptedTurn:
        if fingerprint in self.by_fingerprint:
            return self.by_fingerprint[fingerprint]
        for marker, turn in self.by_marker.items():
            if marker and marker in conversation:
                return turn
        if self.strict:
            raise LookupError("no scripted response matched this request")
        return self.default

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DeterministicScript:
        def _turn(raw: Any) -> ScriptedTurn:
            data = dict(raw or {})
            return ScriptedTurn(
                text=str(data.get("text", "") or ""),
                tool_calls=tuple(dict(c) for c in list(data.get("tool_calls", []) or [])),
                error=str(data.get("error", "") or ""),
                error_kind=str(data.get("error_kind", "") or ""),
                delay_seconds=max(0.0, float(data.get("delay_seconds", 0.0) or 0.0)),
                prompt_tokens=int(data.get("prompt_tokens", 12) or 0),
                completion_tokens=int(data.get("completion_tokens", 8) or 0),
                chunk_size=max(1, int(data.get("chunk_size", 24) or 24)),
            )

        return cls(
            strict=bool(payload.get("strict", False)),
            default=_turn(payload.get("default")) if payload.get("default") else ScriptedTurn(text="ok"),
            by_marker={str(k): _turn(v) for k, v in dict(payload.get("by_marker", {}) or {}).items()},
            by_fingerprint={str(k): _turn(v) for k, v in dict(payload.get("by_fingerprint", {}) or {}).items()},
            on_repeat=_turn(payload.get("on_repeat")) if payload.get("on_repeat") else None,
        )

    @classmethod
    def load(cls, path: Path) -> DeterministicScript:
        return cls.from_payload(json.loads(Path(path).read_text(encoding="utf-8")))


def conversation_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return "\n".join(parts)


def request_fingerprint(model: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None) -> str:
    """Versioned replay identity including roles, tool calls and attachments.

    Only the digest is retained; raw requests are not written to the call log.
    Version 1 text-only fixtures must be regenerated, not silently reused.
    """
    payload = {"version": 2, "model": str(model or ""), "messages": messages, "tools": tools or []}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "v2:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ── litellm-shaped objects, so production parsing runs unchanged ──


class _Message:
    def __init__(self, turn: ScriptedTurn) -> None:
        self.content = turn.text
        self.role = "assistant"
        self.tool_calls = [_ToolCall(call, index) for index, call in enumerate(turn.tool_calls)] or None
        self.reasoning_content = None


class _ToolCall:
    def __init__(self, call: dict[str, Any], index: int) -> None:
        self.id = str(call.get("id") or f"call_{index}")
        self.type = "function"
        self.function = _Function(call)
        self.index = index


class _Function:
    def __init__(self, call: dict[str, Any]) -> None:
        self.name = str(call.get("name", "") or "")
        arguments = call.get("arguments", {})
        # Providers send arguments as a JSON string; keep that so the
        # production argument parser is what turns it back into data.
        self.arguments = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)


class _Choice:
    def __init__(self, turn: ScriptedTurn) -> None:
        self.message = _Message(turn)
        self.finish_reason = "tool_calls" if turn.tool_calls else "stop"
        self.index = 0


class _Usage:
    def __init__(self, turn: ScriptedTurn) -> None:
        self.prompt_tokens = turn.prompt_tokens
        self.completion_tokens = turn.completion_tokens
        self.total_tokens = turn.prompt_tokens + turn.completion_tokens


class _Response:
    def __init__(self, turn: ScriptedTurn, model: str) -> None:
        self.choices = [_Choice(turn)]
        self.usage = _Usage(turn)
        self.model = model
        self.id = "deterministic"


class _StreamDelta:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None
        self.role = "assistant"


class _StreamChoice:
    def __init__(self, delta: _StreamDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason
        self.index = 0


class _StreamChunk:
    def __init__(self, choices: list[_StreamChoice], usage: _Usage | None = None) -> None:
        self.choices = choices
        self.usage = usage


class DeterministicProvider(LLMProvider):
    """An ``LLMProvider`` whose transport is a script rather than a network.

    Everything above the transport -- selection, clamping, parsing, usage
    accounting, event conversion -- is the production implementation, because
    that is the part worth testing.
    """

    def __init__(self, config: LLMConfig, script: DeterministicScript | None = None, ampuly_home: Path | None = None) -> None:
        if not test_mode_enabled():
            raise TestModeRefused(
                f"a scripted model needs {TEST_MODE_ENV}=1; refusing to answer without it"
            )
        safe_config = config.model_copy(update={"api_key": None, "api_key_env": "", "api_base": None})
        super().__init__(safe_config, ampuly_home=ampuly_home)
        self.script = script or DeterministicScript()
        self.calls: list[dict[str, Any]] = []
        # How many times each exact request has been asked. A repeated
        # request is either a retry or money spent twice on one answer,
        # and neither was visible before.
        self.attempts: dict[str, int] = {}
        # Never a real credential, and never read one.
        self._api_key = None
        self._api_base = None
        # The ids a test says are in the catalogue: a scripted provider has no catalogue of its own, so a subagent's
        # model override is refused unless the test lists it (model-routing phase 0).
        self.listed_models: set[str] = set()

    @staticmethod
    def _env_from_user_registry(name: str | None) -> None:
        # The parent constructor calls this fallback. A test double never
        # queries the host registry, including when its input config has a key.
        return None

    def model_listed(self, model: str) -> bool:
        chosen = str(model or "").strip()
        return bool(chosen) and (chosen == self.config.default_model or chosen in self.listed_models)

    def for_model(self, model: str, *, customer_id: str = "", environment_id: str = "") -> "DeterministicProvider":
        """A scripted sibling on ``model``: the same script, and one record of every call, whichever model answered.

        No key is looked up for a test double, so the catalogue resolver is not asked either.
        """
        chosen = str(model or "").strip()
        if not chosen or chosen == self.config.default_model:
            return self
        sibling = DeterministicProvider(
            self.config.model_copy(update={"default_model": chosen, "routing": {}}), script=self.script, ampuly_home=self.ampuly_home,
        )
        sibling.calls = self.calls
        sibling.attempts = self.attempts
        sibling.listed_models = self.listed_models
        return sibling

    # A scripted provider is "credentialed" in the sense the engine cares
    # about -- it can answer -- while holding no secret at all.
    def has_credentials(self) -> bool:
        return True

    @property
    def is_test_double(self) -> bool:
        return True

    def _scripted(self, call_kwargs: dict[str, Any]) -> ScriptedTurn:
        messages = list(call_kwargs.get("messages", []) or [])
        model = str(call_kwargs.get("model", "") or "")
        fingerprint = request_fingerprint(model, messages, tools=list(call_kwargs.get("tools", []) or []))
        attempt = self.attempts.get(fingerprint, 0) + 1
        self.attempts[fingerprint] = attempt
        # The repeat decision lives here, not in ``resolve``: fixtures
        # subclass the script and override that method, and a new
        # argument would break every one of them.
        repeat = getattr(self.script, "on_repeat", None)
        turn = repeat if (attempt > 1 and repeat is not None) else self.script.resolve(fingerprint, conversation_text(messages))
        self.calls.append({
            "model": model,
            "fingerprint": fingerprint,
            "attempt": attempt,
            "tools": [t.get("function", {}).get("name") for t in list(call_kwargs.get("tools", []) or [])],
            "streamed": bool(call_kwargs.get("stream")),
        })
        return turn

    def repeated_requests(self) -> dict[str, int]:
        """Requests this provider was asked more than once, and how often.

        A repeat is not automatically wrong -- a retry is a repeat -- but
        an unexplained one is the same question paid for twice.
        """
        return {key: count for key, count in self.attempts.items() if count > 1}

    async def _acompletion(self, **call_kwargs: Any) -> Any:
        turn = self._scripted(call_kwargs)
        if turn.delay_seconds > 0:
            await asyncio.sleep(turn.delay_seconds)
        if turn.error or turn.error_kind:
            raise _scripted_failure(turn)
        if not call_kwargs.get("stream"):
            return _Response(turn, str(call_kwargs.get("model", "")))
        return _stream(turn)


def _scripted_failure(turn: ScriptedTurn) -> BaseException:
    """The exception a scripted failure raises, in the shape it would arrive.

    A timeout is raised as ``TimeoutError`` (which is
    ``asyncio.TimeoutError``) so code that distinguishes a timed-out call
    from a refused one is exercised rather than bypassed.
    """
    kind = str(turn.error_kind or "").strip().lower()
    if kind == "timeout":
        return TimeoutError(turn.error or "scripted timeout")
    # A "no" the vendor sent back, typed as litellm raises it (model-routing phase 0, C9): a context longer than
    # the model's window (400), a quota or rate limit (429), any other received rejection (400).
    import litellm

    message = turn.error or f"scripted {kind or 'failure'}"
    if kind == "overflow":
        return litellm.ContextWindowExceededError(message, model="scripted", llm_provider="scripted")
    if kind == "rate_limit":
        return litellm.RateLimitError(message, llm_provider="scripted", model="scripted")
    if kind == "rejected":
        return litellm.BadRequestError(message, model="scripted", llm_provider="scripted")
    return RuntimeError(message)


async def _stream(turn: ScriptedTurn):
    text = turn.text or ""
    for start in range(0, max(len(text), 1), turn.chunk_size):
        piece = text[start:start + turn.chunk_size]
        if piece:
            yield _StreamChunk([_StreamChoice(_StreamDelta(content=piece))])
    for index, call in enumerate(turn.tool_calls):
        yield _StreamChunk([_StreamChoice(_StreamDelta(tool_calls=[_ToolCall(call, index)]))])
    yield _StreamChunk([_StreamChoice(_StreamDelta(), finish_reason="tool_calls" if turn.tool_calls else "stop")], _Usage(turn))


def scripted_model_selections(config: Any, *, _depth: int = 0) -> list[tuple[str, str]]:
    """Every place a configuration names a scripted model, as (path, model).

    A walk rather than a list of known fields: models are named in the
    default, in routing tables, in per-profile overrides and in whatever
    the next feature adds, and a check that enumerates today's fields
    stops being true the moment one is added.
    """
    if _depth > 12:
        return []
    found: list[tuple[str, str]] = []
    if config is None:
        return found
    if hasattr(config, "model_dump") and not isinstance(config, dict):
        try:
            config = config.model_dump()
        except Exception:
            return found
    if isinstance(config, str):
        return [("", config)] if is_test_model(config) else []
    if isinstance(config, dict):
        items: Any = config.items()
    elif isinstance(config, (list, tuple)):
        items = ((str(index), value) for index, value in enumerate(config))
    else:
        return found
    for key, value in items:
        for sub_path, model in scripted_model_selections(value, _depth=_depth + 1):
            found.append((f"{key}.{sub_path}" if sub_path else str(key), model))
    return found


def refuse_scripted_models_outside_test_mode(config: Any, *, where: str = "configuration") -> None:
    """Fail at startup, naming every offending field, or do nothing.

    Starting anyway means the mistake surfaces at the first model call,
    as a provider error, long after the person who could fix it has
    stopped watching.
    """
    if test_mode_enabled():
        return
    selections = scripted_model_selections(config)
    if not selections:
        return
    named = "; ".join(f"{path or 'model'}={model}" for path, model in selections)
    raise TestModeRefused(
        f"{where} names scripted model(s) [{named}] but {TEST_MODE_ENV} is not set; "
        "a scripted model answers from a script, so this would run the business on invented output"
    )


def build_provider(config: LLMConfig, script: DeterministicScript | None = None, ampuly_home: Path | None = None) -> LLMProvider:
    """Return a scripted provider when test mode asks for one; else refuse.

    The refusal is the point: a production configuration that names a
    ``deterministic/`` model is a mistake, not a request for a stub, and it
    must fail loudly instead of quietly answering with a script.
    """
    if not is_test_model(getattr(config, "default_model", "")):
        raise TestModeRefused(f"{config.default_model!r} is not a scripted model")
    if not test_mode_enabled():
        raise TestModeRefused(
            f"configuration names the scripted model {config.default_model!r} but {TEST_MODE_ENV} is not set"
        )
    return DeterministicProvider(config, script=script, ampuly_home=ampuly_home)
