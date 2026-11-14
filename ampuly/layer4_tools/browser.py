"""Native Playwright-backed browser tools."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import inspect
import time
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

from ampuly.core.config import AmpulyConfig, get_ampuly_home, replace_with_retry
from ampuly.computer_use.download_safety import (
    deliverable_filename,
    inspect_quarantined,
    normalized_types,
    prepare_confined_directory,
    preflight_type,
    publish_without_overwrite,
    safe_filename,
)
from ampuly.layer4_tools.browser_sessions import BrowserBinding, BrowserSessionRefused, BrowserSessionRegistry
from ampuly.layer4_tools.output_budget import clip_text
from ampuly.layer4_tools.registry import (
    COMPANY_EFFECT_FORBIDDEN,
    COMPANY_EFFECT_NO_LOCAL_FS,
    ToolDefinition,
)

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - exercised via install-hint tests
    PlaywrightError = RuntimeError
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


class BrowserElementNotFound(RuntimeError):
    """A selector or ref matched nothing. Raised before any input is sent, so a
    receipt may say failed_before_effect rather than outcome_unknown."""


class BrowserControlLost(BrowserElementNotFound):
    """G04 / C06: control of the page left the agent before this action sent any input; nothing was sent."""


class BrowserControlLostAfterInput(RuntimeError):
    """G04 / C06: control left the agent after part of this action's input was sent; what the page did is unknown,
    and nothing of the page a person now holds is read back."""


# The longest a browser_wait_for may hold the runtime (and so the person's pane and the owner's revoke) waiting.
MAX_WAIT_SECONDS = 30.0

# Set around one admitted agent action (the tool face, an approved submit): the runtime asks it right before
# sending input and before reading the page back. Unset -- a person's own pane input, tests of the runtime -- asks nothing.
_AGENT_DISPATCH_GUARD: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar("agent_dispatch_guard", default=None)
# What the target must still be when the input is sent (the kind it was admitted as): the actionability wait can take
# seconds, and the same selector can meanwhile resolve to a different control (Next became "Confirm and pay").
_AGENT_TARGET_CHECK: contextvars.ContextVar[Callable[[dict[str, Any]], None] | None] = contextvars.ContextVar("agent_target_check", default=None)


class _DispatchGate:
    """One agent action's input, held to its guard at the last moment before each piece is sent."""

    def __init__(self, guard: Callable[[], bool]) -> None:
        self._guard = guard
        self.input_sent = False

    def check(self) -> None:
        if self._guard():
            return
        if self.input_sent:
            raise BrowserControlLostAfterInput("a person took control of the page during this action; wait for the hand-back and take a new snapshot")
        raise BrowserControlLost("a person took control of the page before this action sent anything; nothing was sent")


def _dispatch_gate() -> _DispatchGate | None:
    guard = _AGENT_DISPATCH_GUARD.get()
    return _DispatchGate(guard) if guard is not None else None


class _GatedLocator:
    """A locator whose input waits for the target (Playwright's own actionability wait), then asks the guard, then
    sends. The upstream action bodies call it exactly as they call a Playwright locator."""

    _SEND_TIMEOUT_MS = 2_000

    def __init__(self, inner: Any, gate: _DispatchGate) -> None:
        self._inner = inner
        self._gate = gate
        self.first = self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _recheck_target(self) -> None:
        """Read the element as it is now, after the wait and before the last guard question, and hold it to what was
        admitted (and, for an approved submit, to the form values approved). A read that cannot finish sent nothing."""
        check = _AGENT_TARGET_CHECK.get()
        if check is None:
            return
        try:
            live = _described_target(await self._inner.evaluate(_DESCRIBE_TARGET_SCRIPT, timeout=self._SEND_TIMEOUT_MS))
            if getattr(check, "wants_form", False):
                check(live, await self._inner.evaluate(_FORM_STATE_SCRIPT, timeout=self._SEND_TIMEOUT_MS))
            else:
                check(live)
        except PlaywrightTimeoutError:
            raise BrowserElementNotFound("the target went away while it was getting ready; nothing was sent") from None

    def _send_timeout(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        timeout = kwargs.get("timeout")
        return {**kwargs, "timeout": min(int(timeout), self._SEND_TIMEOUT_MS) if timeout else self._SEND_TIMEOUT_MS}

    async def click(self, **kwargs: Any) -> Any:
        try:
            await self._inner.click(trial=True, timeout=kwargs.get("timeout", 10_000))
        except PlaywrightTimeoutError:
            if self._gate.input_sent:
                raise  # an earlier piece of this action already sent input
            raise BrowserElementNotFound(
                "target_not_actionable: the click readiness check timed out; no input was sent. "
                "Observe the page and dismiss a covering dialog before proposing a fresh action."
            ) from None
        if not self._gate.input_sent:
            await self._recheck_target()
        self._gate.check()
        result = await self._inner.click(**self._send_timeout(kwargs))
        self._gate.input_sent = True
        return result

    async def _until_ready(self, timeout_ms: int, *checks: str) -> None:
        """Wait for what the send would wait for (visible, enabled, editable), inside the caller's budget, so the short
        send never times out on a page that is still getting ready. A target that never gets ready sent nothing."""
        deadline = time.monotonic() + max(0.1, timeout_ms / 1000)
        await self._inner.wait_for(state="visible", timeout=timeout_ms)
        while True:
            ready = True
            for name in checks:
                probe = getattr(self._inner, name, None)
                if callable(probe) and not await probe():
                    ready = False
                    break
            if ready:
                return
            if time.monotonic() >= deadline:
                raise BrowserElementNotFound("the target did not become ready in time; nothing was sent")
            await asyncio.sleep(0.15)

    async def fill(self, value: str, **kwargs: Any) -> Any:
        await self._until_ready(int(kwargs.get("timeout", 10_000)), "is_enabled", "is_editable")
        await self._recheck_target()
        self._gate.check()
        result = await self._inner.fill(value, **self._send_timeout(kwargs))
        self._gate.input_sent = True
        return result

    async def select_option(self, value: Any = None, **kwargs: Any) -> Any:
        timeout_ms = int(kwargs.get("timeout", 10_000))
        await self._until_ready(timeout_ms, "is_enabled")
        wanted = {key: kwargs[key] for key in ("index", "label") if kwargs.get(key) is not None}
        if value is not None:
            wanted["value"] = value
        deadline = time.monotonic() + max(0.1, timeout_ms / 1000)
        # Options can still be arriving (a cascading select): wait for them with a read, never by retrying the send --
        # a select that timed out may already have fired its change handlers.
        while not await self._inner.evaluate(_OPTION_PRESENT_SCRIPT, wanted):
            if time.monotonic() >= deadline:
                raise BrowserElementNotFound("the option did not appear in time; nothing was selected")
            self._gate.check()
            await asyncio.sleep(0.15)
        await self._recheck_target()
        self._gate.check()
        # From here the change may be applied even if the call fails: a lost control after this is an unknown outcome.
        self._gate.input_sent = True
        send = self._send_timeout(kwargs)
        return await (self._inner.select_option(value, **send) if value is not None else self._inner.select_option(**send))

    async def type(self, text: str, **kwargs: Any) -> Any:
        # Key by key is a loop the driver runs to the end once started: the text goes in short pieces, each asked first,
        # so a takeover stops the typing within a piece rather than after the whole string.
        pieces = [text[start:start + 8] for start in range(0, len(text), 8)] or [text]
        if not self._gate.input_sent:
            await self._recheck_target()
        self._gate.check()
        result = await self._inner.type(pieces[0], **kwargs)
        self._gate.input_sent = True
        # The first piece focused the target; the rest follow the keyboard focus as one typed string would (an
        # auto-advancing phone or card field moves it on purpose), without focusing the target again.
        keyboard = getattr(getattr(self._inner, "page", None), "keyboard", None)
        for piece in pieces[1:]:
            self._gate.check()
            result = await (keyboard.type(piece) if keyboard is not None else self._inner.type(piece, **kwargs))
        return result

    async def press(self, key: str, **kwargs: Any) -> Any:
        self._gate.check()
        result = await self._inner.press(key, **kwargs)
        self._gate.input_sent = True
        return result


class BrowserCloseFailed(RuntimeError):
    """A browser resource could not be proven closed.

    Raised instead of returning success. `reason` is one of `close_failed`
    (a close call raised), `close_cancelled` (the close was cancelled part
    way, so the state is unknown) or `close_pending` (a new page was asked
    for while an earlier close is still unresolved). The provider's own
    error text is deliberately not carried: this reaches a model.
    """

    def __init__(self, message: str, *, reason: str, unclosed: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.reason = str(reason or "").strip()
        self.unclosed = tuple(unclosed)


_INSTALL_HINT = (
    "Browser tools require the optional Playwright dependency. "
    "Install it with `pip install -e .[browser]` and then run "
    "`python -m playwright install chromium`."
)
_EMBEDDED_BROWSER_ARGS = ("--disable-dev-shm-usage", "--no-sandbox")
# X02 / C26: a channel the request-level egress policy cannot see is closed at launch. WebRTC's STUN/ICE is
# UDP -- there is no request for a route to judge. Real run, 2026-09-14 (Playwright 1.62): a page on an
# allowed host sent STUN datagrams to an unlisted host under the default launch. The headless shell honours
# this switch; the full Chromium binary (chrome mode, any headed launch) ignores it and takes the policy from
# the profile's preferences instead -- so those launches run on a profile with the preference written first.
# Service workers' own fetches do go through context routing on this build; the real-browser probe checks
# both channels, so an upgrade that changes either shows up as a failed C26 row.
_EGRESS_LAUNCH_SWITCH = "--force-webrtc-ip-handling-policy"
_SPKI_ALLOW_SWITCH = "--ignore-certificate-errors-spki-list"
_EGRESS_LAUNCH_ARGS = (f"{_EGRESS_LAUNCH_SWITCH}=disable_non_proxied_udp",)

_UNSAFE_TLS_SWITCHES = (
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    _SPKI_ALLOW_SWITCH,
)
_WEBRTC_PROFILE_POLICY = "disable_non_proxied_udp"


def _close_webrtc_udp_in_profile(user_data_dir: str) -> None:
    """Write webrtc.ip_handling_policy into the profile's preferences before the browser reads them.

    Only the one key is set; the rest of an existing Preferences file is kept. An unreadable file is
    replaced by one that holds the key (Chromium rebuilds the rest).
    """
    prefs_path = Path(user_data_dir) / "Default" / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_path.is_file():
        try:
            loaded = json.loads(prefs_path.read_text(encoding="utf-8"))
            prefs = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            prefs = {}
    webrtc = prefs.get("webrtc") if isinstance(prefs.get("webrtc"), dict) else {}
    prefs["webrtc"] = {**webrtc, "ip_handling_policy": _WEBRTC_PROFILE_POLICY}
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = prefs_path.with_name(prefs_path.name + ".superones-tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    replace_with_retry(tmp, prefs_path)


def _validated_spki_pins(values: Any) -> tuple[str, ...]:
    """Return canonical SHA-256 SPKI pins or fail before a browser starts."""
    pins: list[str] = []
    for raw in list(values or ()):
        pin = str(raw or "").strip()
        try:
            decoded = base64.b64decode(pin, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("browser.trusted_spki_sha256 contains invalid base64") from exc
        if len(decoded) != 32:
            raise ValueError("browser.trusted_spki_sha256 entries must be SHA-256 SPKI digests")
        canonical = base64.b64encode(decoded).decode("ascii")
        if canonical not in pins:
            pins.append(canonical)
    return tuple(pins)


def _with_egress_launch_args(args: Any, trusted_spki_sha256: Any = ()) -> list[str]:
    """Apply product-owned network/TLS launch policy after caller arguments.

    Caller-supplied switches cannot reopen WebRTC or disable certificate
    verification. A private service can be trusted only through an explicit,
    validated SPKI pin owned by the SuperOnes configuration.

    WebTransport/QUIC is deliberately not claimed here: real Chromium probes
    show that its UDP bypasses these launch switches from dedicated workers.
    That channel requires a network/process boundary (see the X02 gap record).
    """
    kept: list[str] = []
    for item in list(args or ()):
        value = str(item)
        lowered = value.lower()
        if lowered.startswith(_EGRESS_LAUNCH_SWITCH):
            continue
        if any(lowered == switch or lowered.startswith(f"{switch}=") for switch in _UNSAFE_TLS_SWITCHES):
            continue
        kept.append(value)
    pins = _validated_spki_pins(trusted_spki_sha256)
    owned = list(_EGRESS_LAUNCH_ARGS)
    if pins:
        owned.append(f"{_SPKI_ALLOW_SWITCH}={','.join(pins)}")
    return kept + owned
# X03 / C27: what a screenshot never shows, whatever the caller asks for.
SCREENSHOT_ALWAYS_MASK = (
    'input[type="password"]',
    "[data-sensitive]",
    '[autocomplete="current-password"]',
    '[autocomplete="new-password"]',
    '[autocomplete^="cc-"]',
    '[autocomplete="one-time-code"]',
)


def _child_frames(page: Any) -> list[Any]:
    """Every currently attached child frame, including srcdoc/about:blank.

    Snapshot text applies its own URL and count limits.  Screenshot masking
    must not: a secret field does not become safe because its frame has no URL
    or appears after the first six frames.
    """

    main = getattr(page, "main_frame", None)
    children: list[Any] = []
    for frame in list(getattr(page, "frames", []) or []):
        if frame is page or frame is main:
            continue
        detached = getattr(frame, "is_detached", None)
        try:
            if bool(detached() if callable(detached) else detached):
                continue
        except Exception:  # noqa: BLE001 - include uncertain frames; screenshot then fails closed if detached
            pass
        children.append(frame)
    return children


def _is_tls_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    return any(marker in message for marker in ("err_cert", "certificate", "ssl", "tls"))


# C18: an allowed download is still bounded. 50 MB unless the task says otherwise.
DEFAULT_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024


def _with_stable_refs(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every interactive element a ref that survives re-snapshots of the same page.

    The ref is a digest of what identifies the element (tag, selector, type,
    role, placeholder, text), not its position in the list, so a page whose
    elements reorder keeps the same refs and a page whose elements change
    gets new ones. Duplicates are told apart by occurrence.
    """
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for item in elements:
        basis = "|".join(str(item.get(k, "") or "") for k in ("tag", "selector", "type", "role", "placeholder"))
        basis += "|" + str(item.get("text", "") or "")[:60]
        ref = "e" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:6]
        n = seen.get(ref, 0)
        seen[ref] = n + 1
        out.append({**item, "ref": ref if n == 0 else f"{ref}-{n}"})
    return out


# Q05 B1: the reader lists up to 250 controls a frame and this many a document; the model is shown fewer, in view first.
MAX_SNAPSHOT_ELEMENTS = 600
MODEL_ELEMENT_LIMIT = 80


def _over_cap(read: dict[str, Any]) -> int:
    """How many shown controls a frame's reader left out for its per-frame cap (0 for a reader that does not say)."""
    try:
        total = int(read.get("interactive_total", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, total - len(list(read.get("interactive", []) or [])))


def _count_left_out(omitted: dict[str, Any], left_out: Any) -> None:
    for key in ("in_editable",):
        try:
            omitted[key] += int(dict(left_out or {}).get(key, 0) or 0)
        except (TypeError, ValueError, AttributeError):
            continue


def _model_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The rows the model is shown, and how many it is not: those in view first, each group in document order.

    Only the list the model reads is ordered this way; refs and the page digest keep document order (C05).
    A row whose reader said nothing about the viewport counts as in view (an older reader, a test page).
    """
    def in_view(row: dict[str, Any]) -> bool:
        state = row.get("state")
        return not isinstance(state, dict) or state.get("in_view", True) is not False

    # An aria-hidden or inert control cannot be operated, and what it says reaches nobody watching: kept from the model.
    visible = [row for row in rows if not (isinstance(row.get("state"), dict) and row["state"].get("hidden") is True)]
    ordered = [row for row in visible if in_view(row)] + [row for row in visible if not in_view(row)]
    shown = ordered[:MODEL_ELEMENT_LIMIT]
    return shown, max(0, len(rows) - len(shown))


# What the model reads of a row: the fields it always had, a short name where the control has no words of its own, a
# few flags, a list's first choices. The full row (state, every option) stays with the runtime and the decider.
_MODEL_ROW_FIELDS = ("ref", "tag", "text", "type", "role", "placeholder", "selector", "frame")
# A structural selector can run to hundreds of characters; the model acts by ref, so a long one is left out.
_MODEL_SELECTOR_LIMIT = 80


def _model_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row[key] for key in _MODEL_ROW_FIELDS if key in row and (key != "frame" or row[key])}
    if len(str(out.get("selector", "") or "")) > _MODEL_SELECTOR_LIMIT and out.get("ref"):
        out.pop("selector", None)
    if not str(row.get("text", "") or "").strip() and str(row.get("name", "") or "").strip():
        out["name"] = str(row["name"]).strip()[:60]
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    flags = [key for key in ("disabled", "readonly", "checked", "expanded") if state.get(key) is True]
    if state.get("fill") in {"empty", "filled", "sensitive"}:
        flags.append(str(state["fill"]))
    if flags:
        out["flags"] = flags
    options = [str(o)[:30] for o in list(row.get("options", []) or [])]
    if options:
        out["options"] = options[:5]
        total = int(row.get("options_total", len(options)) or len(options))
        if total > 5:
            out["options_total"] = total
    return out


# A snapshot the model reads stays under the tightest budget on its way: the runtime keeps 12,000 characters of a tool
# message (context_guard.tool_output_char_budget) and cuts the middle of anything longer; the registry keeps 20,000.
# Rows beyond what fits are counted, never cut silently; the ones in view are kept first.
_SNAPSHOT_RESULT_BUDGET = 10_500


def _fit_rows(snapshot: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """`rows` in priority order (in view first); what fits comes back in document order (`_doc` on each row)."""
    room = _SNAPSHOT_RESULT_BUDGET - len(json.dumps(snapshot, ensure_ascii=False, default=str))
    kept: list[dict[str, Any]] = []
    for row in rows:
        size = len(json.dumps({k: v for k, v in row.items() if k != "_doc"}, ensure_ascii=False, default=str)) + 2
        if size > room:
            continue
        kept.append(row)
        room -= size
    kept.sort(key=lambda row: int(row.get("_doc", 0)))
    return [{k: v for k, v in row.items() if k != "_doc"} for row in kept], len(rows) - len(kept)


# C05 covers every row (review of the B3 diff: a window of the first rows let an approved click land on a list that
# moved further down). A clock or a badge count must not refuse every action on a busy page, so the words enter the
# digest with their digits masked; the row set, its order, the selectors and the refs' non-numeric words still count,
# and the row around each target is re-checked at the moment of acting (context, _click_target / _click_still).
_DIGITS = re.compile(r"[0-9０-９]+")


def _digest_rows(interactive: list[dict[str, Any]]) -> list[list[str]]:
    return [[str(item.get(key, "") or "") for key in ("frame", "tag", "selector", "type", "role", "placeholder")]
            + [_DIGITS.sub("#", str(item.get("text", "") or ""))] for item in interactive]


def _structure_digest(url: str, headings: list[Any], interactive: list[dict[str, Any]]) -> str:
    """What a structured snapshot of a page hashes to: url, headings, and each element's ref, tag and text."""
    return hashlib.sha256(json.dumps(
        {"url": url, "headings": headings,
         "elements": _digest_rows(interactive)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _host_path(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return f"{parts.hostname or ''}{parts.path or ''}"[:160]


def _is_navigation(request: Any) -> bool:
    probe = getattr(request, "is_navigation_request", None)
    try:
        return bool(probe()) if callable(probe) else str(getattr(request, "resource_type", "")) in {"document", "iframe"}
    except Exception:  # noqa: BLE001 - treat an unreadable request as a navigation: the stricter path
        return True


@dataclass(frozen=True)
class BrowserLaunchConfig:
    mode: str = "embedded"
    headless: bool = True
    chrome_channel: str = "chrome"
    chrome_executable_path: str = ""
    user_data_dir: str = ""
    args: tuple[str, ...] = ()
    trusted_spki_sha256: tuple[str, ...] = ()
    # Server-owned account sessions opt in; never loaded from user configuration.
    persistent_profile: bool = False

    @classmethod
    def load(cls) -> "BrowserLaunchConfig":
        config_dir = get_ampuly_home() / "config"
        if not config_dir.is_dir():
            return cls()
        config = AmpulyConfig.load(config_dir)
        browser = getattr(config.system, "browser", None)
        if browser is None:
            return cls()
        return cls(
            mode=str(browser.mode or "embedded").strip().lower(),
            headless=bool(browser.headless),
            chrome_channel=str(browser.chrome_channel or "").strip(),
            chrome_executable_path=str(browser.chrome_executable_path or "").strip(),
            user_data_dir=str(browser.user_data_dir or "").strip(),
            args=tuple(str(arg).strip() for arg in (browser.args or []) if str(arg).strip()),
            trusted_spki_sha256=tuple(str(pin).strip() for pin in (browser.trusted_spki_sha256 or []) if str(pin).strip()),
        )


# What a live target is, for classifying the input it is about to get (C08). One element, read in its own frame:
# whether typing into it is typing into a text field, and whether clicking it submits its form. The text of an
# editable element (what someone typed) is never read.
_DESCRIBE_TARGET_SCRIPT = """
(node) => {
  const clip = (v) => (v || "").replace(/\\s+/g, " ").trim().slice(0, 120);
  const isElement = (e) => Boolean(e) && e.nodeType === 1;
  // Up through slots and shadow boundaries, the way a click's activation travels.
  const up = (e) => e.assignedSlot || e.parentElement || (e.getRootNode && e.getRootNode() && e.getRootNode().host) || null;
  const matches = (e, selector) => isElement(e) && Boolean(e.matches) && e.matches(selector);
  let el = node;
  // Retarget the way Playwright's own fill and click do: a label (or text inside one) acts on the control it names.
  if (!matches(el, "input, textarea, select, button, a, [role=button], [role=link], [role=checkbox], [role=radio]") && el.isContentEditable !== true) {
    const label = el.closest ? el.closest("label") : null;
    if (label && label.control) el = label.control;
  }
  const typeOf = (e) => {
    const tag = (e.tagName || "").toLowerCase();
    if (tag === "input" || tag === "button") return String(e.type || "").toLowerCase();
    // A form-associated custom element (a Material or Shoelace button) submits by its own type property.
    return tag.includes("-") ? String(e.type == null ? "" : e.type).toLowerCase() : "";
  };
  const typed = ["text", "search", "email", "url", "tel", "password", "number"];
  const setByFill = ["date", "time", "datetime-local", "month", "week", "color", "range"];
  const elTag = (el.tagName || "").toLowerCase();
  const editable = el.disabled !== true && (elTag === "textarea" || el.isContentEditable === true
    || (elTag === "input" && (typed.includes(typeOf(el)) || setByFill.includes(typeOf(el)))));
  const formOf = (e) => e.form || (e.closest && e.closest("form")) || null;
  const isSubmitter = (e) => {
    const tag = (e.tagName || "").toLowerCase();
    const idl = typeOf(e);
    if (tag.includes("-")) {
      const formAssociated = Boolean(e.constructor && e.constructor.formAssociated === true);
      return formAssociated && (idl === "submit" || idl === "image") && Boolean(formOf(e));
    }
    return ((tag === "button" && idl === "submit") || (tag === "input" && (idl === "submit" || idl === "image"))) && Boolean(formOf(e));
  };
  // A click on a span or an icon inside a button activates the button: describe the control, and let any submitter
  // on the way up count as submitting.
  let control = null;
  let submitter = null;
  for (let at = el, depth = 0; isElement(at) && depth < 40; depth += 1, at = up(at)) {
    if (!submitter && isSubmitter(at)) submitter = at;
    if (!control && matches(at, "button, input, select, textarea, a[href], area[href], summary, [role=button], [role=link], [role=switch], [role=checkbox], [role=radio], [role=tab], [role=menuitem]")) control = at;
    if (control && submitter) break;
  }
  let target = editable ? el : (control || el);
  const tagOf = (e) => (e.tagName || "").toLowerCase();
  const refText = (e, name) => {
    const ids = String((e.getAttribute && e.getAttribute(name)) || "").split(/\\s+/).filter(Boolean);
    const root = e.getRootNode ? e.getRootNode() : null;
    const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null);
    return clip(ids.map((id) => { const found = byId(id); return found ? (found.innerText || found.textContent || "") : ""; }).join(" "));
  };
  // Never inside what someone types: an image pasted into a text field is their content (C27).
  const deep = (e) => e.isContentEditable !== true && !["textarea", "input"].includes(tagOf(e));
  const ownAlt = (e) => ((["img", "area"].includes(tagOf(e)) || (tagOf(e) === "input" && typeOf(e) === "image")) && e.getAttribute
    ? clip(e.getAttribute("alt")) : "");
  // The accessible name: aria-label, the text of aria-labelledby, title, an image's own alt, then an image's alt or an icon's title inside.
  const nameOf = (e) => {
    if (!isElement(e)) return "";
    const attr = (name) => (e.getAttribute && e.getAttribute(name)) || "";
    if (attr("aria-label")) return clip(attr("aria-label"));
    if (refText(e, "aria-labelledby")) return refText(e, "aria-labelledby");
    if (attr("title")) return clip(attr("title"));
    if (ownAlt(e)) return ownAlt(e);
    if (!deep(e)) return "";
    const img = e.querySelector ? e.querySelector("img[alt], input[type=image][alt], area[alt]") : null;
    if (img && img.getAttribute("alt")) return clip(img.getAttribute("alt"));
    const svg = e.querySelector ? e.querySelector("svg[aria-label], svg title") : null;
    if (svg) return clip(svg.getAttribute && svg.getAttribute("aria-label") || svg.textContent);
    return "";
  };
  // Every name a control carries, read for effect words: a row name from aria-labelledby must not hide a title saying Delete.
  const namesOf = (e) => {
    if (!isElement(e)) return "";
    const attr = (name) => (e.getAttribute && e.getAttribute(name)) || "";
    const found = [attr("aria-label"), refText(e, "aria-labelledby"), attr("title"), attr("data-original-title"),
      attr("data-bs-original-title"), attr("data-tooltip"), ownAlt(e)];
    const inside = deep(e) && e.querySelectorAll
      ? Array.from(e.querySelectorAll("img[alt], input[type=image][alt], area[alt], svg[aria-label], svg title")).slice(0, 6) : [];
    for (const one of inside) found.push((one.getAttribute && (one.getAttribute("alt") || one.getAttribute("aria-label"))) || one.textContent);
    const kept = [];
    for (const one of found.map(clip)) if (one && !kept.includes(one)) kept.push(one);
    // Each source was already clipped. Keep enough of the combined names for a
    // trailing tooltip such as title="Delete" to survive a long row label.
    return kept.join(" ").slice(0, 600);
  };
  // With no control, a click on something wordless (an icon) is taken by the nearest element with a handler of its own:
  // a pointer cursor set there rather than inherited, a tab stop, an onclick. That carrier is what is described, and one
  // nothing names is refused by sight. A canvas or a video is a surface of its own, judged as itself.
  let clickable = false;
  let carried = false;
  if (!control && !editable && !["canvas", "video"].includes(elTag) && !clip(el.innerText || el.textContent) && !namesOf(el)) {
    const cursorOf = (e) => {
      try { return isElement(e) && typeof getComputedStyle === "function" ? String(getComputedStyle(e).cursor || "") : ""; } catch (err) { return ""; }
    };
    for (let at = el, depth = 0; isElement(at) && depth < 6; depth += 1, at = up(at)) {
      const ownPointer = cursorOf(at) === "pointer" && cursorOf(up(at)) !== "pointer";
      const tabStop = /^\\s*\\d+\\s*$/.test(String((at.getAttribute && at.getAttribute("tabindex")) || ""));
      if (ownPointer || tabStop || Boolean(at.hasAttribute && at.hasAttribute("onclick"))) {
        target = at; clickable = true; carried = at !== el; break;
      }
    }
  }
  const attr = (name) => (target.getAttribute && target.getAttribute(name)) || "";
  const tag = (target.tagName || "").toLowerCase();
  const idlType = typeOf(target);
  const form = (submitter && formOf(submitter)) || formOf(target);
  // The method the browser will use: the submitter's own formmethod, else the form's, as written.
  const method = submitter && submitter.hasAttribute && submitter.hasAttribute("formmethod")
    ? String(submitter.getAttribute("formmethod") || "").toLowerCase()
    : (form ? String(form.getAttribute("method") || "").toLowerCase() : "");
  const buttonLike = tag === "input" && ["button", "submit", "reset", "image"].includes(idlType);
  const href = (tag === "a" || tag === "area") ? attr("href") : "";
  // A plain link only navigates: no script or framework handler turns its click into a request.
  const scripted = ["onclick", "onmousedown", "onmouseup", "data-method", "data-remote", "data-confirm", "data-turbo-confirm",
    "data-turbo-stream", "hx-post", "hx-put", "hx-patch", "hx-delete", "wire:click", "x-on:click", "ng-click", "v-on:click"]
    .some((name) => target.hasAttribute && target.hasAttribute(name))
    || (attr("data-turbo-method") && attr("data-turbo-method").toLowerCase() !== "get");
  const plainLink = Boolean(href) && !href.startsWith("#") && /^(https?:|\\/|\\.|[^:]*$)/i.test(href)
    && !attr("role") && !scripted && !submitter;
  // A dialog, found the way activation travels (through slots and shadow hosts), and what kind of dialog it is. A
  // popover is its overlay (role=tooltip, a class ending in "popover"), not the row of buttons inside it.
  let dialog = null;
  let fallback = null;
  const buttonCount = (box) => (box.querySelectorAll ? box.querySelectorAll("button, a").length : 0);
  for (let at = target, depth = 0; isElement(at) && depth < 40; depth += 1, at = up(at)) {
    if (matches(at, "[role=alertdialog], [role=dialog], [aria-modal=true], dialog[open], .ant-popconfirm, .ant-modal-confirm, .el-message-box, .el-popconfirm, "
      + ".ivu-modal-confirm, .ivu-poptip-confirm, .n-popconfirm, .arco-popconfirm, .t-popconfirm, .layui-layer-dialog")) { dialog = at; break; }
    const overlay = matches(at, "[role=tooltip]") || /(^|\\s)[\\w-]*popover(\\s|$)/i.test(String((at.getAttribute && at.getAttribute("class")) || ""));
    if ((overlay || matches(at, "[class*=popover]")) && buttonCount(at) >= 2) {
      fallback = fallback || at;
      if (overlay) { dialog = at; break; }
    }
  }
  dialog = dialog || fallback;
  // A way out is any other control the dialog shows (取消, 再想想, a close button); an information popup's lone OK has none.
  const shown = (b) => !(b.hasAttribute && (b.hasAttribute("hidden") || b.getAttribute("aria-hidden") === "true"))
    && (typeof b.getClientRects !== "function" || b.getClientRects().length > 0);
  const others = dialog && dialog.querySelectorAll
    ? Array.from(dialog.querySelectorAll("button, a, [role=button]"))
      .filter((b) => b !== target && !(b.contains && b.contains(target)) && !(target.contains && target.contains(b)) && shown(b))
    : [];
  const dialogChoice = others.length > 0;
  const dialogInfo = Boolean(dialog) && !dialogChoice
    && matches(dialog, ".ant-modal-confirm-info, .ant-modal-confirm-success, .ant-modal-confirm-warning, .ant-modal-confirm-error");
  // What the dialog confirms: its title and message first, so a long body never clips the question away.
  let dialogText = "";
  if (dialog) {
    const heads = [refText(dialog, "aria-labelledby"), refText(dialog, "aria-describedby")];
    const titled = dialog.querySelectorAll
      ? Array.from(dialog.querySelectorAll("h1, h2, h3, h4, h5, h6, [class*=title], [class*=message], [class*=desc]")).filter(deep).slice(0, 4) : [];
    for (const one of titled) heads.push(clip(one.innerText || one.textContent));
    heads.push(clip(dialog.innerText || dialog.textContent));
    const kept = [];
    for (const one of heads) if (one && !kept.includes(one)) kept.push(one);
    dialogText = clip(kept.join(" "));
  }
  // A separately labelled thing inside a link (an action span in a link card) is judged by its own words too.
  const hit = ((control && (tag === "a" || tag === "area")) || carried) && el !== target ? el : null;
  // Q05 B3: a field whose content is a secret (the form reader's rule) is a person's to fill; a list that submits when a
  // choice is made is a submit, not a choice.
  const fieldLabels = el.labels ? Array.from(el.labels).map((l) => l.innerText || l.textContent || "").join(" ") : "";
  const own = (name) => (el.getAttribute && el.getAttribute(name)) || "";
  // The refusal is narrow on purpose (a field named "passenger" is not a password): the type, the autocomplete purpose,
  // the page's own marker, or a whole word that names a secret.
  const secretWords = /\\b(pass(word|code)?|pwd|otp|cvv|cvc|pin)\\b|验证码|校验码|动态码|密码|口令|卡号|身份证|证件号/i;
  const sensitive = editable && (typeOf(el) === "password" || Boolean(el.hasAttribute && el.hasAttribute("data-sensitive"))
    || /(^|\\s)(cc-[a-z-]+|one-time-code|current-password|new-password)(\\s|$)/.test(own("autocomplete").toLowerCase())
    || secretWords.test([own("name"), el.id || "", own("placeholder"), own("aria-label"), fieldLabels].join(" ")));
  const rowBox = target.closest ? target.closest('tr, li, [role="row"], [role="listitem"], article, [role="article"]') : null;
  let context = "";
  if (rowBox) {
    const words = String(rowBox.innerText || rowBox.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 300);
    let h = 5381;
    for (let i = 0; i < words.length; i += 1) h = ((h * 33) ^ words.charCodeAt(i)) >>> 0;
    context = h.toString(16);
  }
  const autosubmit = tag === "select" && ["onchange", "oninput", "data-submit", "data-autosubmit", "data-remote", "hx-post", "hx-get", "hx-trigger"]
    .some((name) => target.hasAttribute && target.hasAttribute(name));
  return {
    tag, type: attr("type").toLowerCase(), role: attr("role").toLowerCase(), placeholder: clip(attr("placeholder")),
    // Never what someone typed: an editable element's text and every input's value stay out (C27).
    text: editable ? "" : clip(buttonLike ? target.value : (target.innerText || target.textContent)),
    label: nameOf(target), names: namesOf(target),
    editable, submits: Boolean(submitter),
    form_method: form ? method : "",
    // Where the click sends, resolved by the browser itself (a relative action rewritten absolute cannot slip through).
    href: (tag === "a" || tag === "area") ? String(target.href || attr("href") || "").slice(0, 512) : "",
    form_action: form
      ? String((submitter && submitter.hasAttribute && submitter.hasAttribute("formaction") ? submitter.formAction : form.action) || "").slice(0, 512)
      : "",
    form_role: form ? String(form.getAttribute("role") || "").toLowerCase() : "",
    plain_link: plainLink, in_dialog: Boolean(dialog), dialog_info: dialogInfo, dialog_choice: dialogChoice,
    dialog_text: dialogText,
    hit_text: hit ? clip(hit.innerText || hit.textContent) : "", hit_label: hit ? nameOf(hit) : "", hit_names: hit ? namesOf(hit) : "",
    clickable, carried, sensitive, autosubmit, context,
  };
}
"""

_ELEMENT_AT_SCRIPT = (
    "([x, y]) => {\n"
    "  const describe = " + _DESCRIBE_TARGET_SCRIPT.strip() + ";\n"
    """
  let el = document.elementFromPoint(x, y);
  if (!el) return null;
  // An open shadow root retargets to its host; look inside it for what is really under the point.
  for (let depth = 0; el && el.shadowRoot && depth < 8; depth += 1) {
    const inner = el.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === el) break;
    el = inner;
  }
  const tag = (el.tagName || "").toLowerCase();
  const described = describe(el);
  return Object.assign(described, {
    // The page's own document cannot say what a control inside a frame or a closed widget is.
    frame: ["iframe", "frame", "object", "embed"].includes(tag),
    // A hyphenated element hit directly, with no open shadow root and nothing inside it to hit instead: a closed widget.
    opaque: tag.includes("-") && !el.shadowRoot && !el.firstElementChild,
  });
}
""")

# The fields a click on this target would submit: its form's controls, or with no form (a script-driven button) every
# field of its document. Checkbox and radio by state, select by chosen values, file by name and size only.
_FORM_STATE_SCRIPT = """
(node) => {
  const isElement = (e) => Boolean(e) && e.nodeType === 1;
  const up = (e) => e.assignedSlot || e.parentElement || (e.getRootNode && e.getRootNode() && e.getRootNode().host) || null;
  let control = node;
  for (let at = node, depth = 0; isElement(at) && depth < 40; depth += 1, at = up(at)) {
    if (at.matches && at.matches("button, input, select, textarea, a[href], [role=button]")) { control = at; break; }
  }
  const form = control.form || (control.closest && control.closest("form")) || null;
  const scope = form || (control.getRootNode ? control.getRootNode() : document);
  const near = control.closest ? control.closest("dialog, [role=dialog], [role=alertdialog], [aria-modal=true], .ant-modal, .el-dialog, .modal, form") : null;
  const selector = "input, select, textarea, [contenteditable=''], [contenteditable=true], [role=switch], [role=checkbox], [role=radio], "
    + "[role=slider], [role=spinbutton], [role=combobox]";
  // Everything the click would send: the form's own elements (fields tied by form= and form-associated custom elements
  // included), its descendants, and fields inside open shadow roots.
  const seen = new Set();
  const nodes = [];
  const add = (el) => { if (!seen.has(el)) { seen.add(el); nodes.push(el); } };
  if (form && form.elements) Array.from(form.elements).forEach(add);
  let visited = 0;
  const collect = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(selector).forEach(add);
    for (const host of root.querySelectorAll("*")) {
      visited += 1;
      if (visited > 20000) return;
      if (host.shadowRoot) collect(host.shadowRoot);
    }
  };
  collect(scope);
  const identifying = /pass|pwd|token|secret|api.?key|otp|captcha|cvv|cvc|(sms|verify|verification|auth)[\\s_-]*code|验证码|校验码|动态码|密码|口令|卡号|身份证|证件号/i;
  const volatileNames = ["cf-turnstile-response", "g-recaptcha-response", "h-captcha-response"];
  const out = [];
  let total = 0;
  for (const field of nodes) {
    const tag = (field.tagName || "").toLowerCase();
    const attr = (name) => (field.getAttribute && field.getAttribute(name)) || "";
    const type = String(field.type == null ? attr("type") : field.type).toLowerCase();
    const role = attr("role").toLowerCase();
    if (["submit", "button", "reset", "image", "fieldset", "output"].includes(type) || tag === "button" || tag === "fieldset") continue;
    const labels = field.labels ? Array.from(field.labels).map((l) => l.innerText || l.textContent || "").join(" ") : "";
    const name = String(attr("name") || field.id || attr("aria-label") || labels.trim() || tag).slice(0, 80);
    let value = "";
    if (type === "checkbox" || type === "radio") value = field.checked ? String(field.value || "on") : "";
    else if (tag === "select") value = Array.from(field.selectedOptions || []).map((o) => o.value).join(",");
    else if (type === "file") value = Array.from(field.files || []).map((f) => `${f.name}:${f.size}`).join(",");
    else if (["switch", "checkbox", "radio"].includes(role)) value = attr("aria-checked");
    else if (["slider", "spinbutton"].includes(role)) value = attr("aria-valuenow");
    else if (role === "combobox") {
      const widget = field.closest ? field.closest(".ant-select, .ant-cascader, .el-select, .el-cascader") : null;
      const chosen = widget ? Array.from(widget.querySelectorAll(".ant-select-selection-item, .ant-select-selection-overflow-item, .el-select__tags-text, .el-input__inner"))
        .map((c) => c.getAttribute("title") || c.value || c.innerText || c.textContent || "").join("|") : "";
      value = `${field.value == null ? "" : field.value}|${chosen}`;
    }
    else if (field.isContentEditable) value = String(field.innerText || "");
    else value = String(field.value == null ? "" : field.value);
    const autocomplete = attr("autocomplete").toLowerCase();
    const volatile = volatileNames.includes(attr("name"));
    const sensitive = volatile || type === "password" || type === "hidden" || field.readOnly === true || attr("data-sensitive") !== ""
      || /cc-|one-time-code|password|csc/.test(autocomplete)
      || identifying.test([attr("name"), field.id || "", attr("placeholder"), attr("aria-label"), labels].join(" "));
    const worthy = value !== "" && type !== "hidden" && !volatile;
    total += value.length;
    out.push({ name, value, sensitive, volatile, near: Boolean(near && near.contains && near.contains(field)), worthy });
    if (out.length > 2000 || total > 1000000) return { fields: out, overflow: true };
  }
  return { fields: out, overflow: false };
}
"""

# Whether a select already holds the option asked for, matched as Playwright matches: a value or a label, a label, an index.
_OPTION_PRESENT_SCRIPT = """
(el, want) => {
  let select = el;
  if (select && select.matches && !select.matches("a, input, textarea, button, select, [role=link], [role=button], [role=checkbox], [role=radio]")
      && !select.isContentEditable) {
    const label = select.closest ? select.closest("label") : null;
    if (label && label.control) select = label.control;
  }
  if (!select || (select.tagName || "").toUpperCase() !== "SELECT") return false;
  const options = Array.from(select.options || []);
  const enabled = (o) => !o.disabled && !(o.parentElement && o.parentElement.tagName === "OPTGROUP" && o.parentElement.disabled)
    && o.getAttribute("aria-disabled") !== "true";
  const norm = (v) => String(v == null ? "" : v).replace(/\\s+/g, " ").trim();
  if (want.index !== undefined) return Boolean(options[Number(want.index)]) && enabled(options[Number(want.index)]);
  if (want.label !== undefined) return options.some((o) => enabled(o) && norm(o.label) === norm(want.label));
  const values = Array.isArray(want.value) ? want.value : [want.value];
  return values.every((v) => options.some((o) => enabled(o) && (o.value === v || norm(o.label) === norm(v))));
}
"""

_DESCRIBED_FIELDS = ("tag", "type", "role", "placeholder", "text", "label", "form_method", "form_role", "dialog_text",
                     "hit_text", "hit_label")
# Effect words may be carried by a trailing tooltip after a long row name. These fields are never shown to the owner;
# each individual source is already capped at 120 characters in the page script.
_WORD_DESCRIBED_FIELDS = ("names", "hit_names")
# Where a click sends: kept longer than a label, because the tail of a pay URL is what tells two orders apart.
_LONG_DESCRIBED_FIELDS = ("href", "form_action")


def form_state_of(raw: Any) -> dict[str, Any]:
    """The form reader's answer as {fields, overflow}; a bare list (an older reader) is fields without overflow."""
    if isinstance(raw, dict):
        fields = raw.get("fields")
        return {"fields": [dict(f) for f in fields if isinstance(f, dict)] if isinstance(fields, list) else [],
                "overflow": raw.get("overflow") is True}
    if isinstance(raw, list):
        return {"fields": [dict(f) for f in raw if isinstance(f, dict)], "overflow": False}
    return {"fields": [], "overflow": False}


def _described_target(raw: Any) -> dict[str, Any]:
    """The describer's answer with every field present; anything else describes nothing (so nothing is editable)."""
    source = raw if isinstance(raw, dict) else {}
    described: dict[str, Any] = {key: str(source.get(key, "") or "")[:120] for key in _DESCRIBED_FIELDS}
    described.update({key: str(source.get(key, "") or "")[:600] for key in _WORD_DESCRIBED_FIELDS})
    described.update({key: str(source.get(key, "") or "")[:512] for key in _LONG_DESCRIBED_FIELDS})
    described["context"] = str(source.get("context", "") or "")[:16]
    for flag in ("editable", "submits", "plain_link", "in_dialog", "dialog_info", "dialog_choice", "clickable", "carried",
                 "sensitive", "autosubmit"):
        described[flag] = source.get(flag) is True
    return described


class BrowserRuntime:
    """Single-browser runtime shared by native browser tools."""

    def __init__(self, config_loader: Callable[[], BrowserLaunchConfig] | None = None) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        # Set when a close left something unresolved; cleared only by a
        # close that fully succeeds. No timer clears it.
        self._close_failed = False
        self._config_loader = config_loader or BrowserLaunchConfig.load
        self._launch_config: BrowserLaunchConfig | None = None
        # C26 / G02: the hosts this browser may talk to at the request level --
        # navigations, redirects, iframes, subresources and WebSockets alike.
        # Empty means no delegated host. Set per session by the Gateway bridge.
        self._egress_hosts: tuple[str, ...] = ()
        self._egress_scope: tuple[str, ...] = ()
        self._person_driving = False
        self._verification_only = False
        self._launch_started = False
        self._egress_blocked: list[dict[str, Any]] = []
        # C4: only transport facts, never URLs, headers or request bodies. A
        # submit provider compares snapshots of this report around its click.
        self._request_failures: list[dict[str, Any]] = []
        self._writes_pending = 0
        # X02 / C03: stable element refs from the last structured snapshot, and
        # that snapshot's digest -- what an action taken "on what was observed"
        # binds to in the Gateway.
        self._refs: dict[str, str] = {}
        self._last_elements: list[dict[str, Any]] = []
        self._last_omitted: dict[str, Any] = {}
        self._last_snapshot_url: str = ""
        self.last_snapshot_digest: str = ""
        self.last_screenshot: dict[str, Any] = {}
        self._output_root: Path | None = None
        # X05 / C06: a page a person will be handed must be on a screen. Set from the task before the
        # first navigate (that is when the browser launches); a running headless browser cannot change.
        self._visible: bool = False
        # X05: the live view. A CDP session on the page streams frames to one owner console and
        # takes a person's input while the person holds the page. Never the model's path.
        self._cdp: Any = None
        self._cdp_generation = 0
        self._screencast_on_frame: Any = None
        # X02 real run : a login form inside an iframe (passport.aliyun.com under
        # account.aliyun.com) left the snapshot empty. Child frames are read too -- every frame the
        # browser could load is on an allowed host, the request-level policy saw to that -- and a
        # ref taken from one is acted on in that frame.
        self._ref_frames: dict[str, str] = {}
        # C18 / C29: a download lands only in a product-owned directory bound to
        # this session, and only when the task allows downloads at all. Off by
        # default: Playwright would otherwise save every download to a temp
        # directory nobody bound to a customer. Popups are closed on sight and
        # counted: the agent drives one page, and a second one is not in the
        # observation/action sequence (X02 later slices may adopt tabs).
        self._downloads_allowed = False
        self._download_dir: Path | None = None
        self._download_root: Path | None = None
        # Data phase 1: tasks of one order step share a download folder, so each runtime stages in its own
        # quarantine and never removes another's.
        self._quarantine_tag = uuid.uuid4().hex[:8]
        # Told the product name of every download this runtime publishes (the bridge journals it per binding).
        self._download_listener: Callable[[str], Any] | None = None
        self._download_max_bytes = DEFAULT_DOWNLOAD_MAX_BYTES
        self._download_types: tuple[str, ...] = normalized_types(())
        self._downloads: list[dict[str, Any]] = []
        self._popups_closed: list[dict[str, Any]] = []
        self._popups_adopted: list[dict[str, Any]] = []
        self._tabs: list[Any] = []
        # The registry owns who may drive the page.  The WS layer checks too,
        # but the runtime re-checks at the final transport boundary so a
        # queued input cannot cross a hand-back.  An in-flight dispatch fences
        # the hand-back until CDP has accepted or refused it.
        self._human_input_guard: Callable[[], bool] | None = None
        self._human_inputs_in_flight = 0
        # The current action's dispatch gate while an agent action holds the lock; None otherwise.
        self._action_gate: _DispatchGate | None = None
        # Set by the registry as it closes this session's browser; cleared when it hands the runtime to a new reservation.
        self._retired = False

    def retire(self) -> None:
        """The session this runtime served is closing: nothing may launch a browser on it afterwards."""
        self._retired = True

    def revive(self) -> None:
        """Handed to a new reservation by the registry (a factory that reuses runtimes)."""
        self._retired = False

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        async with self._lock:
            page = await self._ensure_page()
            await page.goto(url, wait_until=wait_until, timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=6_000)

    async def snapshot(self, filename: str | None = None, max_chars: int = 12_000) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            snapshot = await self._build_snapshot(page, max_chars=max_chars)
            if filename:
                path = self._resolve_output_path(filename, suffix=".md")
                path.write_text(self._snapshot_to_markdown(snapshot), encoding="utf-8")
                snapshot["saved_to"] = str(path)
            return snapshot

    async def observation_facts(self) -> dict[str, Any]:
        """Read the current page for a pre/post comparison without changing refs or snapshot state."""
        async with self._lock:
            page = await self._require_page()
            payload = await self._read_document(page)
            return {
                "url": str(payload.get("url", "") or getattr(page, "url", "")),
                "text": str(payload.get("text", "") or ""),
                "interactive_elements": list(payload.get("interactive", []) or []),
            }

    async def page_facts_for_control(self, *, include_cookie_count: bool = False) -> dict[str, Any]:
        """Observe an already-open page without launching, navigating or replacing refs.

        Absent fields are unknown. Cookie values never cross this interface;
        only the handoff caller requests a count, not every agent admission.
        Ownership and its before/after fence remain the caller's responsibility.
        """
        async with self._lock:
            page = self._page
            if page is None:
                return {}
            url = getattr(page, "url", None)
            facts: dict[str, Any] = {}
            if isinstance(url, str) and 0 < len(url) <= 8192:
                facts["url"] = url
            title = getattr(page, "title", None)
            if callable(title):
                try:
                    value = title()
                    value = await value if inspect.isawaitable(value) else value
                    if isinstance(value, str):
                        facts["title"] = value[:512]
                except Exception:  # an unreadable title is unknown, not an empty title
                    pass
            if include_cookie_count:
                jar = getattr(getattr(page, "context", None), "cookies", None)
                if callable(jar):
                    try:
                        cookies = jar()
                        cookies = await cookies if inspect.isawaitable(cookies) else cookies
                        if isinstance(cookies, (list, tuple)) and len(cookies) <= 1_000_000:
                            facts["cookie_count"] = len(cookies)
                    except Exception:  # an unreadable jar must not become a zero count
                        pass
            # A navigation/close while awaiting a title or cookie query cannot
            # produce a mixture of facts from two different pages.
            if self._page is not page or getattr(page, "url", None) != url:
                return {}
            return facts

    async def click(self, selector: str) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            await locator.click(timeout=10_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=4_000)

    async def press_enter(self, selector: str) -> dict[str, Any]:
        """Press Enter on one resolved field.

        This is intentionally an internal transport primitive.  The public
        tool reaches it only through the approved browser-submit Effect.
        """
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            await locator.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=4_000)

    async def type(
        self,
        selector: str,
        text: str,
        press_enter: bool = False,
        clear_existing: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            if clear_existing:
                await locator.fill(text, timeout=10_000)
            else:
                await locator.click(timeout=10_000)
                await locator.type(text, timeout=10_000)
            if press_enter:
                await locator.press("Enter")
            return await self._build_snapshot(page, max_chars=4_000)

    async def wait_for(
        self,
        selector: str | None = None,
        timeout_seconds: float = 10.0,
        state: str = "visible",
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            # At most 30 s, in half-second slices: a wait holds the runtime lock, and a person taking the page (or the
            # owner revoking it) must not queue behind a long one.
            budget = min(max(0.1, float(timeout_seconds or 0)), MAX_WAIT_SECONDS)
            deadline = time.monotonic() + budget
            load_state = state if state in {"load", "domcontentloaded", "networkidle"} else "networkidle"
            while True:
                # Sliced only under an agent's guard, which is asked between slices; otherwise one wait.
                slice_ms = (max(100, min(500, int((deadline - time.monotonic()) * 1000))) if self._action_gate is not None
                            else max(100, int(budget * 1000)))
                try:
                    if selector:
                        await page.wait_for_selector(selector, state=state, timeout=slice_ms)
                    else:
                        await page.wait_for_load_state(load_state, timeout=slice_ms)
                    break
                except PlaywrightTimeoutError:
                    if time.monotonic() >= deadline:
                        raise
                    if self._action_gate is not None:
                        self._action_gate.check()
            return await self._build_snapshot(page, max_chars=4_000)

    async def scroll(
        self,
        amount: int = 800,
        direction: str = "down",
        to_bottom: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            if to_bottom:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            else:
                delta = abs(int(amount or 0))
                if direction.strip().lower() == "up":
                    delta = -delta
                await page.evaluate(f"window.scrollBy(0, {delta})")
            try:
                await page.wait_for_load_state("networkidle", timeout=3_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=4_000)

    async def select_option(
        self,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            if index is not None:
                await locator.select_option(index=int(index), timeout=10_000)
            elif label is not None:
                await locator.select_option(label=label, timeout=10_000)
            elif value is not None:
                await locator.select_option(value, timeout=10_000)
            else:
                raise RuntimeError("Provide at least one of `value`, `label`, or `index`.")
            return await self._build_snapshot(page, max_chars=4_000)

    async def navigate_back(self) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            previous = await page.go_back(wait_until="domcontentloaded", timeout=15_000)
            if previous is None:
                raise RuntimeError("No previous page in browser history.")
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=6_000)

    async def reload(self) -> dict[str, Any]:
        """Load the current address again -- after the owner widened the hosts its resources may come from."""
        async with self._lock:
            page = await self._require_page()
            await page.reload(wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return {"url": str(getattr(page, "url", "") or ""), "reloaded": True}

    async def evaluate_for_diagnostics(self, expression: str, selector: str | None = None) -> dict[str, Any]:
        """Run script in the page. Diagnostics and tests only: no tool reaches this (G01, audit 8.1).

        Renamed from `evaluate` in X02 so that nothing can wire it to a model by
        accident; the tool face is pinned by tests/test_x01_gateway_refusals.py.
        """
        async with self._lock:
            page = await self._require_page()
            if selector:
                locator = await self._resolve_locator(page, selector)
                result = await locator.evaluate(expression)
            else:
                result = await page.evaluate(expression)
            return {
                "url": page.url,
                "title": await page.title(),
                "result": result,
            }

    async def take_screenshot(
        self,
        filename: str | None = None,
        full_page: bool = True,
        mask_selectors: Any = None,
    ) -> dict[str, Any]:
        """A screenshot with sensitive fields masked before it exists on disk (X03 / C04 / C27).

        Password fields and anything marked data-sensitive are always masked;
        the caller may add selectors. The result says what was masked, and the
        runtime remembers the file so the next Gateway observation can carry
        a redacted screenshot reference rather than a raw image.
        """
        async with self._lock:
            page = await self._require_page()
            path = self._resolve_output_path(filename, suffix=".png")
            selectors = list(SCREENSHOT_ALWAYS_MASK) + [str(s) for s in list(mask_selectors or []) if str(s or "").strip()]
            children: list[Any] = []
            for capture in range(2):
                children = _child_frames(page)
                frame_ids = tuple(id(frame) for frame in children)
                masks = [page.locator(selector) for selector in selectors]
                masks.extend(frame.locator(selector) for frame in children for selector in selectors)
                try:
                    await page.screenshot(path=str(path), full_page=full_page, mask=masks)
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                if tuple(id(frame) for frame in _child_frames(page)) == frame_ids:
                    break
                path.unlink(missing_ok=True)
                if capture:
                    raise RuntimeError("frames_changed_during_screenshot")
            # X03 / C30: what a coordinate read off this picture depends on, taken with it.
            fingerprint = await self._page_fingerprint(page)
            masked_frames = 1 + len(children)
            self.last_screenshot = {"path": str(path), "redacted": True, "masked": selectors,
                                    "masked_frames": masked_frames, "page_fingerprint": fingerprint}
            return {
                "saved_to": str(path),
                "url": page.url,
                "title": await page.title(),
                "redacted_selectors": selectors,
                "masked_frames": masked_frames,
            }

    @property
    def is_open(self) -> bool:
        """Whether this runtime is holding any browser resource.

        Read by the session registry to count live browsers against its cap.
        Any of the four, not just the page: a close that got the page shut
        and then failed on the context has no page and a running Chromium,
        and that is still open.
        """
        return any(
            item is not None
            for item in (self._page, self._context, self._browser, self._playwright)
        )

    @property
    def close_failed(self) -> bool:
        """Whether the last close attempt left something unresolved."""
        return self._close_failed

    async def close(self) -> dict[str, Any]:
        """Close, or say that it did not. Retrying is allowed and is the
        only way out of a failed state -- there is no timer."""
        async with self._lock:
            await self._reset()
            return {"closed": True}

    async def _ensure_page(self) -> Any:
        self._begin_gated_action()
        if self._retired:
            # The registry closed this session's browser: a call that queued behind the close launches nothing.
            raise BrowserCloseFailed("this session's browser was closed; nothing was opened", reason="session_closed")
        self._ensure_dependency()
        launch_config = self._config_loader()
        # Configuration is rejected before Playwright or a browser process is
        # started.  The launcher validates again at the final argument seam so
        # no alternate launch path can bypass the check.
        _validated_spki_pins(launch_config.trusted_spki_sha256)
        if self._close_failed:
            # Nothing is built on top of a browser that may still be running.
            # An explicit close has to succeed first.
            raise BrowserCloseFailed(
                "The previous browser close did not complete; call browser_close "
                "again before opening a new page.",
                reason="close_pending",
            )
        if self._page is not None:
            if self._launch_config == launch_config:
                return self._page
            await self._reset()
        try:
            self._launch_started = True
            self._playwright = await async_playwright().start()
            self._browser, self._context, self._page = await self._launch_browser(launch_config)
            self._launch_config = launch_config
            return self._page
        except Exception as exc:
            try:
                await self._reset()
            except BrowserCloseFailed:
                # The launch error is the one to report; the failed reset has
                # already marked this runtime, and the next call refuses.
                pass
            raise RuntimeError(self._format_launch_error(exc, launch_config)) from exc

    async def _launch_browser(self, launch_config: BrowserLaunchConfig) -> tuple[Any, Any, Any]:
        mode = (launch_config.mode or "embedded").strip().lower()
        if mode == "chrome":
            return await self._launch_local_chrome(launch_config)
        if mode == "auto":
            chrome_error: Exception | None = None
            try:
                return await self._launch_local_chrome(launch_config)
            except Exception as exc:
                if self._context is not None or self._close_failed:
                    # A context whose policy installation failed must close before any fallback can launch.
                    raise
                chrome_error = exc
            try:
                return await self._launch_embedded_browser(launch_config)
            except Exception as embedded_exc:
                raise RuntimeError(
                    f"Auto mode could not launch local Chrome ({chrome_error}) "
                    f"or embedded Chromium ({embedded_exc})."
                ) from embedded_exc
        return await self._launch_embedded_browser(launch_config)

    # ── request-level egress (C26 / G02) ─────────────────────────────────

    def set_egress_hosts(self, hosts: Any) -> None:
        """What this browser may reach. Kept twice on purpose (P2 护栏一).

        An entry may now be an address rather than a whole site (``example.com/creator``). Two rules need
        different parts of it:

        * ``_egress_scope`` keeps the entries as written, and governs **navigation** -- the page the browser is
          moved to.
        * ``_egress_hosts`` is the host part alone, and governs **every request**, exactly as it always has. A
          page legitimately fetches its images, scripts and XHR from paths nobody named; judging those by path
          would break the site rather than protect anything.
        """
        from ampuly.computer_use.web_scope import host_of

        self._egress_scope = tuple(sorted({entry for item in list(hosts or [])
                                           if (entry := str(item or "").strip().lower().lstrip("."))}))
        self._egress_hosts = tuple(sorted({host for entry in self._egress_scope if (host := host_of(entry))}))

    def egress_report(self) -> dict[str, Any]:
        """What was blocked: host and resource type only, never a URL, header or body."""
        return {"policy": list(self._egress_hosts), "blocked": len(self._egress_blocked),
                "hosts": sorted({item["host"] for item in self._egress_blocked})}

    def request_report(self) -> dict[str, int]:
        """Transport facts relevant to a submitted write, redacted to counts."""
        return {"failed": len(self._request_failures), "pending_writes": self._writes_pending}

    def _note_request_failure(self, url: str, resource_type: str) -> None:
        if len(self._request_failures) < 500:
            self._request_failures.append({"host": (urlsplit(url).hostname or "").lower(),
                                           "resource_type": str(resource_type or "")})

    def _egress_allows(self, url: str) -> bool:
        scheme = (urlsplit(url).scheme or "").lower()
        if scheme in {"data", "blob", "about", "chrome-error"}:
            return True
        host = (urlsplit(url).hostname or "").lower()
        if not host or not self._egress_hosts:
            return False
        return any(host == allowed or host.endswith("." + allowed) for allowed in self._egress_hosts)

    def set_person_driving(self, on: bool) -> None:
        """Whether the owner has taken this session over (P2 护栏一).

        The pages the product never opens are the ones it must not open BY ITSELF. The whole reason that deny
        list can have no widening hatch is that a person can still do those pages -- 帮忙登录、短信验证、实名 are
        exactly the hand-offs the connector's `handoff: {captcha: human, login: human}` exists for, and the
        session holding the login is the only one that can finish them. So while a person drives, the delegated
        HOSTS still bind and the page rules step aside.

        Set true only on the console's own person-drives path, which runs only while the session is handed over;
        every agent action clears it again through `_apply_runtime_policies`, so it can never be left on.
        """
        self._person_driving = bool(on)

    def _navigation_allows(self, url: str) -> str:
        """``""`` when the browser may be MOVED to this address, else why not (P2 护栏一).

        Host first (what ``_egress_allows`` already said), then the part of the site that was named, then the
        pages no scope ever includes: the account's own security, the wallet, the cashier.
        """
        from ampuly.computer_use.web_scope import navigation_refusal

        if self._person_driving:
            return "" if self._egress_allows(url) else "off_site"
        return navigation_refusal(url, self._egress_scope or self._egress_hosts)

    def _note_blocked(self, url: str, resource_type: str) -> None:
        if len(self._egress_blocked) < 500:
            self._egress_blocked.append({"host": (urlsplit(url).hostname or "").lower(), "resource_type": str(resource_type or "")})

    def fence_writes(self, on: bool) -> None:
        """Temporarily restrict HTTP methods and new WebSocket connections.

        Refuses non-GET/HEAD/OPTIONS requests, including background POST/PATCH.
        An allowed method alone cannot establish that a site performs no write.
        """
        self._write_fence = bool(on)

    def set_verification_only(self, enabled: bool = True) -> None:
        """Server-only restriction set before launch, sticky for this runtime's lifetime.

        Restricts observable HTTP methods and new WebSocket connections, not
        every possible side effect: a site may write on GET, and browser startup
        before route installation and unrouteable transports remain outside it.
        A runtime already launched cannot be upgraded around existing sockets.
        """
        if type(enabled) is not bool:
            raise ValueError('verification_only must be a boolean')
        if not enabled or self._verification_only:
            return
        if self._launch_started or self.is_open or self._close_failed:
            raise RuntimeError('verification-only requires a fresh browser runtime')
        self._verification_only = True

    def _writes_are_fenced(self) -> bool:
        return self._verification_only or bool(getattr(self, '_write_fence', False))

    def writes_fenced(self) -> int:
        return int(getattr(self, "_fenced_writes", 0) or 0)

    def tab_urls(self) -> list[str]:
        """Every tab's full address, for checks inside the product (never shown to a model: tabs() is)."""
        return [str(getattr(page, "url", "") or "") for page in list(getattr(self, "_tabs", []) or [])]

    async def _egress_route(self, route: Any, request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        if not self._egress_allows(url):
            self._note_blocked(url, getattr(request, "resource_type", ""))
            await route.abort("blockedbyclient")
            return
        # P2 护栏一: the page the browser is being moved to is judged further than its host. Only navigations --
        # a page's own subresources keep the host rule above, which is the only rule they have ever had.
        if _is_navigation(request) and (refusal := self._navigation_allows(url)):
            self._note_blocked(url, refusal)
            await route.abort("blockedbyclient")
            return
        if self._writes_are_fenced() and (
            str(getattr(request, "method", "GET") or "GET").upper() not in {"GET", "HEAD", "OPTIONS"}
            or (urlsplit(url).scheme or "").lower() in {"ws", "wss"}
        ):
            self._fenced_writes = self.writes_fenced() + 1
            self._note_blocked(url, "write_fenced")
            await route.abort("blockedbyclient")
            return
        if (urlsplit(url).scheme or "").lower() in {"http", "https"}:
            # Playwright does not call the handler again for a redirected
            # request, so a 302 to an unlisted host would move the page -- or
            # load an image, a script, an XHR from it (seen on real Chromium,
            # 2026-09-13). Under a policy the runtime fetches every response
            # itself without following redirects and judges the Location
            # before the browser sees it; a redirect it allows is fulfilled
            # as-is, so the browser's follow-up is a fresh request that comes
            # back through here. Each hop costs one round trip through the
            # runtime; that is the price of a policy that holds at every hop.
            navigation = _is_navigation(request)
            try:
                response = await route.fetch(max_redirects=0)
            except Exception as exc:  # noqa: BLE001 - fail closed: no unguarded request
                failure = "tls_error" if _is_tls_error(exc) else ("navigation_fetch_failed" if navigation else "fetch_failed")
                self._note_blocked(url, failure)
                self._note_request_failure(url, failure)
                await route.abort("blockedbyclient")
                return
            status = int(getattr(response, "status", 0) or 0)
            if 300 <= status < 400:
                headers = getattr(response, "headers", {}) or {}
                location = str(headers.get("location", "") or headers.get("Location", "") or "")
                target = urljoin(url, location) if location else ""
                if target and not self._egress_allows(target):
                    self._note_blocked(target, "redirect")
                    await route.abort("blockedbyclient")
                    return
                # A 302 into the wallet is how a site would reach one without anybody clicking (P2 护栏一).
                if target and navigation and (refusal := self._navigation_allows(target)):
                    self._note_blocked(target, f"redirect_{refusal}")
                    await route.abort("blockedbyclient")
                    return
            await route.fulfill(response=response)
            return
        await route.continue_()

    async def _egress_web_socket(self, ws_route: Any) -> None:
        url = str(getattr(ws_route, "url", "") or "")
        if self._egress_allows(url):
            if self._writes_are_fenced():
                self._fenced_writes = self.writes_fenced() + 1
                self._note_blocked(url, "websocket_write_fenced")
                await ws_route.close(code=1008, reason="write fence")
                return
            ws_route.connect_to_server()
            return
        self._note_blocked(url, "websocket")
        await ws_route.close(code=1008, reason="egress policy")

    # ── downloads and popups (C18 / C29) ─────────────────────────────────

    def set_download_policy(self, *, allowed: bool, directory: Path | str | None, max_bytes: int | None = None,
                            types: Any = None, root: Path | str | None = None) -> None:
        """Whether, where, how large and which kinds. Size and type are checked on the saved file, and a
        file that fails is deleted and counted as refused (C18)."""
        self._downloads_allowed = bool(allowed)
        self._download_dir = Path(directory) if (allowed and directory) else None
        self._download_root = Path(root) if (allowed and root) else self._download_dir
        self._download_max_bytes = int(max_bytes) if max_bytes else DEFAULT_DOWNLOAD_MAX_BYTES
        self._download_types = normalized_types(types)

    def set_download_listener(self, listener: Callable[[str], Any] | None) -> None:
        """Who is told the product name of each download this runtime publishes; set by the bridge each call."""
        self._download_listener = listener

    def download_report(self) -> dict[str, Any]:
        return {"allowed": self._downloads_allowed, "saved": len([d for d in self._downloads if d.get("saved")]),
                "refused": len([d for d in self._downloads if not d.get("saved")]), "popups_closed": len(self._popups_closed),
                "tabs_adopted": len(self._popups_adopted)}

    async def _on_download(self, download: Any) -> None:
        """Save into the bound directory, or refuse; never into a browser temp dir or a user folder."""
        name = safe_filename(str(getattr(download, "suggested_filename", "") or "download"))
        record: dict[str, Any] = {"name": name[:120], "saved": False}
        quarantine: Path | None = None
        staged: Path | None = None
        if not self._downloads_allowed or self._download_dir is None:
            record["reason"] = "downloads_not_allowed_for_this_session"
            self._downloads.append(record)
            try:
                await download.cancel()
            except Exception:  # noqa: BLE001 - a cancel that fails still saved nothing of ours
                pass
            return
        allowed, reason, suffix = preflight_type(name, self._download_types)
        if not allowed:
            record["reason"] = reason
            self._downloads.append(record)
            try:
                await download.cancel()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            prepare_confined_directory(self._download_dir, self._download_root or self._download_dir)
            # The runtime-internal prefix: a step's file tools can neither list nor read bytes that were never
            # inspected, and the staging stays on the folder's own volume for the publish link (data phase 1 review).
            quarantine = self._download_dir / f".ampuly-write-quarantine-{self._quarantine_tag}"
            quarantine.mkdir(exist_ok=True)
            if quarantine.is_symlink() or getattr(quarantine, "is_junction", lambda: False)():
                raise ValueError("download quarantine is redirected")
            staged = quarantine / f"{uuid.uuid4().hex}.part"
            await download.save_as(str(staged))
            inspection = inspect_quarantined(staged, suffix=suffix, max_bytes=self._download_max_bytes)
            if not inspection.get("ok"):
                staged.unlink(missing_ok=True)
                record.update({key: value for key, value in inspection.items() if key != "ok"})
            else:
                published_name = deliverable_filename(suffix, str(inspection["sha256"]))
                target = publish_without_overwrite(
                    staged, self._download_dir, published_name, str(inspection["sha256"])
                )
                record.update({"saved": True, "path": str(target), **{key: value for key, value in inspection.items() if key != "ok"}})
                listener = self._download_listener
                if listener is not None:
                    try:
                        listener(Path(target).name)
                    except Exception:  # noqa: BLE001 - the journal note never undoes a saved download
                        pass
        except Exception as exc:  # noqa: BLE001 - reported, never raised into a page event handler
            try:
                if staged is not None:
                    staged.unlink(missing_ok=True)
            except Exception:
                pass
            record["reason"] = f"save_failed:{type(exc).__name__}"
        finally:
            # The quarantine is an implementation detail.  Remove it whenever
            # this handler leaves it empty, so callers never mistake it for a
            # published artifact and refused downloads leave no residue.
            if quarantine is not None:
                try:
                    quarantine.rmdir()
                except OSError:
                    pass
        self._downloads.append(record)

    async def _on_popup(self, page: Any) -> None:
        """A page the page opened. On an allowed host it becomes a tab of this session (the request
        policy already governs what it may load); anywhere else it is closed on sight and counted."""
        url = str(getattr(page, "url", "") or "")
        if not (urlsplit(url).hostname or ""):
            # A page that has not navigated yet has no origin to judge; give its
            # first navigation a moment, then judge what it became.
            waiter = getattr(page, "wait_for_load_state", None)
            if callable(waiter):
                try:
                    await waiter("domcontentloaded", timeout=1500)
                except Exception:  # noqa: BLE001 - an aborted or slow navigation is judged as it stands
                    pass
            url = str(getattr(page, "url", "") or "")
        host = (urlsplit(url).hostname or "").lower()
        # Judged by host alone: the request-level exceptions for about:/data:/chrome-error:
        # do not apply to a whole page. Under a policy, a page with no origin -- including
        # the error page an aborted navigation leaves behind -- is not ours to keep (fail closed).
        # P2 护栏一: a popup is a page, so it is judged as a navigation -- the named part of the site, and
        # never one of the pages no scope includes. The scheme is required to be a real one here rather than
        # left to the navigation rule, which exempts data:/about:/chrome-error: as the request rule does; for a
        # whole page those are not a place, and this gate has always failed closed on them.
        listed = (bool(host) and (urlsplit(url).scheme or "").lower() in {"http", "https"}
                  and not self._navigation_allows(url))
        if not listed:
            self._popups_closed.append({"host": host})
            try:
                await page.close()
            except Exception:  # noqa: BLE001 - a popup that will not close is still not ours to drive
                pass
            return
        if page not in self._tabs:
            self._tabs.append(page)
        self._popups_adopted.append({"host": host})

    def tabs(self) -> list[dict[str, Any]]:
        """The session's tabs as the model may see them: index, host and path, whether active."""
        rows = []
        for index, page in enumerate(self._tabs):
            url = str(getattr(page, "url", "") or "")
            parts = urlsplit(url)
            rows.append({"index": index, "page": f"{parts.hostname or ''}{parts.path or ''}", "active": page is self._page})
        return rows

    async def switch_tab(self, index: int) -> dict[str, Any]:
        """Make one of this session's tabs the page the tools act on. A tab that is gone is refused."""
        async with self._lock:
            self._begin_gated_action()
            live = [p for p in self._tabs if not getattr(p, "is_closed", lambda: False)()]
            self._tabs = live
            try:
                page = live[int(index)]
            except (IndexError, ValueError, TypeError):
                raise BrowserElementNotFound(f"No tab at index {index!r}; the session has {len(live)} tab(s).") from None
            self._page = page
            if self._action_gate is not None:
                self._action_gate.input_sent = True
            try:
                await page.bring_to_front()
            except Exception:  # noqa: BLE001 - a headless page has no front
                pass
            await self._follow_page_with_screencast()
            return {"active": int(index), "tabs": self.tabs()}

    async def open_tab(self, url: str) -> dict[str, Any]:
        """A new tab of this session at `url`, made the active page. The caller has judged the host
        (the registry's domain gate); the request-level policy governs what the tab may load."""
        async with self._lock:
            await self._require_page()
            context = self._context
            if context is None:
                raise RuntimeError("No browser context to open a tab in.")
            page = await context.new_page()
            if page not in self._tabs:
                self._tabs.append(page)
            self._page = page
            listener = getattr(page, "on", None)
            if callable(listener):
                try:
                    listener("download", self._on_download)
                except Exception:  # noqa: BLE001 - a page that takes no listener still gets the context's policy
                    pass
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            await self._follow_page_with_screencast()
            return {"active": self._tabs.index(page), "tabs": self.tabs()}

    async def close_tab(self, index: int) -> dict[str, Any]:
        """Close one tab of this session; the last tab stays (closing the session is browser_close)."""
        async with self._lock:
            self._begin_gated_action()
            live = [p for p in self._tabs if not getattr(p, "is_closed", lambda: False)()]
            self._tabs = live
            if len(live) <= 1:
                raise RuntimeError("The last tab stays open; close the session instead.")
            try:
                page = live[int(index)]
            except (IndexError, ValueError, TypeError):
                raise BrowserElementNotFound(f"No tab at index {index!r}; the session has {len(live)} tab(s).") from None
            was_active = page is self._page
            try:
                await page.close()
            except Exception:  # noqa: BLE001 - a tab that will not close is dropped from the list all the same
                pass
            self._tabs = [p for p in live if p is not page]
            if was_active:
                self._page = self._tabs[-1]
                await self._follow_page_with_screencast()
            return {"active": self._tabs.index(self._page), "tabs": self.tabs()}

    async def _follow_page_with_screencast(self) -> None:
        """X05: the live view is a CDP session on one page; after a tab switch it is re-opened on the new one."""
        if self._cdp is None:
            return
        callback = self._screencast_on_frame
        await self.stop_screencast()
        try:
            await self._cdp.detach()
        except Exception:  # noqa: BLE001 - an old session that will not detach is left to the browser
            pass
        self._cdp = None
        if callback is not None:
            await self.start_screencast(callback)

    def _install_page_policies(self, context: Any, page: Any) -> None:
        self._tabs = [page]
        for holder, event, handler in ((context, "page", self._on_popup), (page, "download", self._on_download)):
            listen = getattr(holder, "on", None)
            if callable(listen):
                listen(event, handler)
        def is_write(request: Any) -> bool:
            method = str(getattr(request, "method", "GET") or "GET").upper()
            return method not in {"GET", "HEAD"} or _is_navigation(request)

        def request_started(request: Any) -> None:
            if is_write(request):
                self._writes_pending += 1

        def request_finished(request: Any) -> None:
            if is_write(request):
                self._writes_pending = max(0, self._writes_pending - 1)

        def request_failed(request: Any) -> None:
            if is_write(request):
                self._writes_pending = max(0, self._writes_pending - 1)
            failure = str(getattr(request, "failure", "") or "")
            if "ERR_BLOCKED_BY_CLIENT" not in failure:
                self._note_request_failure(str(getattr(request, "url", "") or ""), str(getattr(request, "resource_type", "") or ""))

        listen = getattr(context, "on", None)
        if callable(listen):
            for event, handler in (("request", request_started), ("requestfinished", request_finished), ("requestfailed", request_failed)):
                listen(event, handler)

    async def _install_egress(self, context: Any) -> None:
        """Every request through the context passes the policy; a context without
        routing (a test double) is left as it is unless verification is restricted."""
        try:
            router = getattr(context, "route", None)
            ws_router = getattr(context, "route_web_socket", None)
            if self._verification_only and (not callable(router) or not callable(ws_router)):
                raise RuntimeError('verification-only requires HTTP and WebSocket routing')
            if callable(router):
                await router("**/*", self._egress_route)
            if callable(ws_router):
                await ws_router("**/*", self._egress_web_socket)
        except BaseException as exc:
            # Launch has not returned its resources to _ensure_page yet. Keep
            # them visible to its cleanup, and never open an auto-mode fallback
            # on top of this context. A cancelled launch awaits explicit close.
            self._context = context
            self._browser = getattr(context, "browser", None)
            if isinstance(exc, asyncio.CancelledError):
                self._close_failed = True
            raise

    async def _launch_embedded_browser(self, launch_config: BrowserLaunchConfig) -> tuple[Any, Any, Any]:
        headless = launch_config.headless and not self._visible
        profile = (
            self._normalize_user_data_dir(launch_config.user_data_dir)
            if not headless or launch_config.persistent_profile else ""
        )
        if profile:
            # X02 / C26: persistent contexts use full Chromium, which needs the
            # profile policy as well as the WebRTC switch. Account profiles are
            # retained on task close; only legacy task profiles may be purged.
            _close_webrtc_udp_in_profile(profile)
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                headless=headless,
                args=self._embedded_launch_args(launch_config),
                ignore_https_errors=False,
                accept_downloads=self._downloads_allowed,
            )
            await self._install_egress(context)
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else await context.new_page()
            self._install_page_policies(context, page)
            return getattr(context, "browser", None), context, page
        browser = await self._playwright.chromium.launch(
            headless=headless,
            args=self._embedded_launch_args(launch_config),
        )
        context = await browser.new_context(ignore_https_errors=False, accept_downloads=self._downloads_allowed)
        await self._install_egress(context)
        page = await context.new_page()
        self._install_page_policies(context, page)
        return browser, context, page

    async def _launch_local_chrome(self, launch_config: BrowserLaunchConfig) -> tuple[Any, Any, Any]:
        launch_kwargs: dict[str, Any] = {"headless": launch_config.headless and not self._visible}
        launch_kwargs["args"] = _with_egress_launch_args(launch_config.args, launch_config.trusted_spki_sha256)
        executable_path = self._normalize_executable_path(launch_config.chrome_executable_path)
        user_data_dir = self._normalize_user_data_dir(launch_config.user_data_dir)
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        else:
            launch_kwargs["channel"] = launch_config.chrome_channel or "chrome"
        if user_data_dir:
            _close_webrtc_udp_in_profile(user_data_dir)
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                ignore_https_errors=False,
                accept_downloads=self._downloads_allowed,
                **launch_kwargs,
            )
            await self._install_egress(context)
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else await context.new_page()
            self._install_page_policies(context, page)
            browser = getattr(context, "browser", None)
            return browser, context, page
        browser = await self._playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(ignore_https_errors=False, accept_downloads=self._downloads_allowed)
        await self._install_egress(context)
        page = await context.new_page()
        self._install_page_policies(context, page)
        return browser, context, page

    def _embedded_launch_args(self, launch_config: BrowserLaunchConfig) -> list[str]:
        args = list(_EMBEDDED_BROWSER_ARGS)
        for item in launch_config.args:
            if item not in args:
                args.append(item)
        return _with_egress_launch_args(args, launch_config.trusted_spki_sha256)

    def _normalize_executable_path(self, raw_path: str) -> str:
        raw = (raw_path or "").strip()
        if not raw:
            return ""
        return str(Path(raw).expanduser())

    def _normalize_user_data_dir(self, raw_path: str) -> str:
        raw = (raw_path or "").strip()
        if not raw:
            return ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = get_ampuly_home().parent / path
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _format_launch_error(self, exc: Exception, launch_config: BrowserLaunchConfig) -> str:
        if (launch_config.mode or "").strip().lower() == "chrome":
            return (
                f"Failed to start local Chrome browser: {exc}. "
                "Check `system.browser.chrome_executable_path`, `system.browser.user_data_dir`, "
                "or switch `system.browser.mode` back to `embedded`."
            )
        return f"Failed to start browser runtime: {exc}. {_INSTALL_HINT}"

    async def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError('No open browser page. Use `browser_navigate` first.')
        self._begin_gated_action()
        return self._page

    def _begin_gated_action(self) -> None:
        """Called with the lock held, as an action starts: under an agent's guard, this action's input is gated."""
        gate = _dispatch_gate()
        self._action_gate = gate
        if gate is not None:
            gate.check()

    def structured_path_available(self) -> bool:
        """C03: the last structured snapshot of the page this runtime is on offered elements to act on."""
        page = self._page
        url = str(getattr(page, "url", "") or "") if page is not None else ""
        return bool(self._last_elements) and bool(self.last_snapshot_digest) and url == self._last_snapshot_url

    def page_geometry(self) -> dict[str, Any] | None:
        """What a screen coordinate is measured against: the page's viewport. None when unknown (fail closed)."""
        page = self._page
        size = getattr(page, "viewport_size", None) if page is not None else None
        if not size or not size.get("width") or not size.get("height"):
            return None
        return {"width": int(size["width"]), "height": int(size["height"]),
                "dpi_scale": float(getattr(page, "device_scale_factor", 1.0) or 1.0)}

    async def element_at(self, x: float, y: float) -> dict[str, Any] | None:
        """The topmost element at a viewport coordinate, right now, as {tag,type,text,role,placeholder}.

        X03 / C30: a vision coordinate is admitted only after this says what sits under it, so a control the
        page slid there after the screenshot -- or an overlay drawn over it -- is what gets classified, not
        what the picture showed. Walks up to the nearest actionable ancestor. None when there is nothing there.
        """
        async with self._lock:
            page = await self._require_page()
            raw = await page.evaluate(_ELEMENT_AT_SCRIPT, [float(x), float(y)])
        if not isinstance(raw, dict):
            return None
        # Described like a click by ref (the control it activates, whether it submits which form); plus whether the
        # point is inside a frame or a closed widget this document cannot describe.
        return {**_described_target(raw), "frame": raw.get("frame") is True, "opaque": raw.get("opaque") is True}

    async def focused_element(self) -> dict[str, Any] | None:
        """What keyboard input would go to right now: document.activeElement, through open shadow roots and same-origin
        frames. `opaque` when focus is inside a frame this document cannot read. Never a value (C27)."""
        async with self._lock:
            page = await self._require_page()
            return await page.evaluate(
                """
                () => {
                  let el = document.activeElement;
                  let opaque = false;
                  for (let depth = 0; el && depth < 8; depth++) {
                    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
                    const tag = (el.tagName || "").toLowerCase();
                    if (tag === "iframe" || tag === "frame") {
                      let doc = null;
                      try { doc = el.contentDocument; } catch (error) { doc = null; }
                      if (!doc || !doc.activeElement) { opaque = true; break; }
                      el = doc.activeElement;
                      continue;
                    }
                    break;
                  }
                  if (!el) return null;
                  const tag = (el.tagName || "").toLowerCase();
                  const idlType = tag === "input" ? String(el.type || "").toLowerCase() : "";
                  const typed = ["text", "search", "email", "url", "tel", "password", "number"];
                  const editable = el.disabled !== true && (tag === "textarea" || el.isContentEditable === true || (tag === "input" && typed.includes(idlType)));
                  return { tag, type: idlType, role: ((el.getAttribute && el.getAttribute("role")) || "").toLowerCase(), editable, opaque };
                }
                """
            )

    async def perform_vision_action(self, action: Any) -> dict[str, Any]:
        """Perform one grounded action at screen coordinates the Gateway admitted (X03 fourth slice).

        The coordinates come from the parsed model reply, already in viewport
        pixels; the text of a `type` stays out of the result (C27).
        """
        async with self._lock:
            page = await self._require_page()
            function = str(getattr(action, "function", "") or "")
            start, end = getattr(action, "start", None), getattr(action, "end", None)
            inputs = dict(getattr(action, "inputs", {}) or {})
            mouse, keyboard = page.mouse, page.keyboard
            gate = self._action_gate

            def send() -> None:
                # Each piece of input asks first (G04); after the first, a lost control is an unknown outcome.
                if gate is not None:
                    gate.check()
                    gate.input_sent = True

            if function in {"click", "left_double", "right_single"}:
                x, y = start
                send()
                await mouse.click(x, y, button="right" if function == "right_single" else "left",
                                  click_count=2 if function == "left_double" else 1)
                return {"performed": function, "at": [round(x), round(y)]}
            if function == "drag":
                send()
                await mouse.move(*start)
                send()
                await mouse.down()
                # Once the button is down the drag completes as one unit: a button left pressed on a page a person now
                # holds would drag whatever it grabbed on their first move.
                try:
                    await mouse.move(*end)
                finally:
                    await mouse.up()
                return {"performed": function, "from": [round(start[0]), round(start[1])], "to": [round(end[0]), round(end[1])]}
            if function == "scroll":
                direction = str(inputs.get("direction", "down") or "down").lower()
                dx, dy = {"down": (0, 400), "up": (0, -400), "right": (400, 0), "left": (-400, 0)}.get(direction, (0, 400))
                send()
                await mouse.move(*start)
                await mouse.wheel(dx, dy)
                return {"performed": function, "direction": direction}
            if function == "type":
                content = str(inputs.get("content", "") or "")
                for piece in [content[start:start + 8] for start in range(0, len(content), 8)] or [content]:
                    send()
                    await keyboard.type(piece)
                return {"performed": function, "length": len(content)}
            if function == "hotkey":
                combo = _playwright_hotkey(str(inputs.get("key", "") or ""))
                send()
                await keyboard.press(combo)
                return {"performed": function, "hotkey": combo}
            if function == "wait":
                await asyncio.sleep(1.0)
                return {"performed": function, "seconds": 1}
            raise RuntimeError(f"vision action {function!r} has no executor")  # the Gateway refused it before this

    async def target_count(self, selector: str) -> int:
        """How many elements this target names right now, in its own frame (an approval binds to exactly one)."""
        async with self._lock:
            page = await self._require_page()
            raw = self.resolve_ref(selector)
            if not raw:
                return 0
            try:
                return int(await self._frame_for(page, selector).locator(raw).count())
            except (PlaywrightError, BrowserElementNotFound):
                return 0

    async def target_count_strict(self, selector: str) -> int:
        """Like target_count, but a query that fails raises (Q05 B5): for the done check a failed read is "could not
        tell", never "not there" -- which would make a baseline looser and a check pass by accident."""
        async with self._lock:
            page = await self._require_page()
            raw = self.resolve_ref(selector)
            if not raw:
                raise BrowserElementNotFound(f"{selector!r} names nothing this page can resolve")
            return int(await self._frame_for(page, selector).locator(raw).count())

    def saved_downloads(self) -> list[dict[str, Any]]:
        """The downloads this runtime itself inspected and published (Q05 B5): their path, full digest and size at save
        time. A file that only looks like one of them (a name in the same directory) is not."""
        return [{"path": str(d.get("path", "") or ""), "sha256": str(d.get("sha256", "") or ""), "bytes": d.get("bytes")}
                for d in list(getattr(self, "_downloads", []) or []) if d.get("saved") and d.get("path") and d.get("sha256")]

    async def form_state(self, selector: str) -> dict[str, Any]:
        """The values the target's form would send if it were clicked now (or, with no form, the document's fields):
        [{name, value, sensitive}]. Read to bind an approval to the values the owner saw, never logged here."""
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            raw = await locator.evaluate(_FORM_STATE_SCRIPT)
        return form_state_of(raw)

    async def describe_target(self, selector: str) -> dict[str, Any]:
        """What a target is right now, read from the live element the action would use (C08).

        The same frame and the same first match the action resolves, so what is classified is what gets the input,
        not a snapshot row matched by spelling. Never an input's value: a typed password stays out (C27). Raises
        BrowserElementNotFound when nothing matches.
        """
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            described = await locator.evaluate(_DESCRIBE_TARGET_SCRIPT)
        return _described_target(described)

    def element_for(self, selector: str) -> dict[str, Any] | None:
        """What the last snapshot knew about this target (by ref or selector), or nothing."""
        raw = str(selector or "").strip()
        if raw.startswith("ref:"):
            ref = raw[4:].strip()
            return next((dict(e) for e in self._last_elements if e.get("ref") == ref), None)
        return next((dict(e) for e in self._last_elements if e.get("selector") == raw), None)

    def resolve_ref(self, selector: str) -> str:
        """`ref:<id>` names an element of the last snapshot; anything else is a selector as given."""
        raw = str(selector or "").strip()
        if not raw.startswith("ref:"):
            return raw
        ref = raw[4:].strip()
        resolved = self._refs.get(ref)
        if not resolved:
            raise BrowserElementNotFound(f"Unknown element ref `{ref}`; take a new browser_snapshot and use a ref from it.")
        return resolved

    def ref_frame_url(self, selector: str) -> str | None:
        """Frame of a current snapshot ref; None means the ref is unknown.

        An ordinary selector or a known main-page ref has an empty frame URL.
        Reading this mapping neither refreshes nor manufactures a snapshot.
        """
        raw = str(selector or "").strip()
        if not raw.startswith("ref:"):
            return ""
        ref = raw[4:].strip()
        if ref not in self._refs:
            return None
        frame = self._ref_frames.get(ref, "")
        return frame if isinstance(frame, str) and len(frame) <= 8192 else None

    def _frame_for(self, page: Any, selector: str) -> Any:
        """The child frame a `ref:<id>` came from, or the page itself."""
        raw = str(selector or "").strip()
        if not raw.startswith("ref:"):
            return page
        frame_url = self._ref_frames.get(raw[4:].strip(), "")
        if not frame_url:
            return page
        for frame in list(getattr(page, "frames", []) or []):
            if str(getattr(frame, "url", "") or "") == frame_url:
                return frame
        raise BrowserElementNotFound("The frame that element was in is gone; take a new browser_snapshot.")

    async def _resolve_locator(self, page: Any, selector: str) -> Any:
        raw = self.resolve_ref(selector)
        if not raw:
            raise RuntimeError("Selector must not be empty.")
        target = self._frame_for(page, selector)
        try:
            locator = target.locator(raw).first
            count = await locator.count()
            if count >= 1 and str(selector or "").strip().startswith("ref:"):
                # A ref names one element of the snapshot; if its selector now matches several, which one is unknown.
                count = await target.locator(raw).count()
                if count > 1:
                    raise BrowserElementNotFound(f"The ref no longer names one element ({count} match); take a new browser_snapshot.")
        except PlaywrightError as exc:
            raise RuntimeError(f"Invalid selector `{raw}`: {exc}") from exc
        if count < 1:
            raise BrowserElementNotFound(f"No element matched selector `{raw}`.")
        gate = self._action_gate
        return _GatedLocator(locator, gate) if gate is not None else locator

    async def _page_fingerprint(self, page: Any) -> dict[str, Any]:
        """The address, the page's structure and its scroll position: what a coordinate taken from a picture
        of the page depends on. Callers hold the lock. A scroll position that cannot be read is None."""
        payload = await self._read_document(page)
        interactive = _with_stable_refs(list(payload.get("interactive", []) or []))
        url = str(payload.get("url", "") or getattr(page, "url", "") or "")
        scroll: list[int] | None = None
        try:
            raw = await page.evaluate("() => [window.scrollX, window.scrollY]")
            if isinstance(raw, (list, tuple)) and len(raw) == 2:
                scroll = [round(float(raw[0])), round(float(raw[1]))]
        except Exception:  # noqa: BLE001 - a position that cannot be read is compared as unknown
            scroll = None
        return {"url": str(getattr(page, "url", "") or url), "structure": _structure_digest(url, list(payload.get("headings", []) or []), interactive),
                "scroll": scroll}

    async def api_read(self, url: str, *, timeout_ms: int = 10_000, max_bytes: int = 1_000_000) -> dict[str, Any]:
        """One GET with this session's cookies, for an API back-check after a submit (X06).

        A request context does not go through the page's routes, so this runtime's egress policy is held
        here by hand. Redirects are not followed (a hop is not judged, so none is taken); a body larger
        than `max_bytes` is not parsed. Returns {"status", "json"}; nothing of the body is kept elsewhere.
        """
        async with self._lock:
            await self._require_page()
            if (urlsplit(str(url or "")).scheme or "").lower() not in {"http", "https"} or not self._egress_allows(url):
                self._note_blocked(str(url or ""), "api_read")
                raise PermissionError("egress_not_allowed")
            request = getattr(self._context, "request", None)
            if request is None:
                raise RuntimeError("this browser context has no request API")
            response = await request.get(url, max_redirects=0, timeout=timeout_ms, fail_on_status_code=False)
            status = int(getattr(response, "status", 0) or 0)
            body = await response.body()
            if len(body) > max_bytes:
                return {"status": status, "json": None, "too_large": True}
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = None
            return {"status": status, "json": data}

    async def observation_fingerprint(self) -> dict[str, Any]:
        """The page's fingerprint right now (X03): compared with the one its last screenshot carries."""
        async with self._lock:
            page = await self._require_page()
            return await self._page_fingerprint(page)

    async def probe_account_login(self, policy: Any) -> dict[str, Any]:
        """Read server-configured visible markers; never navigate or grant control.

        The registry binds these facts to its account and control incarnation.
        Existing observation fingerprints are compared, but only their hash is
        returned: neither the page URL nor page text is login evidence here.
        """
        from ampuly.layer4_tools.browser_account_status import (
            LoginProbePolicy, fingerprint_digest, interpret_login_flags, page_host,
        )

        if not isinstance(policy, LoginProbePolicy):
            raise ValueError('a trusted LoginProbePolicy is required')
        facts: dict[str, Any] = {
            'host': '', 'host_matches': False, 'fingerprint': '',
            'flags': {'authenticated': None, 'login_required': None, 'challenge': None}, 'result': 'unverified',
        }
        page = self._page
        if page is None:
            return facts
        facts['result'] = 'unknown'
        if self._retired or self._close_failed:
            return facts
        timeout = policy.timeout_ms / 1000

        async def visible_flags() -> dict[str, bool]:
            async with self._lock:
                if await self._require_page() is not page:
                    raise RuntimeError('probe page changed')
                flags: dict[str, bool] = {}
                for name, selector in (('authenticated', policy.authenticated_selector),
                                       ('login_required', policy.login_required_selector),
                                       ('challenge', policy.challenge_selector)):
                    self._begin_gated_action()
                    visible = await asyncio.wait_for(
                        page.locator(f'css={selector}').is_visible(timeout=policy.timeout_ms), timeout,
                    ) if selector else False
                    if type(visible) is not bool:
                        raise RuntimeError('probe returned an invalid visibility flag')
                    flags[name] = visible
                return flags

        try:
            before = await asyncio.wait_for(self.observation_fingerprint(), timeout)
            before_digest = fingerprint_digest(before)
            facts['host'] = page_host(before.get('url')) if isinstance(before, dict) else ''
            facts['host_matches'] = facts['host'] == policy.site_host
            if (not before_digest or not facts['host_matches'] or self._page is not page
                    or page_host(getattr(page, 'url', '')) != policy.site_host):
                return facts
            flags = await asyncio.wait_for(visible_flags(), timeout * 4)
            after = await asyncio.wait_for(self.observation_fingerprint(), timeout)
            if (self._page is not page or self._retired or self._close_failed
                    or page_host(getattr(page, 'url', '')) != policy.site_host
                    or fingerprint_digest(after) != before_digest):
                return facts
            facts['fingerprint'] = before_digest
            facts['flags'] = flags
            facts['result'] = interpret_login_flags(**flags)
            return facts
        except Exception:  # noqa: BLE001 - neither provider text nor an incomplete observation is evidence
            return facts

    async def structure_digest(self) -> str:
        """The digest a fresh snapshot of this page would carry, without taking one (C05).

        Refs and the remembered snapshot stay as the model saw them; the
        Gateway bridge compares this with that snapshot's digest before an
        action decided on it is sent.
        """
        async with self._lock:
            page = await self._require_page()
            payload = await self._read_document(page)
            interactive = _with_stable_refs(list(payload.get("interactive", []) or []))
            return _structure_digest(str(payload.get("url", "") or page.url), list(payload.get("headings", []) or []), interactive)

    async def _read_page(self, page: Any) -> dict[str, Any]:
        return await page.evaluate(
            """
            () => {
              const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const clip = (value, limit) => {
                const text = normalize(value);
                return text.length > limit ? text.slice(0, limit) + "..." : text;
              };
              // A ref names one element: a selector that matches several (a 退款 button on every row) would approve
              // one row and click the first. Unique semantic selectors first, then a structural path, else none.
              const unique = (selector, el) => {
                try {
                  const found = document.querySelectorAll(selector);
                  return found.length === 1 && found[0] === el;
                } catch (error) {
                  return false;
                }
              };
              const pathFor = (el) => {
                const parts = [];
                let at = el;
                for (; at && at.nodeType === 1 && at !== document.documentElement; at = at.parentElement) {
                  if (parts.length >= 64) return "";
                  if (at.id && unique(`#${CSS.escape(at.id)}`, at)) {
                    parts.unshift(`#${CSS.escape(at.id)}`);
                    return parts.join(" > ");
                  }
                  let index = 1;
                  for (let sibling = at.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
                    if (sibling.tagName === at.tagName) index += 1;
                  }
                  parts.unshift(`${at.tagName.toLowerCase()}:nth-of-type(${index})`);
                }
                // Only a walk that reached the document element is rooted at html.
                return parts.length && at === document.documentElement ? `html > ${parts.join(" > ")}` : "";
              };
              // Each tag's texts are read once per snapshot: a page of 300 rows asked the whole document once per row.
              const textsByTag = {};
              const textsOf = (tag) => textsByTag[tag]
                || (textsByTag[tag] = Array.from(document.querySelectorAll(tag)).map((o) => normalize(o.innerText || o.textContent).toLowerCase()));
              const makeSelector = (el) => {
                const tag = (el.tagName || "div").toLowerCase();
                const inShadow = el.getRootNode && el.getRootNode() !== document;
                const candidates = [];
                if (el.id) candidates.push(`#${CSS.escape(el.id)}`);
                const name = el.getAttribute("name");
                if (name) candidates.push(`${tag}[name="${name.replace(/"/g, '\\"')}"]`);
                const aria = el.getAttribute("aria-label");
                if (aria) candidates.push(`${tag}[aria-label="${aria.replace(/"/g, '\\"')}"]`);
                const placeholder = el.getAttribute("placeholder");
                if (placeholder) candidates.push(`${tag}[placeholder="${placeholder.replace(/"/g, '\\"')}"]`);
                if (inShadow) {
                  // The document cannot count inside a shadow root; the action counts through it and refuses a duplicate.
                  return candidates[0] || "";
                }
                for (const candidate of candidates) {
                  if (unique(candidate, el)) return candidate;
                }
                const text = normalize(el.innerText || el.textContent);
                if (text && text.length <= 60 && (tag === "button" || tag === "a")) {
                  const wanted = text.toLowerCase();
                  const same = textsOf(tag).filter((other) => other.includes(wanted)).length;
                  if (same === 1) return `${tag}:has-text("${text.replace(/"/g, '\\"')}")`;
                }
                const path = pathFor(el);
                return path && unique(path, el) ? path : "";
              };

              // Q05 B1: every kind of control a person operates -- links, buttons, fields, lists, disclosure, editable
              // regions and the ARIA widgets -- in document order, an open shadow root's controls right after its host
              // (so a per-frame cap cuts the end of the page, not every widget). The walk inside shadow roots is bounded.
              // The order and the old fields are what refs and the page digest are made of (C05), so a scroll or a lazy
              // row never reorders them.
              const SELECTOR = 'a, button, input, textarea, select, summary, [contenteditable]:not([contenteditable="false" i]), '
                + '[role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], '
                + '[role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="option"], [role="combobox"], '
                + '[role="textbox"], [role="searchbox"], [role="slider"], [role="spinbutton"], [role="treeitem"]';
              const PER_FRAME = 250;
              const found = [];
              const seen = new Set();
              let visited = 0;
              let shadowCut = false;
              const collect = (root, inShadow) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll("*")) {
                  if (inShadow) {
                    visited += 1;
                    if (visited > 20000) { shadowCut = true; return; }
                  }
                  if (el.matches(SELECTOR) && !seen.has(el)) { seen.add(el); found.push(el); }
                  if (el.shadowRoot) collect(el.shadowRoot, true);
                }
              };
              collect(document, false);
              // Not controls: whatever sits inside an editable region is someone's content (a mention chip, a link they
              // pasted, C27), and an aria-hidden or inert subtree cannot be operated. Counted, never listed.
              const editableRoot = '[contenteditable]:not([contenteditable="false" i])';
              let inEditable = 0;
              const shown = found.filter((el) => {
                const style = window.getComputedStyle(el);
                if (!style || style.display === "none" || style.visibility === "hidden") return false;
                const parent = el.parentElement;
                if (parent && parent.closest && parent.closest(editableRoot)) { inEditable += 1; return false; }
                return true;
              });
              // The row around a control (a table row, a list item, a card), as a short hash: an action on a ref is
              // refused when its row now says something else (a list that moved under a structural selector).
              const hashOf = (text) => {
                let h = 5381;
                for (let i = 0; i < text.length; i += 1) h = ((h * 33) ^ text.charCodeAt(i)) >>> 0;
                return h.toString(16);
              };
              const contextOf = (el) => {
                const box = el.closest ? el.closest('tr, li, [role="row"], [role="listitem"], article, [role="article"]') : null;
                return box ? hashOf(normalize(box.innerText || box.textContent).slice(0, 300)) : "";
              };
              // What a field holds is never read out: only whether it is empty, filled, or a secret (the same rule the
              // form reader uses). A text control's own text is what someone typed, so it carries none (C27).
              const identifying = /pass|pwd|token|secret|api.?key|otp|captcha|cvv|cvc|(sms|verify|verification|auth)[\\s_-]*code|验证码|校验码|动态码|密码|口令|卡号|身份证|证件号/i;
              const labelOf = (el, isTyping) => {
                const attr = (name) => (el.getAttribute && el.getAttribute(name)) || "";
                let label = attr("aria-label");
                if (!label && attr("aria-labelledby")) {
                  label = attr("aria-labelledby").split(/\\s+/).map((id) => {
                    const ref = document.getElementById(id);
                    return ref ? (ref.innerText || ref.textContent || "") : "";
                  }).join(" ");
                }
                if (!label && el.labels && el.labels.length) label = Array.from(el.labels).map((l) => l.innerText || l.textContent || "").join(" ");
                // A text control is named from outside only: its title, an image in it, a mention chip are its content.
                if (isTyping) return clip(label, 120);
                if (!label) label = attr("title") || attr("alt");
                if (!label && el.querySelector) {
                  const img = el.querySelector("img[alt], svg[aria-label], [title]");
                  if (img) label = img.getAttribute("alt") || img.getAttribute("aria-label") || img.getAttribute("title") || "";
                }
                return clip(label, 120);
              };
              const typing = (el, tag, role) => tag === "textarea" || el.isContentEditable === true || role === "textbox" || role === "searchbox"
                || (tag === "input" && !["checkbox", "radio", "button", "submit", "reset", "image", "file", "range", "color", "hidden"]
                  .includes(String(el.type || "").toLowerCase()));
              const stateOf = (el, tag, role) => {
                const attr = (name) => (el.getAttribute && el.getAttribute(name)) || "";
                const seen = (e) => {
                  const r = e && e.getBoundingClientRect ? e.getBoundingClientRect() : null;
                  const ok = Boolean(r) && (e.checkVisibility ? e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true)
                    && r.width > 0 && r.height > 0;
                  return { ok, r };
                };
                let look = seen(el);
                let viaLabel = false;
                // A UI kit's checkbox, radio or list is an invisible native control operated through what draws it:
                // judged where its label (or the wrapper around it) is -- and marked, since clicking the control
                // itself would wait on something Playwright cannot see.
                if (!look.ok && ["input", "select"].includes(tag)) {
                  const proxy = (el.labels && el.labels[0]) || (el.closest && el.closest("label")) || el.parentElement;
                  const proxied = proxy ? seen(proxy) : null;
                  if (proxied && proxied.ok) { look = proxied; viaLabel = true; }
                }
                const visible = look.ok;
                const rect = look.r || { top: 0, left: 0, bottom: 0, right: 0 };
                const inView = visible && rect.bottom > 0 && rect.right > 0
                  && rect.top < (window.innerHeight || 0) && rect.left < (window.innerWidth || 0);
                let checked = null;
                if (tag === "input" && ["checkbox", "radio"].includes(String(el.type || "").toLowerCase())) checked = el.checked === true;
                else if (["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"].includes(role)) checked = attr("aria-checked") === "true";
                const expanded = attr("aria-expanded") === "" ? null : attr("aria-expanded") === "true";
                let fill = "";
                if (typing(el, tag, role)) {
                  const labels = el.labels ? Array.from(el.labels).map((l) => l.innerText || l.textContent || "").join(" ") : "";
                  const autocomplete = attr("autocomplete").toLowerCase();
                  const secret = String(el.type || "").toLowerCase() === "password" || el.hasAttribute("data-sensitive")
                    || /cc-|one-time-code|password|csc/.test(autocomplete)
                    || identifying.test([attr("name"), el.id || "", attr("placeholder"), attr("aria-label"), labels].join(" "));
                  const held = el.isContentEditable ? normalize(el.innerText) : String(el.value == null ? "" : el.value);
                  fill = secret ? "sensitive" : (held ? "filled" : "empty");
                }
                return {
                  disabled: Boolean(el.matches && el.matches(":disabled")) || attr("aria-disabled") === "true",
                  readonly: el.readOnly === true || attr("aria-readonly") === "true",
                  required: el.required === true || attr("aria-required") === "true",
                  checked,
                  expanded,
                  fill,
                  visible,
                  in_view: inView,
                  via_label: viaLabel,
                  hidden: Boolean(el.closest && el.closest('[aria-hidden="true"], [inert]')),
                };
              };
              const interactive = shown
                .slice(0, PER_FRAME)
                .map((el) => {
                  const tag = (el.tagName || "").toLowerCase();
                  const role = (el.getAttribute("role") || "").toLowerCase();
                  const row = {
                    tag,
                    text: typing(el, tag, role) ? "" : clip(el.innerText || el.textContent, 120),
                    type: el.getAttribute("type") || "",
                    role: el.getAttribute("role") || "",
                    placeholder: el.getAttribute("placeholder") || "",
                    selector: makeSelector(el),
                    name: labelOf(el, typing(el, tag, role)),
                    state: stateOf(el, tag, role),
                    context: contextOf(el),
                  };
                  if (tag === "select" && el.options) {
                    row.options = Array.from(el.options).slice(0, 50).map((o) => clip(o.label || o.text, 60));
                    row.options_total = el.options.length;
                  }
                  return row;
                });

              const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
                .slice(0, 12)
                .map((el) => clip(el.innerText || el.textContent, 160))
                .filter(Boolean);

              return {
                title: document.title || "",
                url: location.href,
                text: clip(document.body ? document.body.innerText : "", 50000),
                headings,
                interactive,
                interactive_total: shown.length,
                shadow_walk_cut: shadowCut,
                left_out: { in_editable: inEditable },
              };
            }
            """
        )

    _MAX_CHILD_FRAMES = 6

    async def _read_document(self, page: Any) -> dict[str, Any]:
        """The page and its child frames as one document: text, headings and interactive elements.

        Elements from a child frame carry `frame` (the frame's URL) so a ref taken from them is
        located in that frame. A frame that cannot be read (detached, cross-process timing) is
        skipped: a snapshot that names less is still honest, one that fails is not a snapshot.
        """
        payload = dict(await self._read_page(page) or {})
        payload["interactive"] = list(payload.get("interactive", []) or [])
        payload["headings"] = list(payload.get("headings", []) or [])
        omitted = {"over_cap": _over_cap(payload), "frames_not_http": 0, "frames_over_limit": 0, "frames_unreadable": 0,
                   "shadow_walk_cut": payload.pop("shadow_walk_cut", False) is True, "in_editable": 0}
        _count_left_out(omitted, payload.pop("left_out", None))
        payload.pop("interactive_total", None)
        frames = _child_frames(page)
        omitted["frames_over_limit"] = max(0, len(frames) - self._MAX_CHILD_FRAMES)
        for frame in frames[: self._MAX_CHILD_FRAMES]:
            frame_url = str(getattr(frame, "url", "") or "")
            if not frame_url.startswith(("http://", "https://")):
                omitted["frames_not_http"] += 1
                continue
            try:
                inner = dict(await self._read_page(frame) or {})
            except Exception:  # noqa: BLE001 - a frame that will not answer is left out, named below
                omitted["frames_unreadable"] += 1
                continue
            omitted["over_cap"] += _over_cap(inner)
            omitted["shadow_walk_cut"] = omitted["shadow_walk_cut"] or inner.get("shadow_walk_cut") is True
            _count_left_out(omitted, inner.get("left_out"))
            inner_text = str(inner.get("text", "") or "").strip()
            marker = f"[frame: {_host_path(frame_url)}]"
            if inner_text:
                payload["text"] = (str(payload.get("text", "") or "").rstrip() + "\n\n" + marker + "\n" + inner_text).strip()
            payload["headings"].extend(list(inner.get("headings", []) or []))
            for item in list(inner.get("interactive", []) or []):
                payload["interactive"].append({**item, "frame": frame_url})
        if len(payload["interactive"]) > MAX_SNAPSHOT_ELEMENTS:
            omitted["over_cap"] += len(payload["interactive"]) - MAX_SNAPSHOT_ELEMENTS
            payload["interactive"] = payload["interactive"][:MAX_SNAPSHOT_ELEMENTS]
        payload["omitted"] = omitted
        return payload

    async def _build_snapshot(self, page: Any, max_chars: int) -> dict[str, Any]:
        gate = self._action_gate
        if gate is not None:
            # Whatever this action sent, the page is read back only while it is still the agent's (C06).
            gate.input_sent = True
            gate.check()
        payload = await self._read_document(page)
        text = str(payload.get("text", "") or "")
        clip = clip_text(text, limit=max_chars, marker="browser snapshot text truncated") if max_chars > 0 else None
        if clip is not None:
            text = clip.text
        url = str(payload.get("url", "") or page.url)
        headings = list(payload.get("headings", []) or [])
        interactive = _with_stable_refs(list(payload.get("interactive", []) or []))
        self._refs = {str(item["ref"]): str(item.get("selector", "")) for item in interactive if item.get("selector")}
        self._ref_frames = {str(item["ref"]): str(item.get("frame", "")) for item in interactive if item.get("frame")}
        self._last_elements = interactive
        self._last_omitted = {**dict(payload.get("omitted", {}) or {}), "no_ref": sum(1 for item in interactive if not item.get("selector"))}
        self._last_snapshot_url = str(getattr(page, "url", "") or "")
        self.last_snapshot_digest = _structure_digest(url, headings, interactive)
        shown, not_listed = _model_rows(interactive)
        position = {id(item): index for index, item in enumerate(interactive)}
        # A control no unique selector names offers no ref here either: the model is never handed one it cannot use
        # (the runtime keeps the full rows above, for its own digest, its lookups and decision_rows).
        lean = [{**(_model_row(item) if item.get("selector") else {k: v for k, v in _model_row(item).items() if k != "ref"}),
                 "_doc": position.get(id(item), 0)} for item in shown]
        snapshot = {
            "title": str(payload.get("title", "") or ""),
            "url": url,
            "text": text,
            "text_truncated": bool(clip.truncated) if clip is not None else False,
            "text_omitted_chars": int(clip.omitted_chars) if clip is not None else 0,
            "headings": headings,
            "interactive_elements": [],
            # Q05 B1: what the list leaves out, said instead of cut silently.
            "interactive_omitted": {**self._last_omitted, "not_listed": not_listed},
            "snapshot_digest": self.last_snapshot_digest,
            "tabs": self.tabs(),
        }
        fitted, over_budget = _fit_rows(snapshot, lean)
        snapshot["interactive_elements"] = fitted
        snapshot["interactive_omitted"]["not_listed"] = not_listed + over_budget
        return snapshot

    async def lookup_block(self, key: str, needles: list[str], limit: int = 2000) -> dict[str, Any]:
        """Bulk P1 (lookup mode): the part of the live page that belongs to the order's own key, and whether it holds
        every needle. Read outward from where the key is written: inside a table row or a list item (a record among
        records) only that row counts -- another row's answer is never this order's; elsewhere the smallest element
        holding the key and every needle, of at most 4,000 characters. Also the smallest element around the key alone
        (for a "not found" message). Fields, editable regions, anything marked sensitive or hidden are never read.
        Main frame only. Read only; it settles nothing."""
        async with self._lock:
            page = await self._require_page()
            raw = await page.evaluate(
                """
                (args) => {
                  const key = String(args.key || "");
                  const needles = (args.needles || []).map(String).filter(Boolean);
                  const limit = Number(args.limit || 2000);
                  // Another record's key: a token shaped like this one (ORD-2026-0002 beside ORD-2026-0001). A part of
                  // the page that holds one holds more than this order, and is never its answer.
                  const shape = args.shape ? new RegExp(args.shape, "g") : null;
                  const otherRecord = (text) => shape !== null && (text.match(shape) || []).some(token => token !== key);
                  const maxClimb = shape !== null ? 64 : 1;
                  if (!document.body || !key) return {block: null, around: null, via_row: false, capped: false};
                  const skip = 'script, style, noscript, template, svg, input, textarea, select, '
                    + '[contenteditable]:not([contenteditable="false" i]), [data-sensitive], [aria-hidden="true"], [inert]';
                  const rowish = 'tr, li, [role="row"], [role="listitem"], article, [role="article"]';
                  const shown = (el) => {
                    if (!el || el.closest(skip) || el.getClientRects().length === 0) return false;
                    const style = getComputedStyle(el);
                    return style.visibility !== "hidden" && style.display !== "none";
                  };
                  const holdsAll = (text) => needles.every(n => text.indexOf(n) >= 0);
                  let best = null;
                  let bestRow = false;
                  let around = null;
                  const arounds = [];
                  let capped = false;
                  let seen = 0;
                  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                    seen += 1;
                    if (seen > 60000) { capped = true; break; }
                    if (String(node.nodeValue || "").indexOf(key) < 0) continue;
                    const holder = node.parentElement;
                    if (!shown(holder)) continue;
                    const aroundText = holder.innerText || "";
                    if (around === null || aroundText.length < around.length) around = aroundText;
                    // Every place the key is written, not only the smallest: a 「未找到」 banner for this query
                    // often sits in a different part of the page from a leftover answer.
                    if (arounds.length < 20 && arounds.indexOf(aroundText) < 0) arounds.push(aroundText.slice(0, limit));
                    // Without a shape there is no other-record guard (key_shape gives none for a non-ASCII key),
                    // so the row branch would accept a whole multi-record card. Fall through to the climb, which
                    // is capped at one level for exactly this reason.
                    const row = shape !== null ? holder.closest(rowish) : null;
                    let candidate = null;
                    if (row) {
                      const text = row.innerText || "";
                      if (text.length <= 4000 && holdsAll(text) && !otherRecord(text)) candidate = text;
                    } else {
                      let level = 0;
                      for (let at = holder; at && at !== document.documentElement && level <= maxClimb; at = at.parentElement, level += 1) {
                        const text = at.innerText || "";
                        if (text.length > 4000 || otherRecord(text)) break;
                        // Bulk P1 review: 「一小块」 needs a floor, and <body> is the page, not a part of it.
                        // Without this the climb could stop at <body>, so a sidebar saying 「最近查看：<key>」
                        // beside another order's tracking number in the main panel read as one record, and that
                        // other order's number was reported as this one's. Judged by identity, not by length:
                        // on a page whose whole content IS one card, the card's text equals the body's, and
                        // refusing it by length would throw away a perfectly good answer.
                        if (at === document.body) break;
                        if (holdsAll(text)) { candidate = text; break; }
                      }
                    }
                    if (candidate !== null && (best === null || candidate.length < best.length)) {
                      best = candidate;
                      bestRow = Boolean(row);
                    }
                  }
                  return {block: best === null ? null : best.slice(0, limit), around: around === null ? null : around.slice(0, limit),
                          arounds, via_row: bestRow, capped};
                }
                """,
                {"key": str(key), "needles": [str(n) for n in list(needles or [])], "limit": int(limit), "shape": key_shape(key)},
            )
        raw = raw if isinstance(raw, dict) else {}
        block, around = raw.get("block"), raw.get("around")
        return {"block": None if block is None else str(block), "around": None if around is None else str(around),
                "via_row": bool(raw.get("via_row")), "capped": bool(raw.get("capped"))}

    async def decision_page_text(self, max_chars: int = 4000) -> dict[str, Any]:
        """The page's words for a decider (Q05 B3) and the done check (B5): never a field's content, never a secret.

        The live body's rendered text is walked, skipping inputs, text areas, editable regions, [data-sensitive] and
        scripts -- what someone typed never leaves the page this way -- with the live address. Read only; it settles
        nothing (not an observation).
        """
        async with self._lock:
            page = await self._require_page()
            raw = await page.evaluate(
                """
                (limit) => {
                  if (!document.body) return {text: "", omitted: 0};
                  // The live page, rendered text only: no field, no editable region (any value but false), nothing a page
                  // marks sensitive, nothing hidden; a line break where a block ends so words of neighbours never run on.
                  const skip = 'input, textarea, select, script, style, noscript, template, svg, [contenteditable]:not([contenteditable="false" i]), '
                    + '[data-sensitive], [aria-hidden="true"], [inert]';
                  const blockish = new Set(["block", "list-item", "table", "table-row", "table-caption", "flex", "grid", "flow-root"]);
                  const displayOf = new WeakMap();
                  const display = (el) => {
                    if (!displayOf.has(el)) displayOf.set(el, getComputedStyle(el).display);
                    return displayOf.get(el);
                  };
                  const blockOf = (el) => {
                    for (let at = el, depth = 0; at && at !== document.body && depth < 8; depth += 1, at = at.parentElement) {
                      if (blockish.has(display(at))) return at;
                    }
                    return document.body;
                  };
                  let visited = 0;
                  let capped = false;
                  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
                    acceptNode(node) {
                      visited += 1;
                      if (visited > 60000) { capped = true; return NodeFilter.FILTER_REJECT; }
                      if (node.nodeType === 1) {
                        if (node.matches(skip)) return NodeFilter.FILTER_REJECT;
                        const style = getComputedStyle(node);
                        if (style.display === "none" || style.visibility === "hidden") return NodeFilter.FILTER_REJECT;
                        return NodeFilter.FILTER_SKIP;
                      }
                      return NodeFilter.FILTER_ACCEPT;
                    },
                  });
                  const parts = [];
                  let lastBlock = null;
                  let total = 0;
                  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                    const words = String(node.nodeValue || "").replace(/\\s+/g, " ").trim();
                    if (!words) continue;
                    const block = blockOf(node.parentElement);
                    const piece = (lastBlock !== null && block !== lastBlock ? "\\n" : (parts.length ? " " : "")) + words;
                    lastBlock = block;
                    total += piece.length;
                    if (total <= limit) parts.push(piece);
                  }
                  const text = parts.join("");
                  return {text: text.slice(0, limit), omitted: Math.max(0, total - Math.min(total, limit)), capped};
                }
                """,
                int(max_chars),
            )
        raw = raw if isinstance(raw, dict) else {}
        return {"text": str(raw.get("text", "") or ""), "omitted": int(raw.get("omitted", 0) or 0),
                # the walk stopped at its node cap: the words after it were never read (not "not there")
                "capped": bool(raw.get("capped")),
                "title": str(await page.title() or "")[:200] if hasattr(page, "title") else "",
                "url": str(getattr(page, "url", "") or "")}

    def decision_rows(self) -> dict[str, Any]:
        """The last snapshot's full rows, for a decider choosing among them (Q05 B3): never read afresh here.

        Read from the snapshot the model or the last action already took, so asking a decider never counts as an
        observation (only a model-facing one settles an unknown outcome, gateway.py).
        """
        return {
            "url": self._last_snapshot_url,
            "digest": self.last_snapshot_digest,
            "rows": [dict(item) for item in self._last_elements],
            "omitted": dict(self._last_omitted),
        }

    def _snapshot_to_markdown(self, snapshot: dict[str, Any]) -> str:
        parts = [
            f"# {snapshot.get('title') or 'Browser Snapshot'}",
            "",
            f"- URL: {snapshot.get('url', '')}",
            "",
            "## Page Text",
            "",
            str(snapshot.get("text", "") or ""),
        ]
        interactive = snapshot.get("interactive_elements", []) or []
        if interactive:
            parts.extend(["", "## Interactive Elements", ""])
            for item in interactive:
                # A control no unique selector names offers no ref: the model is never handed one it cannot use.
                handle = f"ref:{item.get('ref', '')} `{item.get('selector', '')}`" if item.get("selector") else "(no ref)"
                words = item.get("text", "") or item.get("name", "") or item.get("placeholder", "")
                flags = [str(flag) for flag in list(item.get("flags", []) or [])]
                parts.append(f"- {handle} [{item.get('tag', '')}] {words}" + (f" ({', '.join(flags)})" if flags else ""))
        omitted = dict(snapshot.get("interactive_omitted", {}) or {})
        left_out = {key: value for key, value in omitted.items() if value}
        if left_out:
            parts.extend(["", f"(not listed: {', '.join(f'{key} {value}' for key, value in left_out.items())})"])
        return "\n".join(parts).strip() + "\n"

    async def start_screencast(self, on_frame: Any, *, max_width: int = 1280, max_height: int = 800, quality: int = 60,
                               every_nth_frame: int = 1) -> dict[str, Any]:
        """Stream the page as JPEG frames to `on_frame(data_base64, metadata)` until stopped.

        Frames are the real page, unmasked: the caller shows them to the owner's own console and to
        nothing else (never to a model, never to a log). Not available before the first navigate.
        The stream is repaint-driven: a page that does not change produces one frame, so every
        frame is taken ( real run: everyNthFrame=2 skipped the only frame of a static
        login page and the pane stayed empty). Calling again re-sends the current frame.
        """
        page = self._page
        context = self._context
        if page is None or context is None:
            raise RuntimeError("no page yet: navigate before opening the live view")
        if self._cdp is None:
            cdp = await context.new_cdp_session(page)
            self._cdp = cdp
            self._cdp_generation += 1
            generation = self._cdp_generation

            async def _frame(params: dict[str, Any]) -> None:
                session_id = params.get("sessionId")
                try:
                    if session_id is not None:
                        # Ack on the session that produced the frame.  A tab
                        # switch may already have installed another CDP
                        # session; acknowledging the old frame on the new one
                        # corrupts both streams.
                        await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except Exception:  # noqa: BLE001 - an ack that fails ends the stream on the browser's side
                    pass
                if self._cdp is not cdp or self._cdp_generation != generation:
                    return
                callback = self._screencast_on_frame
                if callback is not None:
                    try:
                        result = callback(str(params.get("data", "") or ""), dict(params.get("metadata", {}) or {}))
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:  # noqa: BLE001 - a viewer that went away must not stop the page
                        pass

            cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(_frame(params)))
        self._screencast_on_frame = on_frame
        await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": int(quality), "maxWidth": int(max_width),
                                                      "maxHeight": int(max_height), "everyNthFrame": int(every_nth_frame)})
        return {"streaming": True, "max_width": int(max_width), "max_height": int(max_height)}

    async def stop_screencast(self) -> None:
        self._screencast_on_frame = None
        if self._cdp is not None:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001
                pass

    @property
    def screencast_active(self) -> bool:
        return self._screencast_on_frame is not None

    async def inject_input(self, event: dict[str, Any]) -> dict[str, Any]:
        """One input event from the person holding the page, forwarded to the browser over CDP.

        Accepts mouse (move/down/up/wheel with page coordinates in CSS pixels), key (keyDown/keyUp
        with a key name) and text (insertText). Nothing is recorded here: the caller decides who may
        send and keeps the content out of every log.
        """
        if self._cdp is None:
            raise RuntimeError("the live view is not open")
        if self._human_input_guard is not None and not self._human_input_guard():
            raise PermissionError("human_not_in_control")
        self._human_inputs_in_flight += 1
        try:
            kind = str(event.get("kind", "") or "")
            if kind == "mouse":
                payload = {"type": str(event.get("type", "mouseMoved")), "x": float(event.get("x", 0)), "y": float(event.get("y", 0)),
                           "button": str(event.get("button", "none")), "clickCount": int(event.get("click_count", 1) or 1)}
                if payload["type"] == "mouseWheel":
                    payload.update({"deltaX": float(event.get("delta_x", 0)), "deltaY": float(event.get("delta_y", 0))})
                await self._cdp.send("Input.dispatchMouseEvent", payload)
                return {"forwarded": "mouse"}
            if kind == "key":
                payload = {"type": str(event.get("type", "keyDown")), "key": str(event.get("key", ""))[:32]}
                code = str(event.get("code", "") or "")
                if code:
                    payload["code"] = code[:32]
                modifiers = int(event.get("modifiers", 0) or 0)
                if modifiers:
                    payload["modifiers"] = modifiers
                await self._cdp.send("Input.dispatchKeyEvent", payload)
                return {"forwarded": "key"}
            if kind == "text":
                await self._cdp.send("Input.insertText", {"text": str(event.get("text", ""))[:512]})
                return {"forwarded": "text"}
            raise ValueError(f"unknown input kind {kind!r}")
        finally:
            self._human_inputs_in_flight = max(0, self._human_inputs_in_flight - 1)

    def set_human_input_guard(self, guard: Callable[[], bool] | None) -> None:
        """Bind the final CDP input boundary to the registry's current holder."""
        self._human_input_guard = guard if callable(guard) else None

    @property
    def human_inputs_in_flight(self) -> int:
        return int(self._human_inputs_in_flight)

    def set_visible(self, visible: bool) -> None:
        """Launch on a screen rather than headless, so a person can take the page over (C06). Read from
        the task by the Gateway bridge; it takes effect only for a browser not yet launched."""
        self._visible = bool(visible)

    @property
    def visible(self) -> bool:
        return self._visible

    def set_output_root(self, root: Path | str | None) -> None:
        """Where this session's snapshots and screenshots may land: a product-owned directory bound
        to the session. Set by the Gateway bridge; nothing a tool argument says can move it."""
        self._output_root = Path(root) if root else None

    def _resolve_output_path(self, filename: str | None, *, suffix: str) -> Path:
        """A file name is a name, never a path (G08 / C29). Any directory part -- relative, absolute,
        a drive, a parent reference -- is dropped, and the file lands in the session's output root."""
        root = self._output_root or (get_ampuly_home() / "artifacts" / "browser")
        name = Path(str(filename or "")).name if filename else ""
        if name in {"", ".", ".."}:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"browser-{stamp}{suffix}"
        path = root / name
        if not path.suffix:
            path = path.with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _ensure_dependency(self) -> None:
        if async_playwright is None:
            raise RuntimeError(_INSTALL_HINT)

    async def _reset(self) -> None:
        """Release the four resources, innermost first, and keep whatever
        did not release.

        This used to clear all four references and then swallow all four
        exceptions, which turned "Chromium refused to exit" into "closed".
        A reference is dropped only after its close call returned; a call
        that raised leaves the reference in place and the runtime marked,
        and the caller gets an exception rather than a success dict. A
        cancellation mid-way is left as unknown -- the reference stays and
        the runtime is marked -- because a cancelled close proves nothing.
        """
        steps = (
            ("page", "_page", "close"),
            ("context", "_context", "close"),
            ("browser", "_browser", "close"),
            ("playwright", "_playwright", "stop"),
        )
        failed: list[str] = []
        for label, attr, method in steps:
            resource = getattr(self, attr)
            if resource is None:
                continue
            try:
                await getattr(resource, method)()
            except asyncio.CancelledError:
                self._close_failed = True
                raise
            except Exception:  # noqa: BLE001 - the provider's text does not leave the process
                # Stop here. Closing the outer resources after an inner one
                # failed would tear the failed one down from outside and leave
                # a reference that no retry can ever close -- a browser that
                # is gone cannot close its own context. Stopping keeps the
                # remaining chain intact, so a later close can walk it again.
                failed.append(label)
                break
            setattr(self, attr, None)
        if failed:
            self._close_failed = True
            raise BrowserCloseFailed(
                "The browser did not fully close; "
                f"still holding: {', '.join(failed)}. Retry browser_close.",
                reason="close_failed",
                unclosed=tuple(failed),
            )
        self._launch_config = None
        self._close_failed = False
        self._tabs = []
        self._cdp = None
        self._screencast_on_frame = None


# One browser per binding rather than one browser. See `browser_sessions`:
# the module-level runtime this replaced was shared by every task in the
# process, which made a customer's logged-in page reachable from the next
# task that called a browser tool.
_browser_sessions = BrowserSessionRegistry(
    # X05 / C14: hand-offs, hand-backs and revocations outlive the process.
    state_path=lambda: get_ampuly_home() / "browser" / "sessions-state.json",
)


async def _runtime_for(task: Any) -> Any:
    """The browser this task owns.

    `task` arrives from the runtime execution envelope, never from a
    ToolCall -- the registry injects it and refuses to read it from
    arguments -- so a model cannot name the customer whose browser it wants.
    """
    _refuse_a_step_without_the_browser(task)
    return await _browser_sessions.runtime_for(BrowserBinding.for_task(task))


def _refuse_a_step_without_the_browser(task: Any) -> None:
    """Q05 B2: the other steps of a connector step's role hold its browser tools and may not use them.

    A step that works through an API or MCP component must never move to a website -- least of all after its own
    outcome was unknown (owner decision 2). Refused before any browser is opened for it.
    """
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    if isinstance(node, dict) and str(node.get("browser", "") or "") == "none":
        from ampuly.computer_use.gateway import GatewayRefused

        raise GatewayRefused("browser_not_in_this_step",
                             "this step does not work in the browser; its role's browser belongs to the step that carries the browser connector")


def _playwright_hotkey(spec: str) -> str:
    """UI-TARS spells a chord as space-separated words (`ctrl c`); Playwright wants `Control+c` (one rule, in vision_actions)."""
    from ampuly.computer_use.vision_actions import playwright_hotkey

    return playwright_hotkey(spec)


def _compose_agent_dispatch_guard(lease_guard: Callable[[], bool], still_ok: Callable[[], bool] | None = None) -> Callable[[], bool]:
    """Lease, any outer selection guard, and still_ok must all hold before input.

    The visual path and the structured path share this combiner so a pre-installed
    ContextVar guard is not replaced by the lease guard alone.
    """
    prior_guard = _AGENT_DISPATCH_GUARD.get()
    parts: list = [lease_guard]
    if prior_guard is not None and prior_guard is not lease_guard:
        parts.append(prior_guard)
    if still_ok is not None:
        parts.append(still_ok)
    if len(parts) == 1:
        return parts[0]
    from ampuly.layer4_tools.browser_account_bind_guard import compose_agent_dispatch_guards

    return compose_agent_dispatch_guards(*parts)


def _target(selector: Any) -> dict[str, Any]:
    raw = str(selector or "")[:256]
    return {"ref": raw[4:]} if raw.startswith("ref:") else {"selector": raw}


async def _through_gateway(task: Any, tool: str, target: dict[str, Any], run, *, egress_hosts: tuple[str, ...] = (),
                           resolve_kind: Callable[[Any], Any] | None = None,
                           target_check: Callable[[Any, Any, dict[str, Any]], None] | None = None,
                           verify: Callable[[Any, Any], Awaitable[dict[str, Any] | None]] | None = None,
                           still_ok: Callable[[], bool] | None = None) -> Any:
    """One tool call as one Gateway step: lease synced, page observed, action admitted, receipt recorded.

    `still_ok` (Q05 B3) is asked with the lease right before any input is sent: a decider's cancel generation.

    The registry's own refusals still come first (it owns the lifecycle); the
    Gateway adds the classified, hashed sequence that X06 and the audit trail
    build on. `run` is called with the runtime and does the actual work.
    """
    from ampuly.computer_use import browser_bridge

    binding = BrowserBinding.for_task(task)
    browser_bridge.note_task_flags(binding, dict(getattr(task, "metadata", {}) or {}) if task is not None else {})
    browser_bridge.sync_lease(_browser_sessions, binding)
    runtime = await _runtime_for(task)
    _browser_sessions.note_agent_operating(binding)
    # A kind read from the live target is resolved inside admit, after the page is observed and checked against
    # the snapshot the model decided on (C05): a refusal there is journaled with that observation.
    admission = await browser_bridge.admit(_browser_sessions, binding, runtime, tool=tool, target=target, egress_hosts=egress_hosts,
                                           resolve_kind=(lambda: resolve_kind(runtime)) if resolve_kind is not None else None)
    report = getattr(runtime, "egress_report", None)
    blocked_before = report()["blocked"] if callable(report) else 0
    downloads = getattr(runtime, "download_report", None)
    downloads_before = downloads() if callable(downloads) else {}

    def _egress_evidence() -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        if callable(report):
            after = report()
            if after["blocked"] > blocked_before:
                evidence.update({"egress_blocked": after["blocked"] - blocked_before, "egress_blocked_hosts": after["hosts"]})
        if callable(downloads):
            now = downloads()
            for key in ("saved", "refused", "popups_closed", "tabs_adopted"):
                delta = int(now.get(key, 0)) - int(downloads_before.get(key, 0))
                if delta:
                    evidence[f"downloads_{key}" if key in {"saved", "refused"} else key] = delta
        return evidence

    before_hash = str(getattr(runtime, "last_snapshot_digest", "") or "")
    # Bulk P1 (L3): on a lookup step every action runs behind the write fence -- the page may read, not write.
    fence = getattr(runtime, "fence_writes", None) if browser_bridge.lookup_step(task) else None
    fenced_before = runtime.writes_fenced() if callable(fence) and hasattr(runtime, "writes_fenced") else 0
    if callable(fence):
        fence(True)
    lease_guard = browser_bridge.dispatch_guard(_browser_sessions, binding, admission)
    # B06-C6: keep any pre-installed selection / outer dispatch guard (C4 ContextVar)
    # under the live admit path instead of overwriting it. Compose with lease + still_ok.
    guard = _compose_agent_dispatch_guard(lease_guard, still_ok)
    token = _AGENT_DISPATCH_GUARD.set(guard)
    check_token = _AGENT_TARGET_CHECK.set((lambda live: target_check(runtime, admission, live)) if target_check is not None else None)
    try:
        result = await run(runtime)
    except BrowserElementNotFound:
        # Nothing was sent to the page: the target was never found, or control left the agent first.
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False, before_effect=True, evidence=_egress_evidence())
        raise
    except BaseException:
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False, evidence=_egress_evidence())
        raise
    finally:
        _AGENT_TARGET_CHECK.reset(check_token)
        _AGENT_DISPATCH_GUARD.reset(token)
        if callable(fence):
            fence(False)
    if not guard():
        # Control moved (a revoke, a hand-off, an ended lease) while this ran: what it read or did is not handed back.
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False, evidence=_egress_evidence())
        raise _control_moved()
    # A read is complete when it came back with what it read (from the result: the runtime's last digest is stale after a
    # screenshot or a tab switch). A write is only accepted input unless a stated page expectation was checked (§5.3).
    observed = isinstance(result, dict) and bool(result.get("snapshot_digest") or result.get("saved_to") or "active" in result)
    verification = await verify(result, runtime) if verify is not None and isinstance(result, dict) else None
    receipt = browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=True,
                                    evidence={"result_keys": sorted(result)[:12] if isinstance(result, dict) else [], **_egress_evidence()},
                                    after_hash=str(getattr(runtime, "last_snapshot_digest", "") or ""), before_hash=before_hash,
                                    verification=verification, observed_after=observed)
    _refuse_a_result_control_overtook(receipt)
    if (tool in {"browser_snapshot", "browser_observe"} or (tool == "browser_take_screenshot" and not target.get("purpose"))):
        browser_bridge.facts_observed(binding, admission)
    if verification is not None:
        result = {**result, "expectation": {key: verification.get(key) for key in ("checked", "failed", "holds")}}
    if _connector_of_task(task) is not None:
        await _note_done_baseline(task, binding, runtime, tool)  # Q05 B5: before its first write, and wherever it lands
    if callable(fence) and hasattr(runtime, "writes_fenced") and runtime.writes_fenced() > fenced_before:
        from ampuly.computer_use.gateway import GatewayRefused

        raise GatewayRefused("lookup_step_does_not_write",
                             "this step is a lookup: the page tried to write to the site and was stopped; read it another way or hand it to a person")
    return result


_PAGE_EXPECT_KEYS = ("url_contains", "text_contains", "element")


def _page_expectation(expect: Any) -> Callable[[Any, Any], Awaitable[dict[str, Any]]] | None:
    """A plain click is verified only by what the page shows afterwards: the stated url, text or element. Never an api
    read -- that would be a request with the session's login nobody approved."""
    page_expect = {key: value for key, value in dict(expect).items() if key in _PAGE_EXPECT_KEYS} if isinstance(expect, dict) else {}
    if not page_expect:
        return None

    async def check(result: Any, runtime: Any) -> dict[str, Any]:
        from ampuly.effects.browser_submit import check_postconditions_live, verification_of

        return verification_of(await check_postconditions_live(page_expect, result if isinstance(result, dict) else {}, runtime))

    return check


def _control_moved() -> Exception:
    from ampuly.computer_use.gateway import GatewayRefused

    return GatewayRefused("agent_not_in_control", "control of the page moved while this action ran; wait for the hand-back "
                                                  "or the owner, then take a new snapshot")


def _refuse_a_result_control_overtook(receipt: Any) -> None:
    """A result that came back after the turn was cancelled or control moved is not handed to the model (C06, C12):
    the page it describes may be the one a person now holds."""
    from ampuly.computer_use.contracts import Outcome
    from ampuly.computer_use.gateway import GatewayRefused

    if getattr(receipt, "outcome", None) is Outcome.CANCELLED_UNKNOWN:
        raise GatewayRefused("agent_not_in_control", "control of the page moved while this action ran; wait for the hand-back "
                                                     "or the owner, then take a new snapshot")


async def browser_navigate(
    url: str,
    wait_until: str = "domcontentloaded",
    task: Any = None,
) -> dict[str, Any]:
    binding = BrowserBinding.for_task(task)
    runtime = await _runtime_for(task)
    # W05: the allowed domains come from the task that launched this run --
    # never from a tool argument -- and a target outside them is refused
    # before the page moves.
    from ampuly.computer_use import browser_bridge as _bridge

    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    # X08: the scope the console delegated since this task was loaded wins over the task's own copy.
    _bridge.note_task_flags(binding, metadata)
    domains = _bridge.task_flag(binding, "browser_allowed_domains") or metadata.get("browser_allowed_domains")
    if domains:
        _browser_sessions.bind_domains(binding, domains)
    _browser_sessions.check_navigation(binding, url)
    host = (urlsplit(str(url or "")).hostname or "").lower()
    connector_domains = _bridge.task_flag(binding, _bridge.CONNECTOR_DOMAINS_FLAG)
    if connector_domains is not None:
        from ampuly.computer_use.web_scope import REFUSAL_WORDS, navigation_refusal

        # Q05 B2: delegated to the order is not enough; a connector step goes only to the sites it names -- and
        # since P2 护栏一 a step may name an address rather than a whole site, judged by the same shared rule.
        # The scheme is required here rather than left to that rule, which exempts data:/about:/blob: as the
        # request rule does. This gate compared a hostname before, so an address with no origin always failed it;
        # letting one through would move the page to a document the owner's live view still reads as 「the page」.
        scheme = (urlsplit(str(url or "")).scheme or "").lower()
        refusal = "off_site" if scheme not in {"http", "https"} else navigation_refusal(str(url or ""), connector_domains)
        if refusal:
            from ampuly.computer_use.gateway import GatewayRefused

            raise GatewayRefused("off_connector_domain",
                                 f"{host or 'this address'} is not somewhere this step's browser connector names: "
                                 f"{REFUSAL_WORDS[refusal]}")
    moved = await _through_gateway(task, "browser_navigate", {"url": str(url)[:2048]},
                                   lambda rt: rt.navigate(url=url, wait_until=wait_until), egress_hosts=(host,) if host else ())
    # Bulk P1: the page was opened, not merely looked at. Recorded only after the navigation came back, so a
    # refused or failed move never counts as having opened anything.
    _bridge.note_page_opened(binding)
    return moved


async def browser_snapshot(
    filename: str | None = None,
    max_chars: int = 12_000,
    task: Any = None,
) -> dict[str, Any]:
    return await _through_gateway(task, "browser_snapshot", {"max_chars": int(max_chars)},
                                  lambda rt: rt.snapshot(filename=filename, max_chars=max_chars))


# Q05 B3: the decider -- PULY (JEV) picks the next few low-risk steps on a connector step's page.
DECIDER_STEPS_DEFAULT = 8
DECIDER_STEPS_LIMIT = 20
DECIDER_OPTION_LIMIT = 240
_DECIDER_PAGE_CHARS = 4000
_DECIDER_STUCK_AFTER = 3


def _connector_of_task(task: Any) -> dict[str, Any] | None:
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    connector = node.get("connector") if isinstance(node, dict) else None
    return connector if isinstance(connector, dict) else None


def _decider_label(row: dict[str, Any]) -> str:
    return str(row.get("text") or row.get("name") or row.get("placeholder") or "").strip()[:60]


_TEXT_ROLES = {"textbox", "searchbox"}
_TEXT_INPUTS = {"", "text", "search", "email", "url", "tel", "number", "password", "date", "time", "datetime-local", "month", "week"}


def decision_options(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """What a decider may choose from, by product ids (never page text as a key), in view first; and how many were left out.

    A control may be offered when it has a ref, is not disabled and has a name (a nameless icon may be anything,
    a delete included). A field is offered for the step's own model to fill (needs_text); a list offers its choices.
    Scrolling the page, going back, waiting, done and a person are always there.
    """
    def in_view(row: dict[str, Any]) -> bool:
        state = row.get("state")
        return not isinstance(state, dict) or state.get("in_view", True) is not False

    ordered = [r for r in rows if in_view(r)] + [r for r in rows if not in_view(r)]
    options: list[dict[str, Any]] = []
    left_out = 0
    room = DECIDER_OPTION_LIMIT - 5
    for row in ordered:
        ref = str(row.get("ref", "") or "")
        state = dict(row.get("state", {}) or {})
        if (not ref or not row.get("selector") or state.get("disabled") is True or state.get("visible") is False
                or state.get("hidden") is True or state.get("via_label") is True):
            continue  # not operable here (a kit's invisible control is clicked through its label, by the step's own model)
        if str(row.get("tag", "") or "") == "input" and str(row.get("type", "") or "").lower() == "file":
            continue  # an upload is a person's (the connector's hand-off)
        label = _decider_label(row)
        tag = str(row.get("tag", "") or "")
        role = str(row.get("role", "") or "").lower()
        base = {"ref": ref, "tag": tag, "role": role, "label": label,
                "state": {k: state[k] for k in ("checked", "expanded", "fill", "readonly") if k in state and state[k] not in (None, "")}}
        if tag == "select":
            choices = [str(c) for c in list(row.get("options", []) or [])]
            for index, choice in enumerate(choices):
                if len(options) >= room:
                    left_out += 1
                    continue
                options.append({**base, "id": f"s.{ref}.{index}", "op": "select", "choice": choice[:60]})
            continue
        typing = (tag == "textarea" or role in _TEXT_ROLES or (tag == "input" and str(row.get("type", "") or "").lower() in _TEXT_INPUTS)
                  or state.get("fill") in {"empty", "filled", "sensitive"})
        if typing:
            if state.get("readonly") is True:
                continue
            entry = {**base, "id": f"t.{ref}", "op": "fill"}
        else:
            if not label:
                continue
            entry = {**base, "id": f"c.{ref}", "op": "click"}
        if len(options) >= room:
            left_out += 1
            continue
        options.append(entry)
    options.extend({"id": op, "op": op} for op in ("scroll", "back", "wait", "done", "human"))
    return options, left_out


def _page_hosts_allowed(runtime: Any, connector: dict[str, Any], delegated: Any) -> bool:
    from ampuly.computer_use.browser_bridge import narrowed_hosts

    allowed = narrowed_hosts(delegated, connector.get("allowed_domains", []))
    view = runtime.decision_rows()
    urls = [str(view.get("url", "") or "")]
    urls.extend(str(row.get("frame", "") or "") for row in list(view.get("rows", []) or []) if row.get("frame"))
    tab_urls = getattr(runtime, "tab_urls", None)
    if callable(tab_urls):
        try:
            urls.extend(str(url or "") for url in list(tab_urls() or []))
        except Exception:  # noqa: BLE001 - a tab list that cannot be read is judged by the page alone
            pass
    for url in urls:
        scheme = (urlsplit(url).scheme or "").lower()
        if not url or scheme in {"about", "data", "blob", "chrome-error"}:
            continue
        host = (urlsplit(url).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in allowed):
            return False
    return True


# Q05 B5: the connector's done check. Code reads the live page and the downloads this browser saved against the step's
# postconditions; a model (JEV or the step's own) saying done is not done, and what already held on the step's own
# sites before its first write is not evidence.
_DONE_TEXT_CHARS = 20_000
_ANY_FILE = frozenset({"*", "any", "任意"})
_PRODUCT_DOWNLOAD = re.compile(r"^download-([0-9a-f]{12})(?:-\d{1,2})?\.[a-z0-9]{1,8}$")
# Tools that move the page somewhere by the model's choice (reads to the Gateway): wherever they land is a baseline,
# even after the first write -- a page reached by going there is not work the step did.
_LANDING_TOOLS = frozenset({"browser_navigate", "browser_navigate_back", "browser_switch_tab"})


def _download_key(name: str) -> str:
    """A product-named download by its content (the digest prefix in its name): the same bytes saved again are not new."""
    match = _PRODUCT_DOWNLOAD.match(str(name or ""))
    return match.group(1) if match else ""
_TYPE_MIME = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "csv": "text/csv", "txt": "text/plain", "md": "text/markdown",
    "json": "application/json", "yaml": "application/yaml", "yml": "application/yaml", "xml": "application/xml",
    "svg": "image/svg+xml", "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _connector_hosts(binding: Any, connector: dict[str, Any]) -> tuple[str, ...]:
    from ampuly.computer_use.browser_bridge import narrowed_hosts, task_flag

    return narrowed_hosts(task_flag(binding, "browser_allowed_domains"), connector.get("allowed_domains", []))


def _on_hosts(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return bool(host) and any(host == d or host.endswith("." + d) for d in hosts)


# Bulk P4: how a condition names the row it is about -- 「已发布 {item.标题}」. Filled by code from the order's own
# row; the model never writes one and never sees the list.
_ITEM_FIELD = re.compile(r"\{item\.([^{}]{1,64})\}")


def _fill_item(value: str, row: dict[str, str]) -> str | None:
    """A condition with this row's own words in it, or None when the row carries no value for a field it names.

    A missing field must never become an empty string. 「已发布 」 with nothing after it is a condition almost any
    page satisfies, so it would prove an item done that nobody ever looked at -- the exact shape of failure P1's
    review found in the lookup extractor. An item whose condition cannot be written is refused instead.
    """
    missing = False

    def one(match: "re.Match[str]") -> str:
        nonlocal missing
        got = str(row.get(match.group(1), "") or "").strip()
        if not got:
            missing = True
        return got

    filled = _ITEM_FIELD.sub(one, str(value or ""))
    return None if missing else filled


def _posts_of(connector: dict[str, Any], row: dict[str, str] | None = None) -> list[dict[str, str]] | None:
    """The step's postconditions. With a row (bulk P4) every ``{item.<field>}`` is filled from it, and None comes
    back when the row cannot answer one of them."""
    posts = [{"kind": str(p.get("kind", "") or ""), "value": str(p.get("value", "") or "").strip()}
             for p in list(connector.get("postconditions", []) or []) if isinstance(p, dict)]
    if row is None:
        return posts
    filled: list[dict[str, str]] = []
    for post in posts:
        value = _fill_item(post["value"], row)
        if value is None:
            return None
        filled.append({"kind": post["kind"], "value": value})
    return filled


async def _done_page(runtime: Any) -> dict[str, Any]:
    """The page as the done check reads it: the live address and up to 20,000 characters of its rendered words, and
    whether any of the words went unread (cut at the limit or at the walk's node cap)."""
    page = await runtime.decision_page_text(_DONE_TEXT_CHARS)
    url = str(page.get("url", "") or "") or str(runtime.decision_rows().get("url", "") or "")
    return {"url": url, "text": str(page.get("text", "") or ""),
            "text_truncated": int(page.get("omitted", 0) or 0) > 0 or bool(page.get("capped"))}


async def _page_conditions(posts: list[dict[str, str]], page: dict[str, Any], runtime: Any) -> dict[int, dict[str, bool]]:
    """Each page postcondition against the page now, by its index: {holds, unanswered}. The address is matched as the
    evidence shows it (scheme, host, path: never the query or the fragment); words that may lie past what was read,
    and an element query that failed, are unanswered -- "could not tell", never "not there"."""
    from ampuly.computer_use.audit import _safe_page

    out: dict[int, dict[str, bool]] = {}
    shown = _safe_page(page["url"])
    for index, post in enumerate(posts):
        kind, value = post["kind"], post["value"]
        if not value or kind not in _PAGE_EXPECT_KEYS:
            continue
        if kind == "url_contains":
            out[index] = {"holds": value in shown, "unanswered": False}
        elif kind == "text_contains":
            holds = value in page["text"]
            out[index] = {"holds": holds, "unanswered": not holds and bool(page["text_truncated"])}
        else:
            strict = getattr(runtime, "target_count_strict", None)
            try:
                count = int(await strict(value)) if callable(strict) else 0
                out[index] = {"holds": count > 0, "unanswered": not callable(strict)}
            except Exception:  # noqa: BLE001 - a query that failed could not tell
                out[index] = {"holds": False, "unanswered": True}
    return out


async def _note_done_baseline(task: Any, binding: Any, runtime: Any, tool: str = "") -> None:
    """While a connector step has written nothing, every look at its own sites extends its baseline: the page conditions
    that hold there (or that the read could not settle) and the downloads the work item already has. After its first
    write (a click, typing, a choice, a submit) only a page it went to by itself (navigate, back, another tab) still
    extends it -- the conditions that hold where it landed, not the downloads its work produced. Never fails the tool."""
    from ampuly.computer_use import browser_bridge

    connector = _connector_of_task(task)
    if connector is None:
        return
    try:
        # Bulk P4: on a batch step the baseline belongs to the item now running. Item 1's post is already on the
        # page when item 2 begins, and 「原本就有不算」 has to be asked per item or item 2 would inherit it.
        item = browser_bridge.current_item(binding)
        row = _item_row(task, item) if item else None
        written = browser_bridge.writes_admitted(binding, item=item)
        if written:
            if tool not in _LANDING_TOOLS:
                return
            if browser_bridge.verified_external_effect(binding, item=item):
                # A receipt page reached after an independently verified
                # submit is observed after the work, never before the work.
                # Plain navigation and unverified writes keep the old rule.
                return
        page = await _done_page(runtime)
        if not _on_hosts(page["url"], _connector_hosts(binding, connector)):
            return
        posts = _posts_of(connector, row)
        if posts is None:
            return
        found = await _page_conditions(posts, page, runtime)
        held = [browser_bridge.done_condition_key(posts[i]["kind"], posts[i]["value"])
                for i, c in found.items() if c["holds"] or c["unanswered"]]
        files = [] if written else [key for key in (_download_key(p.name) for p in browser_bridge.order_downloads(binding)) if key]
        browser_bridge.note_done_baseline(binding, held=held, downloads=files, item=item)
    except Exception:  # noqa: BLE001 - without one the check takes the page it reads as the baseline (stricter)
        return


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_downloads(runtime: Any, known: set[str]) -> list[tuple[int, Path, str, int]]:
    """The downloads this browser saved itself, whose content is not in the baseline (`known`: digest prefixes), still
    as they were saved: (order, path, digest, size).

    A file is counted only when the runtime published it (inspected, under the product's own name) and its bytes still
    hash to what was recorded then; a file that merely sits in the directory, or was changed since, is not."""
    reader = getattr(runtime, "saved_downloads", None)
    saved = list(reader() or []) if callable(reader) else []
    out: list[tuple[int, Path, str, int]] = []
    for seq, record in enumerate(saved, 1):
        path = Path(str(record.get("path", "") or ""))
        if not _PRODUCT_DOWNLOAD.match(path.name):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            digest = _sha256_of(path)
            size = path.stat().st_size
        except OSError:
            continue
        if digest != str(record.get("sha256", "") or "") or digest[:12] in known:
            continue  # changed since it was saved, or the same bytes the order already had
        out.append((seq, path, digest, size))
    return out


def _download_evidence(binding: Any, seq: int, path: Path, digest: str, size: int) -> dict[str, Any]:
    """A saved download as evidence: its place among this browser's downloads, its digest prefix (recomputed), size and
    type -- never a page's file name. ``path`` is relative to the project's folder; a download in the order step's own
    folder also says how the step's file tools name it (``read_as``, e.g. ``../steps/fetch/downloads/download-….csv``)."""
    from ampuly.computer_use import browser_bridge

    evidence = {"seq": seq, "sha256_prefix": digest[:12], "bytes": size,
                "mime": _TYPE_MIME.get(path.suffix.lower().lstrip("."), "application/octet-stream"),
                "path": f"orders/{getattr(binding, 'task_id', '')}/browser-downloads/{path.name}"}
    area = browser_bridge.task_flag(binding, browser_bridge.ORDER_AREA_FLAG)
    if area is not None:
        in_folder = area.read_as(path) if path.parent == area.downloads or _same_folder(path.parent, area.downloads) else None
        project_path = area.p_relative(path) if in_folder else None
        if in_folder and project_path:
            evidence["path"] = project_path
            evidence["read_as"] = in_folder
    return evidence


def _same_folder(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return False


def _item_row(task: Any, item: str) -> dict[str, str] | None:
    """The batch step's own row for this item, read from the order's record. None when it is not one of its rows."""
    from ampuly.computer_use import browser_bridge

    rows = browser_bridge.batch_items(task)
    field = browser_bridge.batch_item_key(task)
    if not rows or not field:
        return None
    wanted = str(item or "").strip()
    return next((row for row in rows if str(row.get(field, "") or "").strip() == wanted), None) if wanted else None


async def _check_done(binding: Any, runtime: Any, connector: dict[str, Any], task: Any = None, *,
                      item: str = "", row: dict[str, str] | None = None) -> dict[str, Any]:
    """Q05 B5: whether a connector step's work is done, by code: every postcondition holds now, and at least one of them
    did not already hold on the step's own sites before its first write (a file this browser saved since then always
    counts as new).

    Returns {status: done_verified | done_unverified, reason, evidence}; the verdict is journaled, and it stands only
    while nothing is written after it (browser_bridge.done_verified). Evidence: the address without query or fragment,
    each condition (holds, held before), the Gateway's last action ids, and the downloads it rests on.
    """
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.audit import _safe_page

    posts = _posts_of(connector, row)
    output = dict(connector.get("output", {}) or {})
    # Two items with the same template get DIFFERENT digests, because the row's own words are in the value: a
    # verdict about item 1 can never be read as a verdict about item 2.
    checked = browser_bridge.done_conditions_digest(posts or [])
    if str(_verify_of(connector).get("mode", "") or "") == "lookup":
        return await _check_lookup(task, binding, runtime, connector)

    def verdict(status: str, reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
        browser_bridge.note_done_verdict(binding, status, reason, conditions=checked, item=item)
        out: dict[str, Any] = {"status": status, "reason": reason, "evidence": evidence}
        if status == "done_verified":
            out["output"] = {"path": str(output.get("path", "") or ""), "fields": [str(f) for f in list(output.get("fields", []) or [])]}
        return out

    if runtime is None:
        return verdict("done_unverified", "no_page", {})
    if posts is None:
        # The row does not answer a field its own conditions name. Refused rather than filled with nothing.
        return verdict("done_unverified", "item_field_missing", {"item": item[:80]})
    if not posts:
        return verdict("done_unverified", "no_postconditions", {})
    if browser_bridge.outcome_unresolved(binding, item=item) or browser_bridge.effect_outcome_unknown(binding, item=item):
        # A write whose outcome nobody knows -- a click, or a submit the owner approved -- could be why the page shows
        # what it shows; it is never verified past it.
        return verdict("done_unverified", "unresolved_outcome", {})
    try:
        page = await _done_page(runtime)
        found = await _page_conditions(posts, page, runtime)
    except Exception as exc:  # noqa: BLE001 - a page that cannot be read proves nothing
        return verdict("done_unverified", "page_unreadable", {"detail": type(exc).__name__})
    if not _on_hosts(page["url"], _connector_hosts(binding, connector)):
        return verdict("done_unverified", "off_connector_domain", {"url": _safe_page(page["url"])})
    baseline = browser_bridge.done_baseline(binding, item=item)
    keys = [browser_bridge.done_condition_key(post["kind"], post["value"]) for post in posts]
    if baseline is None:
        # No look was recorded before a write (the step never reached its sites through a tool, or its journal is
        # gone): the page as it is now is the baseline, so nothing on it counts as new -- stricter, never looser.
        held_before = {keys[i] for i, c in found.items() if c["holds"] or c["unanswered"]}
        known = {key for key in (_download_key(p.name) for p in browser_bridge.order_downloads(binding)) if key}
        overflow = False
    else:
        held_before = set(baseline.get("held", []))
        known = set(baseline.get("downloads", []))
        overflow = bool(baseline.get("overflow"))
    new_files = [] if overflow else _new_downloads(runtime, known)
    conditions: list[dict[str, Any]] = []
    used: list[tuple[int, Path, str, int]] = []
    for index, post in enumerate(posts):
        kind, value = post["kind"], post["value"]
        if kind == "file_exists":
            wanted = value.lower().lstrip(".")
            matching = [f for f in new_files if wanted in _ANY_FILE or f[1].suffix.lower().lstrip(".") == wanted]
            used.extend(f for f in matching if f not in used)
            conditions.append({"kind": kind, "value": value[:80], "holds": bool(matching), "held_before": False})
            continue
        result = found.get(index, {"holds": False, "unanswered": False})
        entry: dict[str, Any] = {"kind": kind, "value": value[:80], "holds": result["holds"], "held_before": keys[index] in held_before}
        if result["unanswered"]:
            entry["unanswered"] = True
        conditions.append(entry)
    evidence = {
        "url": _safe_page(page["url"]),
        "conditions": conditions,
        "baseline": "before_the_first_write" if baseline is not None else "taken_at_this_check",
        "actions": browser_bridge.recent_actions(binding),
        "downloads": [_download_evidence(binding, *f) for f in used],
    }
    missing = [c for c in conditions if not c["holds"]]
    if missing:
        return verdict("done_unverified", "condition_unanswered" if any(c.get("unanswered") for c in missing) else "condition_not_met", evidence)
    new = [c for c in conditions if not c["held_before"]]
    if not new:
        return verdict("done_unverified", "held_before_the_work", evidence)
    if all(c["kind"] == "url_contains" for c in new):
        # Only the address changed: a link can take a page anywhere without any work being done.
        return verdict("done_unverified", "only_the_address_changed", evidence)
    return verdict("done_verified", "", evidence)


# Bulk P1 (lookup mode, owner ruling ): a step that only READS a site is done when the site, in this turn,
# shows the order's own key and the answer together in one small block -- reaching the page is the work. The baseline
# rule of an operation step does not apply; the key comes from the order's business record, never from the model.
LOOKUP_KEY_MIN = 6
_NO_RESULT_DEFAULTS = ("未找到", "没有找到", "查无", "无结果", "暂无结果", "无记录", "暂无数据", "不存在",
                       "no results", "no result", "not found", "no records", "nothing found")
_KEY_REF = re.compile(r"^order\.inputs\.([A-Za-z0-9_\-]{1,64})$")


def _verify_of(connector: dict[str, Any]) -> dict[str, Any]:
    verify = connector.get("verify")
    return dict(verify) if isinstance(verify, dict) else {}


def _key_input_name(verify: dict[str, Any]) -> str:
    key = verify.get("key")
    ref = str(key.get("from", "") if isinstance(key, dict) else key or "").strip()
    match = _KEY_REF.match(ref)
    return match.group(1) if match else ""


def key_shape(key: str) -> str:
    """A regular expression for tokens shaped like this key (letters for letters, digits for digits, marks as they
    are, runs counted), bounded by non-alphanumerics: another record's key on the same page. '' when the key holds
    anything but ASCII -- such a key is only read one level around where it is written."""
    key = str(key or "")
    if not key or not key.isascii():
        return ""
    parts: list[str] = []
    for char in key:
        part = r"\d" if char.isdigit() else "[A-Za-z]" if char.isalpha() else re.escape(char)
        if parts and parts[-1][0] == part:
            parts[-1] = (part, parts[-1][1] + 1)
        else:
            parts.append((part, 1))
    body = "".join(part if count == 1 else f"{part}{{{count}}}" for part, count in parts)
    return rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"


def _event_at(event: dict[str, Any]) -> float:
    """An event's wall-clock stamp, or 0.0 when it carries none that can be read."""
    try:
        return float(event.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _joins(neighbour: str, edge: str) -> bool:
    """Whether these two characters are one token rather than two.

    Both ASCII letters or digits ("A-1001" inside "A-10012"), or both outside ASCII. Python's ``isalnum()`` is
    True for Chinese characters, so the plain test made 「订单ORD-2026-0001的物流单号」 -- an ordinary Chinese
    heading, where the key really is this order's -- read as another record's number and refused. Whether a
    correct lookup completed then depended on whether the portal happened to print punctuation before the number.
    This is the boundary :func:`key_shape` already uses one layer down.
    """
    return neighbour.isalnum() and edge.isalnum() and neighbour.isascii() == edge.isascii()


def _key_stands_alone(block: str, key: str) -> bool:
    """The key appears as itself: nothing right before or after it extends it into a longer token."""
    start = block.find(key)
    while start >= 0:
        before = block[start - 1] if start > 0 else " "
        after = block[start + len(key)] if start + len(key) < len(block) else " "
        if not _joins(before, key[:1]) and not _joins(after, key[-1:]):
            return True
        start = block.find(key, start + 1)
    return False


# What a portal prints where it has no answer yet. Reading one of these as the answer is worse than reading
# nothing at all: nothing is handed to a person, while a placeholder is written into the delivery as fact.
_EMPTY_VALUES = frozenset({
    "暂无", "无", "待定", "待生成", "未生成", "未分配", "待发货", "待补充", "-", "--", "n/a", "na", "null", "none",
})


def _extract(block: str, label: str) -> str:
    r"""The value written after a label, **on the same line and in the same cell** ("物流单号：SF123" -> "SF123").

    The bound is the whole point. ``\s`` matches a newline and a tab, so a label with nothing after it used to
    run on and take the NEXT field's value: 「物流单号：」 followed by 「收件人：张三」 answered 张三, and that
    answer went into the delivery as this order's tracking number. A label with nothing after it means the page
    did not answer. '' here reaches ``answer_not_found``, which hands the item to a person -- which is what
    「查不实就交人」 means.
    """
    # A space or a full-width space, and nothing else. A TAB is a cell boundary, not spacing:
    # 「物流单号<tab>收件人」 is two columns, and reading the tab as spacing took the next column's
    # heading as this field's answer.
    match = re.search(re.escape(label) + r"[ \u3000]*[:：]?[ \u3000]*([^\s，,；;。]{1,200})", block)
    value = (match.group(1) if match else "").strip()
    if not value.strip("-—–/·•.、:：_"):
        return ""   # only punctuation: the same thing as an empty cell, written as a dash
    return "" if value.casefold() in _EMPTY_VALUES else value


async def _check_lookup(task: Any, binding: Any, runtime: Any, connector: dict[str, Any]) -> dict[str, Any]:
    """Bulk P1: whether a lookup step's answer is on the page, by code (rules L1-L3 of the design's appendix)."""
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.audit import _safe_page

    verify = _verify_of(connector)
    posts = _posts_of(connector)
    output = dict(connector.get("output", {}) or {})
    fields = [str(f) for f in list(output.get("fields", []) or []) if str(f).strip()]
    labels = {f: str(dict(verify.get("extract", {}) or {}).get(f, f) or f) for f in fields}
    checked = browser_bridge.done_conditions_digest(posts)

    def verdict(status: str, reason: str, evidence: dict[str, Any], values: dict[str, str] | None = None) -> dict[str, Any]:
        browser_bridge.note_done_verdict(binding, status, reason, conditions=checked)
        out: dict[str, Any] = {"status": status, "reason": reason, "evidence": evidence, "mode": "lookup"}
        if status == "done_verified":
            out["output"] = {"path": str(output.get("path", "") or ""), "fields": fields, "values": dict(values or {})}
        return out

    if runtime is None:
        return verdict("done_unverified", "no_page", {})
    if browser_bridge.outcome_unresolved(binding) or browser_bridge.effect_outcome_unknown(binding):
        return verdict("done_unverified", "unresolved_outcome", {})
    events = browser_bridge._binding_events(binding)
    # A lookup may look, scroll, switch tabs and type a draft. It may not CLICK: a state-changing GET
    # (<a href="/orders/{key}/cancel">取消订单</a>) is admitted as an ordinary reversible write, fires no
    # approval because its label is in no effect-word list, and leaves the request fence at zero because it is a
    # GET. Refusing the click is the conservative direction, and it degrades to a person rather than to a wrong
    # answer. (The design's 「在声明过的搜索框里输入除外」 wants a declared search submit to be allowed; the
    # declaration now exists as the connector's ``controls``, but wiring it into this verdict is its own change.)
    if any(e.get("type") == "admitted"
           and (str(e.get("risk", "") or "") == "external_effect" or str(e.get("kind", "") or "") == "select_candidate")
           for e in events):
        return verdict("done_unverified", "lookup_step_wrote", {})
    name = _key_input_name(verify)
    key = (browser_bridge.order_input(task, name) or "").strip() if name else ""
    if not key:
        return verdict("done_unverified", "key_unresolved", {"key_input": name})
    if len(key) < LOOKUP_KEY_MIN:
        return verdict("done_unverified", "key_too_short", {"key_input": name})
    # 「页面是这一回合打开的」, held to what it says. It used to accept any admitted action at all -- a plain
    # browser_observe of a page left open since an earlier turn satisfied it, which is precisely the case the
    # rule exists to refuse. Only moving the browser opens a page, so only that counts. And a missing turn stamp
    # now refuses rather than skipping the rule: with no clock, nothing shows the page is current, and a lookup's
    # entire claim is 「这个网站此刻对这张单显示了这个答案」.
    try:
        started = float(dict(getattr(task, "metadata", {}) or {}).get("connector_turn_started_at"))
    except (TypeError, ValueError):
        return verdict("done_unverified", "not_this_turn", {"key_input": name})
    if not any(e.get("type") == "page_opened" and _event_at(e) >= started for e in events):
        return verdict("done_unverified", "not_this_turn", {"key_input": name})
    try:
        page = await _done_page(runtime)
    except Exception as exc:  # noqa: BLE001 - a page that cannot be read proves nothing
        return verdict("done_unverified", "page_unreadable", {"detail": type(exc).__name__})
    if not _on_hosts(page["url"], _connector_hosts(binding, connector)):
        return verdict("done_unverified", "off_connector_domain", {"url": _safe_page(page["url"])})
    shown = _safe_page(page["url"])
    filled = [{"kind": p["kind"], "value": p["value"].replace("{key}", key)} for p in posts]
    needles = [p["value"] for p in filled if p["kind"] == "text_contains" and p["value"]] + list(labels.values())
    evidence: dict[str, Any] = {"url": shown, "key_input": name, "baseline": "not_used_in_lookup",
                                "actions": browser_bridge.recent_actions(binding)}
    try:
        found = await runtime.lookup_block(key, needles)
    except Exception as exc:  # noqa: BLE001
        return verdict("done_unverified", "page_unreadable", {**evidence, "detail": type(exc).__name__})
    block = found.get("block")
    patterns = [str(p).lower() for p in (*_NO_RESULT_DEFAULTS, *list(verify.get("no_result", []) or [])) if str(p).strip()]
    # Read wherever the key is written, not only in the block that was picked. A search page shows 「未找到 <key>
    # 的物流信息」 in one place and 「最近查询 <key> 物流单号：SF000…（3 天前）」 in another; the block chosen is
    # the history row, which says nothing about not finding anything, and a three-day-old number was reported as
    # this turn's answer while the site was saying it had found nothing. Still anchored to the key: a page-wide
    # scan would fire on an unrelated 「暂无评价」.
    nearby = [str(t) for t in (found.get("arounds") or [found.get("around") or ""]) if str(t).strip()]
    if any(p in text.lower() for text in nearby for p in patterns):
        return verdict("done_unverified", "no_result", evidence)   # "未找到订单 <key>": said as such
    if block is None:
        return verdict("done_unverified", "condition_unanswered" if found.get("capped") else "answer_not_in_one_block", evidence)
    evidence["block_sha256"] = hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]
    evidence["block_kind"] = "row" if found.get("via_row") else "smallest_element"
    if not _key_stands_alone(block, key):
        return verdict("done_unverified", "key_not_matched", evidence)
    if any(p in block.lower() for p in patterns):
        return verdict("done_unverified", "no_result", evidence)
    if any(block.count(label) > 1 for label in labels.values()):
        # The label written twice in the part that holds the key: two records, or an answer and a hint -- never a guess.
        return verdict("done_unverified", "answer_ambiguous", evidence)
    conditions: list[dict[str, Any]] = []
    for post in filled:
        kind, value = post["kind"], post["value"]
        if kind == "url_contains":
            holds = value in shown
        elif kind == "text_contains":
            holds = value in block
        elif kind == "element":
            strict = getattr(runtime, "target_count_strict", None)
            try:
                holds = callable(strict) and int(await strict(value)) > 0
            except Exception:  # noqa: BLE001
                holds = False
        else:
            holds = False   # a download is not a lookup's evidence
        conditions.append({"kind": kind, "value": value[:80], "holds": bool(holds)})
    evidence["conditions"] = conditions
    if not conditions or any(not c["holds"] for c in conditions):
        return verdict("done_unverified", "condition_not_met", evidence)
    values = {field: _extract(block, label) for field, label in labels.items()}
    if any(not v for v in values.values()):
        return verdict("done_unverified", "answer_not_found", {**evidence, "missing": [f for f, v in values.items() if not v]})
    return verdict("done_verified", "", evidence, values)


def _open_runtime(binding: Any) -> Any:
    """The browser this binding already has open, or None -- never opening one (a check must not start a browser)."""
    entry = getattr(_browser_sessions, "_entries", {}).get(binding.key())
    runtime = getattr(entry, "runtime", None)
    if runtime is None or getattr(runtime, "is_open", True) is False:
        return None
    return runtime


def _note_decision_trail(binding: Any, fields: dict[str, Any]) -> None:
    """4.7-A17: one decide_act ending, or one step PULY took, journaled with where it came from, who checked it and who
    has it now (audit.PROVENANCE_KEYS). Vocabulary ids and product refs only, never page words. With no live session
    (a check with no page open) it goes to the journal directly, as an owner's control does; never raises."""
    from ampuly.computer_use import browser_bridge

    event = {"type": "decision_trail", **{k: v for k, v in fields.items() if v not in (None, "")}}
    current = browser_bridge.gateway()
    try:
        current.annotate(binding.key(), event)
    except Exception:  # noqa: BLE001 - a session the Gateway has not seen: the journal alone keeps it
        journal = getattr(current, "_journal", None)
        try:
            if journal is not None:
                journal.append(binding.key(), {**event, "at": time.time()})
        except Exception:  # noqa: BLE001 - a record that cannot be written never stops the step
            pass


async def browser_decide_act(steps: int = DECIDER_STEPS_DEFAULT, note: str = "", check_only: bool = False,
                             item: str = "", task: Any = None) -> dict[str, Any]:
    """Q05 B3: PULY (JEV) takes the next low-risk steps on this connector step's page, and hands back the rest.

    Only on a step carrying the browser universal connector (JEV allowed there by default, policy).
    Each step is one question to JEV over the last snapshot's controls -- by product ids, never page text as keys --
    and one ordinary tool call through the Gateway (admitted, receipted, never retried). It clicks what is not a
    submit, picks a choice in a list that does not submit, scrolls, goes back, waits. It never types, never submits,
    never looks again by itself (asking JEV is not an observation):
    - a field to fill -> needs_text (the step's own model writes it; a secret -> hand_off);
    - a submit (click or list) -> needs_submit (the step proposes it with its expectation; the owner approves);
    - done -> code checks the step's postconditions (B5): done_verified or done_unverified with the reason (JEV saying
      so is not done); a person -> hand_off. `check_only` runs that check alone, without asking JEV;
    - JEV unsure, an answer that is not one of the offered ids, no progress for three steps, a refusal, an unknown
      outcome, a cancel, a switch turned off or a page off the step's sites -> it stops and says why.
    """
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.audit import decision_provenance, step_model
    from ampuly.computer_use.gateway import GatewayRefused
    from ampuly.llm.budget import ModelBudgetStop

    binding = BrowserBinding.for_task(task)
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    browser_bridge.note_task_flags(binding, metadata)
    # 4.7-A17: every ending says who suggested it, who checked it and who has it now -- in the result and the journal.
    model = step_model(metadata)

    def provenance(status: str, reason: str, *, answered: bool, check: bool = False, **journal_only: Any) -> dict[str, str]:
        fields = decision_provenance(status, reason, puly_answered=answered, step_model=model, check_only=check)
        _note_decision_trail(binding, {"outcome": status, "reason": reason, **fields, **({"item": item} if item else {}), **journal_only})
        return fields

    connector = _connector_of_task(task)
    if connector is None or (not check_only and dict(connector.get("decider", {}) or {}).get("jev", True) is False):
        raise GatewayRefused("decider_not_enabled_for_this_step", "only a step carrying the browser connector (with JEV on) uses the decider")
    mode = str(dict(connector.get("decider", {}) or {}).get("mode", "act") or "act")
    # Bulk P4: which row of the step's list this call is about. A key the order's own list does not carry is
    # refused here, which is how 「清单由代码生成，模型不能增删改」 holds: the only thing the model may say about
    # the list is which of ITS rows to work on.
    row = None
    if item:
        rows = browser_bridge.batch_items(task)
        field = browser_bridge.batch_item_key(task)
        if not rows or not field:
            raise GatewayRefused("not_a_batch_step",
                                 "this step does not work a list; item is for a step the owner set to batch mode")
        row = _item_row(task, item)
        if row is None:
            raise GatewayRefused("item_not_in_list",
                                 "that is not one of the rows this order's list carries; the list is the order's, not the model's")
        if browser_bridge.current_item(binding) != item:
            browser_bridge.note_item_start(binding, item, index=rows.index(row), total=len(rows))
    if check_only:
        # B5: the step's own model says the work is done -- code checks it, nothing is asked of JEV and nothing is done.
        # A check never opens a browser: without one open there is no page to check.
        _refuse_a_step_without_the_browser(task)
        runtime = _open_runtime(binding)
        if runtime is not None:
            browser_bridge.sync_lease(_browser_sessions, binding)
        found = await _check_done(binding, runtime, connector, task, item=item, row=row)
        if item:
            # The row is written the moment this item closes, and nothing later can change it.
            browser_bridge.note_item_result(
                binding, item, "verified" if found.get("status") == "done_verified" else "unverified",
                str(found.get("reason", "") or ""),
                conditions=browser_bridge.done_conditions_digest(_posts_of(connector, row) or []),
                evidence={"url": str(dict(found.get("evidence", {}) or {}).get("url", "") or "")})
        checked = provenance(str(found.get("status", "") or ""), str(found.get("reason", "") or ""), answered=False, check=True)
        return {**found, "steps": [], "mode": mode, **({"item": item} if item else {}), **checked}
    provider = browser_bridge.decision_provider()
    if provider is None:
        # PULY is not there to ask: the step's own model goes on with the browser tools (recorded, then refused).
        provenance("refused", "no_decision_provider", answered=False)
        raise GatewayRefused("no_decision_provider", "no decider is bound for this product; use the browser tools directly")
    limit = max(1, min(int(steps or DECIDER_STEPS_DEFAULT), DECIDER_STEPS_LIMIT))
    max_steps = int(dict(connector.get("handoff", {}) or {}).get("max_steps", 20) or 20)
    generation = browser_bridge.cancel_generation(binding)
    runtime = await _runtime_for(task)
    goal = str(connector.get("goal", "") or "").strip()
    performed: list[dict[str, Any]] = []
    history: list[dict[str, str]] = []
    last_result: dict[str, Any] | None = None
    seen_progress: list[str] = []
    # Whether PULY answered this round's question (a choice, or 「没把握」): what the ending came from; and the kind of
    # step its answer chose (click, select, ...), so the words say what it suggested.
    puly_answered = False
    puly_op = ""

    def finish(status: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        out = {"status": status, "reason": reason, "steps": performed, "mode": mode, **extra}
        if last_result is not None:
            out["page"] = last_result
        target = dict(extra.get("target", {}) or {})
        handed = status == "needs_submit"
        # A submit handed to the owner keeps the control's ref (a product id) and the page state it was pointed at (the
        # snapshot digest a proposal binds to), so the approval can be tied back to it -- and a later submit that reuses
        # the ref on another page is not taken for it.
        out.update(provenance(status, reason, answered=puly_answered, function=puly_op if puly_answered else "",
                              ref=str(target.get("ref", "") or "") if handed else "",
                              decided_on=str(dict(runtime.decision_rows() or {}).get("digest", "") or "")[:16] if handed else ""))
        return out

    async def finish_done(said: str) -> dict[str, Any]:
        # B5: JEV says the goal is met -- code checks the step's postconditions; its saying so is not done.
        checked = await _check_done(binding, runtime, connector, task)
        return finish(checked["status"], checked["reason"] or said, said=said,
                      **{key: value for key, value in checked.items() if key in {"evidence", "output"}})

    def gate(*, before_asking: bool = True) -> str:
        """Everything re-checked before each question and each action. '' when the step may go on."""
        browser_bridge.note_task_flags(binding, dict(getattr(task, "metadata", {}) or {}))
        if browser_bridge.task_flag(binding, "browser_allow_decider") is False:
            return "decider_switched_off"
        if not browser_bridge.task_flag(binding, "browser_allowed_domains"):
            return "no_delegated_hosts"
        if browser_bridge.cancel_generation(binding) != generation:
            return "cancelled"
        if browser_bridge.decider_held(binding):
            return "decider_unavailable"
        if before_asking and browser_bridge.decisions_made(binding) >= max_steps:
            return "connector_step_limit"
        browser_bridge.sync_lease(_browser_sessions, binding)
        if not browser_bridge.agent_may_act(_browser_sessions, binding):
            return "agent_not_in_control"
        if browser_bridge.outcome_unresolved(binding):
            return "unresolved_outcome"
        if not runtime.decision_rows().get("rows"):
            return "no_observation"
        if not _page_hosts_allowed(runtime, connector, browser_bridge.task_flag(binding, "browser_allowed_domains")):
            return "off_connector_domain"
        return ""

    for _ in range(limit):
        # A new round: until PULY answers this round's question, nothing it said before is what this ends on -- a gate
        # that stops here stops before PULY was asked again, even when its last answer was already carried out.
        puly_answered, puly_op = False, ""
        reason = gate()
        if reason:
            return finish("stopped", reason)
        view = runtime.decision_rows()
        options, left_out = decision_options(list(view.get("rows", []) or []))
        by_id = {option["id"]: option for option in options}
        from ampuly.computer_use.audit import _safe_page

        try:
            page = await runtime.decision_page_text(_DECIDER_PAGE_CHARS)
        except Exception as exc:  # noqa: BLE001 - a page that went away mid-loop: what was done still reaches the model
            return finish("stopped", "page_unreadable", detail=type(exc).__name__)
        try:
            decision = await provider(task=task, goal=goal, note=str(note or "")[:300],
                                      page={"url": _safe_page(view.get("url", "")), "title": page.get("title", ""),
                                            "text": page.get("text", ""), "text_omitted": page.get("omitted", 0)},
                                      options=options, options_omitted=left_out + int(dict(view.get("omitted", {}) or {}).get("over_cap", 0) or 0),
                                      history=history[-5:])
        except ModelBudgetStop as exc:
            browser_bridge.hold_decider(binding, "budget_stopped")
            return finish("stopped", "decider_budget_stopped", detail=str(exc)[:160])
        except Exception as exc:  # noqa: BLE001 - asked, and how it ended is not known: held, never asked again here
            browser_bridge.hold_decider(binding, "decider_failed")
            return finish("stopped", "decider_unavailable", detail=type(exc).__name__)
        decision = dict(decision or {})
        browser_bridge.note_decision(binding, {"function": str(by_id.get(decision.get("choice", ""), {}).get("op", "") or "none"),
                                               "model": str(decision.get("model", "") or ""), "outcome": str(decision.get("status", "")),
                                               "decided_on": str(view.get("digest", "") or "")[:16]})
        status = str(decision.get("status", "") or "")
        # A choice or 「没把握」 is PULY's answer; a failure, an unknown ending or a request too large to ask is not.
        puly_answered = status in {"decided", "abstained"}
        if status == "unknown":
            # Asked, and how it ended is not known: not asked again this step (it may have been answered and billed).
            browser_bridge.hold_decider(binding, "outcome_unknown")
            return finish("stopped", "decider_unavailable", detail=str(decision.get("error", ""))[:160])
        if status != "decided":
            return finish("hand_back", "decider_not_sure" if status == "abstained" else f"decider_{status or 'failed'}",
                          detail=str(decision.get("error", ""))[:160])
        if decision.get("goal_met") is True:
            return await finish_done("decider_says_goal_met")
        choice = by_id.get(str(decision.get("choice", "") or ""))
        if choice is None:
            return finish("hand_back", "decider_answer_not_offered")
        op = str(choice["op"])
        puly_op = op
        if op == "done":
            return await finish_done("decider_chose_done")
        if op == "human":
            return finish("hand_off", "decider_chose_a_person")
        if op == "fill":
            if dict(choice.get("state", {}) or {}).get("fill") == "sensitive":
                return finish("hand_off", "a_secret_field", field={"ref": choice["ref"], "label": choice["label"]})
            return finish("needs_text", "decider_chose_a_field", field={"ref": choice["ref"], "label": choice["label"]})
        if mode == "suggest":
            return finish("suggested", "", choice={k: choice.get(k) for k in ("op", "ref", "label", "choice") if choice.get(k) is not None})
        reason = gate(before_asking=False)
        if reason:
            return finish("stopped", reason)
        selector = f"ref:{choice['ref']}" if choice.get("ref") else ""
        still_ok = (lambda: browser_bridge.cancel_generation(binding) == generation)
        fence = getattr(runtime, "fence_writes", None)
        fenced_before = runtime.writes_fenced() if hasattr(runtime, "writes_fenced") else 0
        try:
            if op == "click":
                element = await _click_target(runtime, selector)
                if browser_bridge.classify_click(element) == "submit" or browser_bridge.names_a_decision(element):
                    return finish("needs_submit", "decider_chose_a_submit", target={"ref": choice["ref"], "label": choice["label"]})
                if callable(fence):
                    fence(True)
                last_result = await _through_gateway(task, "browser_click", _target(selector), lambda rt: rt.click(selector=selector),
                                                     resolve_kind=_click_kind(selector, task), target_check=_click_still(selector), still_ok=still_ok)
            elif op == "select":
                index = int(str(choice["id"]).rsplit(".", 1)[1])
                if await _select_kind(selector, index=index)(runtime) == "submit":
                    return finish("needs_submit", "decider_chose_a_submitting_choice", target={"ref": choice["ref"], "label": choice["label"]})
                if callable(fence):
                    fence(True)
                last_result = await _through_gateway(task, "browser_select_option", _target(selector),
                                                     lambda rt: rt.select_option(selector=selector, value=None, label=None, index=index),
                                                     resolve_kind=_select_kind(selector, index=index), still_ok=still_ok)
            elif op == "scroll":
                last_result = await _through_gateway(task, "browser_scroll", {"amount": 800, "direction": "down", "to_bottom": False},
                                                     lambda rt: rt.scroll(amount=800, direction="down", to_bottom=False), still_ok=still_ok)
            elif op == "back":
                last_result = await _through_gateway(task, "browser_navigate_back", {}, lambda rt: rt.navigate_back(), still_ok=still_ok)
            elif op == "wait":
                last_result = await _through_gateway(task, "browser_wait_for", {"selector": "", "state": "visible"},
                                                     lambda rt: rt.wait_for(selector=None, timeout_seconds=3.0, state="visible"), still_ok=still_ok)
            else:
                return finish("hand_back", "decider_answer_not_offered")
        except GatewayRefused as exc:
            performed.append({"op": op, "ref": choice.get("ref", ""), "outcome": "refused", "reason": exc.reason})
            return finish("stopped", exc.reason)
        except BrowserElementNotFound:
            performed.append({"op": op, "ref": choice.get("ref", ""), "outcome": "not_found"})
            return finish("stopped", "target_not_found")
        except Exception as exc:  # noqa: BLE001 - sent, and how it ended is not known: the Gateway holds it; never retried
            if op in {"scroll", "back", "wait"}:
                # A read-only step that failed changed nothing on the site: said as a failure, not as an unknown write.
                performed.append({"op": op, "ref": "", "outcome": "failed", "error": type(exc).__name__})
                return finish("stopped", "action_failed")
            performed.append({"op": op, "ref": choice.get("ref", ""), "outcome": "unknown", "error": type(exc).__name__})
            return finish("stopped", "outcome_unknown")
        finally:
            if callable(fence):
                fence(False)
        if hasattr(runtime, "writes_fenced") and runtime.writes_fenced() > fenced_before:
            # The control tried to write to the server and was stopped: the step proposes it (approval) or a person does.
            performed.append({"op": op, "ref": choice.get("ref", ""), "outcome": "write_fenced"})
            return finish("needs_submit", "control_writes_to_the_server", target={"ref": choice.get("ref", ""), "label": choice.get("label", "")})
        performed.append({"op": op, "ref": choice.get("ref", ""), "outcome": "acted"})
        # 4.7-A17: PULY suggested it, the gates and the Gateway let it through, and it was done -- nobody else took it.
        _note_decision_trail(binding, {"outcome": "acted", "function": op, "suggested_by": "puly", "reviewed_by": "code_check",
                                       "fallback_to": "none", **({"item": item} if item else {})})
        history.append({"op": op, "label": str(choice.get("label", "") or choice.get("choice", ""))[:40]})
        if browser_bridge.outcome_unresolved(binding):
            return finish("stopped", "unresolved_outcome")
        after = runtime.decision_rows()
        progress = "|".join([str(after.get("digest", "")), str(after.get("url", "")), op, str(choice.get("ref", ""))])
        seen_progress.append(progress)
        if op not in {"select", "scroll"} and seen_progress.count(progress) >= _DECIDER_STUCK_AFTER:
            return finish("stopped", "no_progress")
    return finish("stopped", "step_budget_for_this_call")


_OBSERVE_TEXT_DEFAULT = 4_000
_OBSERVE_TEXT_LIMIT = 6_000


async def browser_observe(max_chars: int = _OBSERVE_TEXT_DEFAULT, task: Any = None) -> dict[str, Any]:
    """The page as the model sees it, written nowhere (Q05 B1).

    browser_snapshot may write a file, so a company step may not call it; this one only reads. It is the observation
    that settles an action whose outcome was not known (the Gateway clears that lock on a model-facing read). The text
    is held to 6,000 characters so the controls fit beside it in one tool message (the runtime keeps 12,000).
    """
    chars = max(500, min(int(max_chars or _OBSERVE_TEXT_DEFAULT), _OBSERVE_TEXT_LIMIT))
    return await _through_gateway(task, "browser_observe", {"max_chars": chars},
                                  lambda rt: rt.snapshot(filename=None, max_chars=chars))


async def _click_target(runtime: Any, selector: str) -> dict[str, Any] | None:
    """What a click on this target would hit: the live element, with what the last snapshot said about it.

    The live element is the one the click resolves (same frame, same first match), so a submit reached by another
    spelling of its selector, or one the snapshot never listed, is still seen as a submit. Where the two disagree
    the stricter reading wins: either one saying submit is a submit. None when nothing describes the target.
    """
    lookup = getattr(runtime, "element_for", None)
    row = lookup(selector) if callable(lookup) else None
    describe = getattr(runtime, "describe_target", None)
    live = await describe(selector) if callable(describe) else None
    _refuse_a_moved_row(row, live)
    return _merge_target(row, live)


def _refuse_a_moved_row(row: dict[str, Any] | None, live: dict[str, Any] | None) -> None:
    """The row around a control (its table row, list item, card) must still say what it said in the snapshot."""
    before = str((row or {}).get("context", "") or "")
    now = str((live or {}).get("context", "") or "")
    if before and now and before != now:
        raise BrowserElementNotFound("the row around this control changed since the snapshot (the list may have moved); "
                                     "nothing was sent; take a new browser_observe")


_READER_ONLY_KEYS = frozenset({"name", "state", "options", "options_total"})


def _merge_target(row: dict[str, Any] | None, live: dict[str, Any] | None) -> dict[str, Any] | None:
    """A snapshot row and a live description as one target; either one saying submit makes it a submit."""
    if not live and not row:
        return None
    # Q05 B1: the reader's own additions (a name, a state, a list's choices) never reach classification or an approval:
    # they would turn a labelled checkbox into a submit, or put someone's draft into the owner's prompt.
    merged: dict[str, Any] = {key: value for key, value in dict(row or {}).items() if key not in _READER_ONLY_KEYS}
    for key, value in dict(live or {}).items():
        if key in {"submits", "editable", "in_dialog", "dialog_choice", "clickable"}:
            merged[key] = bool(value) or bool((row or {}).get(key))
        elif key in {"form_method", "form_role"} or value not in ("", None):
            merged[key] = value
    if row and live:
        # Both texts are read for effect words: the snapshot's label and the live one -- except a text control's, whose
        # snapshot text can be what someone typed (a React textarea's content), never read back (C27).
        texts = (live.get("text", ""),) if str(live.get("tag", "") or "") in {"textarea", "input"} else (row.get("text", ""), live.get("text", ""))
        merged["text"] = " ".join(dict.fromkeys(str(t) for t in texts if t))[:240]
        if str(row.get("type", "") or "").lower() in {"submit", "image"}:
            merged["type"] = row["type"]
        merged["in_dialog"] = bool(live.get("in_dialog")) or bool(row.get("in_dialog"))
        merged["plain_link"] = bool(live.get("plain_link")) and bool(row.get("plain_link", True))
        merged["dialog_info"] = bool(live.get("dialog_info")) and bool(row.get("dialog_info", True))
    return merged


def _refuse_undeclared_control(task: Any, element: dict[str, Any] | None) -> None:
    """P2 护栏一: a step that declared which buttons it may click may click no others.

    A step that declared nothing -- every step until one does -- is not touched by this at all. Clicks by sight
    are not checked here and do not need to be: that path already refuses every submit and every control with no
    accessible name, so an undeclared button cannot be reached by coordinate either.
    """
    from ampuly.computer_use import browser_bridge

    binding = BrowserBinding.for_task(task)
    # Read from the task each time, as every other scope on this path is: a click may reach here before anything
    # else on this turn has refreshed the flags, and a declaration that is not read is a declaration not enforced.
    browser_bridge.note_task_flags(binding, dict(getattr(task, "metadata", {}) or {}) if task is not None else {})
    declared = browser_bridge.task_flag(binding, browser_bridge.CONNECTOR_CONTROLS_FLAG)
    if declared is None or not browser_bridge.control_refusal(element, declared):
        return
    from ampuly.computer_use.gateway import GatewayRefused

    named = "、".join(str(item) for item in list(declared)[:6]) or "（一个都没有）"
    raise GatewayRefused("undeclared_control",
                         f"this step may only click the buttons its template declares: {named}. "
                         "Add the button to the step's connector, or hand the page to a person.")


def _click_kind(selector: str, task: Any = None) -> Callable[[Any], Any]:
    """A submit is an external effect (X06); the target is classified from the live element, and a target nothing
    describes is refused rather than clicked as a draft (C08)."""
    from ampuly.computer_use.browser_bridge import classify_click

    async def resolve(runtime: Any) -> str:
        element = await _click_target(runtime, selector)
        _refuse_undeclared_control(task, element)
        return classify_click(element)

    return resolve


async def browser_click(selector: str, expect: dict[str, Any] | None = None, task: Any = None) -> dict[str, Any]:
    from ampuly.computer_use import browser_bridge

    if browser_bridge.effect_proposer(task) is not None:
        binding = BrowserBinding.for_task(task)
        browser_bridge.sync_lease(_browser_sessions, binding)
        runtime = await _runtime_for(task)
        _browser_sessions.note_agent_operating(binding)
        # C05 and control first, as admit does: a page that moved since the snapshot is not proposed from.
        sequence = await browser_bridge.observe_before_proposal(_browser_sessions, binding, runtime, _target(selector))
        try:
            element = await _click_target(runtime, selector)
            # P2 护栏一 before the classification: a button the template never declared is refused whether or not
            # it would have gone to the owner for approval.
            _refuse_undeclared_control(task, element)
            submit = browser_bridge.classify_click(element) == "submit"
        except Exception as exc:
            browser_bridge.gateway_annotate(binding, {"type": "refused", "reason": getattr(exc, "reason", "target_not_resolved"),
                                                      "kind": "select_candidate", "error": type(exc).__name__, "sequence": sequence})
            raise
        if submit:
            # X06: a submit is proposed for approval, not clicked. The approved
            # effect's provider performs the click after re-observing the page
            # and holds `expect` against the page afterwards (C08, C11). The owner
            # is shown the live element, whatever spelling the model used.
            return await browser_bridge.propose_submit(_browser_sessions, binding, runtime, task, selector=selector, element=element,
                                                       expect=expect if isinstance(expect, dict) else None)
    # Classified again inside admit: the page may have moved since, and the stricter answer is the one that counts.
    return await _through_gateway(task, "browser_click", _target(selector), lambda rt: rt.click(selector=selector),
                                  resolve_kind=_click_kind(selector, task), target_check=_click_still(selector), verify=_page_expectation(expect))


async def browser_submit_enter(selector: str, expect: dict[str, Any] | None = None, task: Any = None) -> dict[str, Any]:
    """Propose Enter on an already-filled field through the submit Effect path.

    Text entry remains a separate draft action; this tool contains no text and
    cannot be used to smuggle a message past the approval boundary.
    """
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.gateway import GatewayRefused

    binding = BrowserBinding.for_task(task)
    browser_bridge.sync_lease(_browser_sessions, binding)
    runtime = await _runtime_for(task)
    _browser_sessions.note_agent_operating(binding)
    sequence = await browser_bridge.observe_before_proposal(_browser_sessions, binding, runtime, _target(selector))
    try:
        element = await _click_target(runtime, selector)
        if not element.get("editable") or element.get("tag") in {"button", "a", "select"}:
            raise GatewayRefused("enter_target_not_editable", "browser_submit_enter only acts on an editable field")
    except Exception as exc:
        browser_bridge.gateway_annotate(binding, {"type": "refused", "reason": getattr(exc, "reason", "target_not_resolved"),
                                                  "kind": "submit", "error": type(exc).__name__, "sequence": sequence})
        raise
    return await browser_bridge.propose_submit(
        _browser_sessions, binding, runtime, task, selector=selector, element=element,
        expect=expect if isinstance(expect, dict) else None, operation="press_enter",
    )


def _click_still(selector: str) -> Callable[[Any, Any, dict[str, Any]], None]:
    """At the moment of sending, the click's target must still classify as what it was admitted as."""
    from ampuly.computer_use.browser_bridge import classify_click

    def check(runtime: Any, admission: Any, live: dict[str, Any]) -> None:
        lookup = getattr(runtime, "element_for", None)
        row = lookup(selector) if callable(lookup) else None
        _refuse_a_moved_row(row, live)
        if classify_click(_merge_target(row, live)) != admission.action.kind:
            raise BrowserElementNotFound("the target changed while it was getting ready (it is no longer what was admitted); "
                                         "nothing was sent; take a new browser_snapshot")

    return check


def _type_still(runtime: Any, admission: Any, live: dict[str, Any]) -> None:
    """At the moment of typing, the target must still be a text field."""
    if not live.get("editable") or live.get("submits") or live.get("tag") in {"button", "a", "select"}:
        raise BrowserElementNotFound("the target is no longer a text field; nothing was typed; take a new browser_snapshot")


def _type_kind(task: Any, selector: str, text: str, *, press_enter: bool, clear_existing: bool) -> Callable[[Any], Any]:
    """Typing is a draft only into a text field, and only while it commits nothing (C08).

    Enter submits a form or sends a chat message, and a line break typed key by key is an Enter; so either is a
    submit, which the Gateway refuses without an approved Effect claim. A target that is not a text field is
    refused outright: typing into it would click it (a button) under the name of a draft.
    """
    from ampuly.computer_use.gateway import GatewayRefused

    async def classify(runtime: Any) -> str:
        describe = getattr(runtime, "describe_target", None)
        described = await describe(selector) if callable(describe) else {}
        # A control is never typed into, whatever else it claims: typing key by key clicks it first.
        if not described.get("editable") or described.get("submits") or described.get("tag") in {"button", "a", "select"}:
            raise GatewayRefused("type_target_not_editable", "browser_type only types into a text field; use browser_click or browser_select_option for controls")
        # Q05 B3: on a connector step a secret (password, one-time code, card number) is a person's to type, never a model's.
        if described.get("sensitive") and _connector_of_task(task) is not None:
            raise GatewayRefused("sensitive_field_needs_a_person",
                                 "this field holds a secret; hand the page to a person (browser_request_human) to fill it")
        # fill() replaces the value, so its line breaks are content; typing key by key presses Enter for them.
        commits = press_enter or (not clear_existing and ("\n" in text or "\r" in text))
        return "submit" if commits else "type_draft"

    return classify


async def browser_type(
    selector: str,
    text: str,
    press_enter: bool = False,
    clear_existing: bool = True,
    task: Any = None,
) -> dict[str, Any]:
    from ampuly.computer_use.gateway import GatewayRefused

    # The text never enters the Gateway's events: it may be a password (C27).
    try:
        return await _through_gateway(
            task, "browser_type", {**_target(selector), "length": len(str(text or "")), "press_enter": bool(press_enter)},
            lambda rt: rt.type(selector=selector, text=text, press_enter=press_enter, clear_existing=clear_existing),
            resolve_kind=_type_kind(task, selector, str(text or ""), press_enter=bool(press_enter), clear_existing=bool(clear_existing)),
            target_check=_type_still,
        )
    except GatewayRefused as exc:
        if exc.reason != "external_effect_not_authorized":
            raise
        raise GatewayRefused(exc.reason, "Enter (or a line break typed key by key) submits the form or sends the message, so it is always "
                                         "refused here: type without press_enter. If the form has a submit control, submit with browser_click, "
                                         "which is proposed to the owner for approval. If the widget commits only on Enter (a search box with "
                                         "no button, a tag input, a chat send), first type the draft without Enter, then use "
                                         "browser_submit_enter, which uses the same owner approval path") from None


async def browser_take_screenshot(
    filename: str | None = None,
    full_page: bool = True,
    task: Any = None,
) -> dict[str, Any]:
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.gateway import GatewayRefused

    # C04: a picture of the page leaves the structured path only when the task
    # allows vision. Decided from the task, before any capture, never from an argument.
    binding = BrowserBinding.for_task(task)
    browser_bridge.note_task_flags(binding, dict(getattr(task, "metadata", {}) or {}) if task is not None else {})
    if not browser_bridge.task_flag(binding, "browser_allow_vision"):
        raise GatewayRefused("vision_not_allowed_for_this_task", "the task does not allow screenshots; use browser_snapshot")
    return await _through_gateway(task, "browser_take_screenshot", {"full_page": bool(full_page)},
                                  lambda rt: rt.take_screenshot(filename=filename, full_page=full_page))


async def browser_vision_act(instruction: str, task: Any = None) -> dict[str, Any]:
    """Act on the page by sight: a masked screenshot to the vision model, one grounded action back, both through the Gateway.

    Only with the task's leave (C04), only when the structured path is not
    there (C03), never with a raw screenshot (C27), inside the task's budget
    (C23). The model's reply is untrusted text parsed by the strict grammar;
    the Gateway admits the action against the observation it was decided on.
    """
    from ampuly.computer_use import browser_bridge
    from ampuly.computer_use.gateway import GatewayRefused

    binding = BrowserBinding.for_task(task)
    browser_bridge.note_task_flags(binding, dict(getattr(task, "metadata", {}) or {}) if task is not None else {})
    if not browser_bridge.task_flag(binding, "browser_allow_vision"):
        raise GatewayRefused("vision_not_allowed_for_this_task", "the task does not allow vision; use browser_snapshot and refs")
    if browser_bridge.vision_provider(task) is None:
        raise GatewayRefused("no_vision_provider", "no vision model is bound for this product; use the structured tools")
    runtime = await _runtime_for(task)
    # C03: when the last structured snapshot of this page offered elements, the model sees no picture.
    if runtime.structured_path_available() and not browser_bridge.task_flag(binding, "browser_vision_preferred"):
        raise GatewayRefused("structured_path_available", "the last snapshot has interactive elements; act by ref, not by sight")
    # Step 1 (observe): a masked screenshot of the viewport, as its own step in the sequence.
    await _through_gateway(task, "browser_take_screenshot", {"full_page": False, "purpose": "vision"},
                           lambda rt: rt.take_screenshot(full_page=False))
    # Step 2: grounding inside the budget, admission at the Gateway, then the act.
    # The outer guard is re-checked after the model wait, before any pointer or key input.
    generation = browser_bridge.cancel_generation(binding)
    browser_bridge.sync_lease(_browser_sessions, binding)
    _browser_sessions.note_agent_operating(binding)
    admission, grounded = await browser_bridge.admit_vision(_browser_sessions, binding, runtime, task, instruction=instruction)
    summary = {"thought": str(grounded.thought or "")[:500], "model": grounded.model}
    if admission is None:
        return {"status": "finished" if grounded.finished else "hand_off", **summary}
    lease_guard = browser_bridge.dispatch_guard(_browser_sessions, binding, admission)
    guard = _compose_agent_dispatch_guard(
        lease_guard, lambda: browser_bridge.cancel_generation(binding) == generation
    )
    if not guard():
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False, before_effect=True)
        raise _control_moved()
    token = _AGENT_DISPATCH_GUARD.set(guard)
    try:
        result = await runtime.perform_vision_action(grounded.vision)
    except BrowserElementNotFound:
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False, before_effect=True)
        raise
    except BaseException:
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False)
        raise
    finally:
        _AGENT_DISPATCH_GUARD.reset(token)
    if not guard():
        browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=False)
        raise _control_moved()
    receipt = browser_bridge.settle(binding, admission, registry=_browser_sessions, ok=True, evidence={"result_keys": sorted(result)})
    _refuse_a_result_control_overtook(receipt)
    return {"status": "acted", "kind": admission.action.kind, **summary, **result}


async def browser_wait_for(
    selector: str | None = None,
    timeout_seconds: float = 10.0,
    state: str = "visible",
    task: Any = None,
) -> dict[str, Any]:
    return await _through_gateway(task, "browser_wait_for", {"selector": str(selector or "")[:256], "state": str(state)},
                                  lambda rt: rt.wait_for(selector=selector, timeout_seconds=timeout_seconds, state=state))


async def browser_scroll(
    amount: int = 800,
    direction: str = "down",
    to_bottom: bool = False,
    task: Any = None,
) -> dict[str, Any]:
    return await _through_gateway(task, "browser_scroll", {"amount": int(amount), "direction": str(direction), "to_bottom": bool(to_bottom)},
                                  lambda rt: rt.scroll(amount=amount, direction=direction, to_bottom=to_bottom))


async def browser_select_option(
    selector: str,
    value: str | None = None,
    label: str | None = None,
    index: int | None = None,
    task: Any = None,
) -> dict[str, Any]:
    return await _through_gateway(task, "browser_select_option", _target(selector),
                                  lambda rt: rt.select_option(selector=selector, value=value, label=label, index=index),
                                  resolve_kind=_select_kind(selector, value=value, label=label, index=index))


def _select_kind(selector: str, *, value: Any = None, label: Any = None, index: Any = None) -> Callable[[Any], Any]:
    """Q05 B3: a choice in a list is a draft unless making it submits -- a list that submits on change (its own
    handler attributes), or a choice whose words name an irreversible act. Then it is a submit, which the Gateway
    refuses without an approved Effect claim."""
    from ampuly.computer_use.browser_bridge import names_an_effect

    async def resolve(runtime: Any) -> str:
        describe = getattr(runtime, "describe_target", None)
        described = await describe(selector) if callable(describe) else {}
        if dict(described or {}).get("autosubmit"):
            return "submit"
        words = [str(label or ""), str(value or "")]
        if index is not None:
            lookup = getattr(runtime, "element_for", None)
            row = lookup(selector) if callable(lookup) else None
            choices = list((row or {}).get("options", []) or [])
            try:
                words.append(str(choices[int(index)]))
            except (IndexError, TypeError, ValueError):
                pass
        return "submit" if names_an_effect(" ".join(words)) else "select_candidate"

    return resolve


async def browser_switch_tab(index: int = 0, task: Any = None) -> dict[str, Any]:
    return await _through_gateway(task, "browser_switch_tab", {"index": int(index)}, lambda rt: rt.switch_tab(index))


async def browser_navigate_back(task: Any = None) -> dict[str, Any]:
    return await _through_gateway(task, "browser_navigate_back", {}, lambda rt: rt.navigate_back())


# X05 / V03: how long an agent waits for a person before giving the turn back to the model.
_HUMAN_POLL_SECONDS = 1.0
_HUMAN_WAIT_DEFAULT_MINUTES = 15.0
_HUMAN_WAIT_MAX_MINUTES = 120.0


def _human_wait_seconds(task: Any) -> float:
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    try:
        minutes = float(metadata.get("browser_human_wait_minutes", _HUMAN_WAIT_DEFAULT_MINUTES) or _HUMAN_WAIT_DEFAULT_MINUTES)
    except (TypeError, ValueError):
        minutes = _HUMAN_WAIT_DEFAULT_MINUTES
    return max(1.0, min(minutes, _HUMAN_WAIT_MAX_MINUTES)) * 60.0


async def browser_request_human(reason: str, task: Any = None) -> dict[str, Any]:
    """Hand this task's page to a person and wait until it comes back verified.

    The page stays where it is; the registry marks it awaiting_human, so every
    browser tool of this task is refused until a person hands it back and the
    system verifies; the owner's console is told and shows the page in the
    client. The call returns when the agent may go on (resumed), or when the
    wait ran out or the session was revoked (resumed False, with the state).
    Nothing the person does on the page is seen here.
    """
    from ampuly.computer_use import browser_bridge as _bridge

    binding = BrowserBinding.for_task(task)
    state = _browser_sessions.state_of(binding)
    if not state.get("live"):
        raise BrowserSessionRefused(f"no page to hand over for {binding.describe()}: navigate first", reason="no_session")
    # Not through the agent's gate: a page already handed to a person still has facts to read
    # (its address), and asking again while the person holds it must not be a refusal.
    runtime = getattr(_browser_sessions._entries.get(binding.key()), "runtime", None)
    url_before, _title = await _bridge._page_facts(runtime)
    why = str(reason or "").strip()[:300] or "the agent asked for a person"
    _browser_sessions.hand_to_human(binding, note=why, facts=await _bridge.page_facts_for_handoff(runtime))
    _bridge.sync_lease(registry=_browser_sessions, binding=binding)
    _bridge.gateway_annotate(binding, {"type": "handoff_requested", "by": "agent", "reason": why,
                                       "host": (urlsplit(url_before).hostname or "")})
    started = time.monotonic()
    budget = _human_wait_seconds(task)
    resumed = False
    while True:
        current = _browser_sessions.state_of(binding).get("state", "")
        if current in {"identity_verified", "agent_operating"}:
            resumed = True
            break
        if current in {"revoked", "closed", "lease_expired"} or time.monotonic() - started >= budget:
            break
        if current in {"awaiting_human", "human_returned"}:
            # The agent is waiting on this person for the task's wait budget: the browser they may be logging
            # into (a QR code scanned on a phone moves nothing in the pane) stays open that long (C16).
            _browser_sessions.touch(binding)
        await asyncio.sleep(_HUMAN_POLL_SECONDS)
    waited = round(time.monotonic() - started, 1)
    final = _browser_sessions.state_of(binding)
    if not resumed:
        outcome = "human_timeout" if final.get("state") in {"awaiting_human", "human_returned"} else str(final.get("state", ""))
        _bridge.gateway_annotate(binding, {"type": "handoff_not_returned", "outcome": outcome, "waited_seconds": waited})
        return {"resumed": False, "outcome": outcome, "state": final.get("state", ""), "waited_seconds": waited,
                "note": "the page was not handed back verified; do not ask the user for credentials, report and stop"}
    _bridge.sync_lease(registry=_browser_sessions, binding=binding)
    url_after, title_after = await _bridge._page_facts(runtime)
    _bridge.gateway_annotate(binding, {"type": "handoff_returned", "waited_seconds": waited, "url_changed": url_after != url_before})
    return {"resumed": True, "outcome": "verified", "state": final.get("state", ""), "waited_seconds": waited,
            "url_changed": url_after != url_before, "host": (urlsplit(url_after).hostname or ""), "title": str(title_after or "")[:120],
            "note": "take a fresh browser_snapshot before acting: the person may have moved the page"}


async def browser_close(task: Any = None) -> dict[str, Any]:
    """Close this task's browser, and only this task's.

    Closing is per binding for the same reason opening is: a shared close
    would let one order end another order's half-finished login.
    """
    binding = BrowserBinding.for_task(task)
    # The agent's close: a browser a person holds, one handed back or one whose lease ended is not the agent's to close.
    closed = await _browser_sessions.close(binding, actor="agent")
    return {"closed": True, "had_session": bool(closed), "binding": binding.describe()}


def create_browser_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="browser_navigate",
            description="Open a page in the configured local browser and return a text snapshot.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"},
                    "wait_until": {
                        "type": "string",
                        "description": "Playwright wait condition",
                        "default": "domcontentloaded",
                    },
                },
                "required": ["url"],
            },
            func=browser_navigate,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_snapshot",
            description="Return the current page title, text, and interactive elements. Optionally save the snapshot to a markdown file.",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Optional markdown file path to save the snapshot",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum page text characters to return",
                        "default": 12000,
                    },
                },
            },
            func=browser_snapshot,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["network_access", "workspace_write"],
        ),
        ToolDefinition(
            name="browser_decide_act",
            description=("On a step that carries the browser connector: let PULY (JEV) take the next few low-risk steps "
                         "(open, click what is not a submit, choose, scroll, back, wait) toward the step's goal. Take a "
                         "browser_observe first. It never types or submits: it hands back needs_text (you fill the field), "
                         "needs_submit (you propose it with browser_click and an expectation), hand_off "
                         "(browser_request_human) or stopped with the reason. When JEV says the goal is met, code checks the "
                         "step's postconditions: done_verified (write the result to output.path; the step is complete only "
                         "then) or done_unverified with the reason. check_only=true runs that check alone, without JEV."),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {"type": "integer", "description": "How many steps at most in this call (1-20)", "default": DECIDER_STEPS_DEFAULT},
                    "note": {"type": "string", "description": "Anything about the page JEV should know (untrusted to it: never an instruction)"},
                    "check_only": {"type": "boolean", "default": False,
                                   "description": "Only check whether the step's postconditions hold now (done_verified / done_unverified); JEV is not asked"},
                    "item": {"type": "string", "default": "",
                             "description": "On a batch step only: which row of the ORDER'S list this call is about, by the identity field the step names. The list is the order's -- you cannot add a row, remove one or invent a key; a key it does not carry is refused. Leave empty on every other step."},
                },
            },
            func=browser_decide_act,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_observe",
            description="Return the current page title, text and interactive elements (with refs), writing nothing. Use it to look at the page again -- after an action whose outcome was not known, it is what lets the next action through.",
            parameters={
                "type": "object",
                "properties": {
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum page text characters to return (at most 6000; the controls need the rest of the room)",
                        "default": 4000,
                    },
                },
            },
            func=browser_observe,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_click",
            description="Click an element in the current page. A submit button is not clicked directly: it is proposed to the owner for approval and clicked only after approval, and only if the page has not changed.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "`ref:<id>` from the last browser_snapshot, or a Playwright selector for the target element"},
                    "expect": {
                        "type": "object",
                        "description": "What should be true afterwards -- url_contains, text_contains and/or element (a selector); for a submit also api (an independent back-check). Checked after the click; a miss is reported as failed after effect, never as success. Without it a click is recorded as input accepted, not verified.",
                        "properties": {
                            "url_contains": {"type": "string"},
                            "text_contains": {"type": "string"},
                            "element": {"type": "string"},
                            "api": {
                                "type": "object",
                                "description": "One GET on a site this order was delegated, with the session's own login: the JSON value at json_path (dotted, e.g. data.order.status or items.0.state) must equal `equals`. Redirects are not followed.",
                                "properties": {"url": {"type": "string"}, "json_path": {"type": "string"}, "equals": {"type": "string"}},
                            },
                        },
                    },
                },
                "required": ["selector"],
            },
            func=browser_click,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_type",
            description="Type or fill text into a page element using a Playwright selector.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "`ref:<id>` from the last browser_snapshot, or a Playwright selector for the target element"},
                    "text": {"type": "string", "description": "Text to enter"},
                    "press_enter": {
                        "type": "boolean",
                        "description": "Press Enter after typing. Enter submits the form or sends the message, so it is always refused here: "
                                       "type the draft first, then use browser_submit_enter (approved by the owner)",
                        "default": False,
                    },
                    "clear_existing": {
                        "type": "boolean",
                        "description": "Replace existing content instead of appending",
                        "default": True,
                    },
                },
                "required": ["selector", "text"],
            },
            func=browser_type,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_submit_enter",
            description="Press Enter on an already-filled editable field. This is an external side effect: it is proposed to the owner and runs only after approval if the page, target and form values are unchanged.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "`ref:<id>` from the last browser_snapshot, or a Playwright selector for the already-filled field"},
                    "expect": {
                        "type": "object",
                        "description": "What should be true afterwards -- url_contains, text_contains and/or element; optionally an independent api back-check.",
                        "properties": {
                            "url_contains": {"type": "string"},
                            "text_contains": {"type": "string"},
                            "element": {"type": "string"},
                            "api": {"type": "object"},
                        },
                    },
                },
                "required": ["selector"],
            },
            func=browser_submit_enter,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_take_screenshot",
            description="Save a screenshot of the current page.",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Optional screenshot path"},
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page",
                        "default": True,
                    },
                },
            },
            func=browser_take_screenshot,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["network_access", "workspace_write"],
        ),
        ToolDefinition(
            name="browser_wait_for",
            description="Wait for a selector or page load state before continuing.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Optional selector to wait for. If omitted, waits for page load state.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Maximum wait time in seconds (at most 30)",
                        "default": 10.0,
                        "maximum": 30,
                    },
                    "state": {
                        "type": "string",
                        "description": "Selector state (attached/visible/hidden/detached) or load state (load/domcontentloaded/networkidle)",
                        "default": "visible",
                    },
                },
            },
            func=browser_wait_for,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_scroll",
            description="Scroll the current page up or down, or jump to the bottom.",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": "Scroll distance in pixels",
                        "default": 800,
                    },
                    "direction": {
                        "type": "string",
                        "description": "Scroll direction: down or up",
                        "default": "down",
                    },
                    "to_bottom": {
                        "type": "boolean",
                        "description": "Jump directly to the bottom of the page",
                        "default": False,
                    },
                },
            },
            func=browser_scroll,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_select_option",
            description="Choose an option in a select element by value, label, or index.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "`ref:<id>` from the last browser_snapshot, or a selector for the <select> element"},
                    "value": {"type": "string", "description": "Option value to choose"},
                    "label": {"type": "string", "description": "Visible label to choose"},
                    "index": {"type": "integer", "description": "Zero-based option index"},
                },
                "required": ["selector"],
            },
            func=browser_select_option,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_vision_act",
            description="Act on the page by sight when the snapshot offers nothing to act on: one instruction, one grounded "
                        "action (click, type, scroll, hotkey) decided from a masked screenshot. Refused when the task does not "
                        "allow vision or the last snapshot has interactive elements; the reply may also be finished or hand_off.",
            parameters={
                "type": "object",
                "properties": {"instruction": {"type": "string", "description": "what to do on the page, in words"}},
                "required": ["instruction"],
            },
            func=browser_vision_act,
            category="browser",
            # It writes a screenshot file, as browser_take_screenshot does: not for company mode.
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["network_access", "workspace_write"],
        ),
        ToolDefinition(
            name="browser_switch_tab",
            description="Make one of this session's tabs (opened by the page on an allowed host) the active page. The last browser_snapshot lists them under `tabs`.",
            parameters={
                "type": "object",
                "properties": {"index": {"type": "integer", "description": "tab index from the last snapshot's `tabs`"}},
                "required": ["index"],
            },
            func=browser_switch_tab,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_request_human",
            description="Hand the current page to a person and wait for it to come back: a login, a verification code, a captcha, "
                        "a choice only they can make. The page stays open in the owner's client; every browser tool is refused "
                        "until the person hands it back and the system verifies. Never ask the user for a password or code in "
                        "chat -- call this instead. Returns resumed=true when you may continue (take a fresh snapshot first).",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "what the person has to do on the page, in words"}},
                "required": ["reason"],
            },
            func=browser_request_human,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            # A wait on the registry: no network, no file, no side effect of its own.
            permission_effects=["runtime_internal"],
        ),
        ToolDefinition(
            name="browser_navigate_back",
            description="Go back to the previous page in browser history.",
            parameters={"type": "object", "properties": {}},
            func=browser_navigate_back,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_close",
            description="Close the active local browser session and clear in-memory page state.",
            parameters={"type": "object", "properties": {}},
            func=browser_close,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["runtime_internal"],
        ),
    ]
