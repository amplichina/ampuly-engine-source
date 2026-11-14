"""A browser submit as a real side effect: proposed, approved, claimed, then dispatched (4.3 X06).

A submit click or Enter key is not performed by the tool that asked for it.
The tool proposes it into the owner's approval inbox with a preview of what
will be submitted and the page state it was decided on; the owner's answer is
consumed by the existing Effect approval path, which claims the effect
durably and calls this provider's ``apply``. ``apply`` re-observes the page:
if the page has changed since the approval the input is not sent (C09), and
the effect ends as ``failed`` with a reason the owner can read. An input that
raised proves nothing either way and stays the Effect Gateway's
``outcome_unknown``; it is never retried by this layer (G03, C28).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from .models import EffectRecord, ProviderOutcome, ProviderResult, SimulationStatus, VerificationStatus

BROWSER_SUBMIT = "browser.submit"
PROVIDER_NAME = "browser:submit"
REQUIRED = ("binding_key", "selector", "observation_hash", "element")


async def _wait_for_write_completion(runtime: Any, before: dict[str, int]) -> dict[str, int]:
    """Observe a pending request for at most three seconds; never send input again."""
    reader = getattr(runtime, "request_report", None)
    if not callable(reader):
        return before
    report = dict(reader() or {})
    for _ in range(60):
        if (int(report.get("failed", 0) or 0) > int(before.get("failed", 0) or 0)
                or int(report.get("pending_writes", 0) or 0) == 0):
            return report
        await asyncio.sleep(0.05)
        report = dict(reader() or {})
    return report


def _default_registry() -> Any:
    from ampuly.layer4_tools import browser as browser_tools

    return browser_tools._browser_sessions


def _still_approved(approved: dict[str, Any], approved_hash: str) -> Any:
    """The check an approved click runs at the moment of sending: the same control, the same destination, the same form values."""
    from ampuly.computer_use.browser_bridge import destination_of, form_state_digest
    from ampuly.layer4_tools.browser import BrowserElementNotFound

    keys = ("tag", "type", "text", "label", "editable", "submits", "form_method")
    destination = destination_of(approved)

    def check(live: dict[str, Any], form: Any) -> None:
        if any(live.get(key) != approved.get(key) for key in keys):
            raise BrowserElementNotFound("the approved control changed while the click waited for it; nothing was sent")
        if destination_of(live) != destination:
            raise BrowserElementNotFound("the approved control now sends somewhere else; nothing was sent")
        if form_state_digest(form) != approved_hash:
            raise BrowserElementNotFound("the form's values changed while the click waited; nothing was sent")

    check.wants_form = True  # type: ignore[attr-defined]
    return check


class BrowserSubmitEffectProvider:
    """Performs one approved submit action in one bound browser session."""

    supports_idempotency = False
    name = PROVIDER_NAME

    def __init__(self, registry_getter: Callable[[], Any] | None = None, *, ego_delegate: Any | None = None) -> None:
        self._registry = registry_getter or _default_registry
        self._ego_delegate = ego_delegate

    def _select_ego(self, effect: EffectRecord, *, readback: bool = False):
        """Only a server registry can select Ego; a payload flag is not authority."""
        delegate = self._ego_delegate
        binding = delegate.select_effect(effect, readback=readback) if delegate is not None else None
        if binding is None and effect.payload.get("backend") == "ego":
            raise ValueError("ego_effect_binding_unavailable")
        return delegate, binding

    async def simulate(self, effect: EffectRecord) -> tuple[SimulationStatus, dict[str, Any]]:
        try:
            delegate, ego_binding = self._select_ego(effect)
        except Exception:
            return SimulationStatus.INCOMPLETE, {"reason": "ego_effect_binding_unavailable"}
        if ego_binding is not None:
            return await delegate.simulate(effect, binding=ego_binding)
        payload = effect.payload
        operation = str(payload.get("operation", "click") or "click")
        if operation not in {"click", "press_enter"}:
            return SimulationStatus.INCOMPLETE, {"unsupported_operation": operation}
        missing = [field for field in REQUIRED if not payload.get(field)]
        registry = self._registry()
        binding = registry.binding_for_key(str(payload.get("binding_key", "") or ""))
        if missing or binding is None:
            return SimulationStatus.INCOMPLETE, {"missing": missing, "session_known": binding is not None}
        state = registry.state_of(binding)
        return SimulationStatus.PREVIEW_ONLY, {
            "session": binding.describe(), "session_state": state.get("state", ""),
            "element": dict(payload.get("element", {}) or {}), "page": str(payload.get("page", "") or ""),
            "observation_hash": str(payload.get("observation_hash", ""))[:16],
            "network_call_performed": False,
            "checked": ["required fields present", "session is known to the registry"],
            "not_checked": ["that the page still shows this element", "what the site will do with the click"],
        }

    async def apply(self, effect: EffectRecord, *, idempotency_key: str) -> ProviderResult:
        try:
            delegate, ego_binding = self._select_ego(effect)
        except Exception:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail="ego_effect_binding_unavailable")
        if ego_binding is not None:
            return await delegate.apply(effect, binding=ego_binding, idempotency_key=idempotency_key)
        from ampuly.computer_use import browser_bridge
        from ampuly.computer_use.gateway import GatewayRefused
        from ampuly.layer4_tools.browser import BrowserElementNotFound
        from ampuly.layer4_tools.browser_sessions import BrowserSessionRefused

        if effect.kind != BROWSER_SUBMIT:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"unsupported browser effect kind: {effect.kind}")
        payload = effect.payload
        operation = str(payload.get("operation", "click") or "click")
        if operation not in {"click", "press_enter"}:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"unsupported browser submit operation: {operation}")
        registry = self._registry()
        binding = registry.binding_for_key(str(payload.get("binding_key", "") or ""))
        if binding is None:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail="browser session is gone; nothing was clicked")
        try:
            runtime = await registry.runtime_for(binding)
        except BrowserSessionRefused as exc:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"browser session refused: {exc.reason}")
        # C09: the approval bound to a page state; look again before acting on it.
        try:
            before = await runtime.snapshot(max_chars=0)
        except Exception as exc:  # noqa: BLE001 - no page to look at means nothing was sent
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"page could not be observed: {type(exc).__name__}")
        digest_now = str(before.get("snapshot_digest", "") or getattr(runtime, "last_snapshot_digest", ""))
        approved = str(payload.get("observation_hash", "") or "")
        if digest_now != approved:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                  detail="page changed since the approval; re-observe and approve again",
                                  receipt={"approved_observation": approved[:16], "current_observation": digest_now[:16]})
        source_url = str(payload.get("source_url", "") or "")
        source_hash = str(payload.get("source_url_hash", "") or "")
        from ampuly.computer_use.local_browser_consent import source_binding
        source_changed = (source_binding(str(before.get("url", "") or "")) !=
                          {"source_url": source_url, "source_url_hash": source_hash}) if source_hash else (
                              bool(source_url) and str(before.get("url", "") or "") != source_url)
        if source_changed:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                  detail="the approved page URL changed; nothing was sent")
        from ampuly.computer_use.local_browser_consent import REGISTRY as local_consents
        if not local_consents.effect_allowed(effect.effect_id):
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                  detail="local run consent expired or was revoked; nothing was sent")
        preexisting = set(str(key) for key in list(payload.get("expect_held_before", []) or []))
        preexisting.update(await page_expectations_held(payload.get("expect"), before, runtime))
        if (local_consents.effect_is_local(effect.effect_id)
                and not set(clean_expectation(payload.get("expect"))) - preexisting):
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                  detail="local_consent_expectation_required: state a result that does not already hold before clicking; no input was sent")
        selector = str(payload.get("selector", "") or "")
        frame_url = str(payload.get("frame_url", "") or "")
        if frame_url:
            # Approved inside an embedded page: click that element there, never a same-named one in the main page.
            ref = str(dict(payload.get("element") or {}).get("ref", "") or "")
            frames = getattr(runtime, "_ref_frames", {}) or {}
            resolver = getattr(runtime, "resolve_ref", None)
            try:
                same = bool(ref) and frames.get(ref) == frame_url and callable(resolver) and resolver(f"ref:{ref}") == selector
            except Exception:  # noqa: BLE001 - a ref the fresh snapshot does not know is not the approved target
                same = False
            if not same:
                return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                      detail="the approved target inside an embedded page is no longer the same; re-observe and approve again")
            selector = f"ref:{ref}"
        counter = getattr(runtime, "target_count", None)
        if callable(counter) and await counter(selector) != 1:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail="the approved target no longer names exactly one element; re-observe and approve again")
        reader = getattr(runtime, "form_state", None)
        approved_hash = str(payload.get("form_hash", "") or "")
        if callable(reader):
            from ampuly.computer_use.browser_bridge import form_state_digest
            from ampuly.layer4_tools.browser import form_state_of

            try:
                state = form_state_of(await reader(selector))
                now_hash = "overflow" if state["overflow"] else form_state_digest(state)
            except Exception:  # noqa: BLE001 - values that cannot be read are not the values approved
                now_hash = "unreadable"
            if now_hash != approved_hash:
                return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail="the form's values changed since the approval; re-observe and approve again")
        describe = getattr(runtime, "describe_target", None)
        try:
            approved_live = await describe(selector) if callable(describe) else None
        except BrowserElementNotFound as exc:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"the approved target is not on the page: {exc}"[:300])
        if operation == "press_enter" and (
            not dict(approved_live or {}).get("editable")
            or dict(approved_live or {}).get("tag") in {"button", "a", "select"}
        ):
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                  detail="the approved Enter target is no longer an editable field; re-observe and approve again")
        if "destination" in payload:
            # C09: the owner approved a click that sends to this address; a form action or href moved since is a
            # different act, whatever the button still says.
            from ampuly.computer_use.browser_bridge import destination_of

            if destination_of(approved_live or {}) != str(payload.get("destination", "") or ""):
                return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT,
                                      detail="the approved control now sends somewhere else; re-observe and approve again")
        target = {"ref": selector[4:]} if selector.startswith("ref:") else {"selector": selector[:256]}
        try:
            tool = "browser_submit_enter" if operation == "press_enter" else "browser_click"
            admission = await browser_bridge.admit(registry, binding, runtime, tool=tool, target=target,
                                                   kind="submit", effect_claim=effect.effect_id,
                                                   verification_sources=planned_sources(payload.get("expect")))
        except GatewayRefused as exc:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"gateway refused: {exc.reason}")
        from ampuly.layer4_tools.browser import _AGENT_DISPATCH_GUARD, _AGENT_TARGET_CHECK, BrowserControlLost, _compose_agent_dispatch_guard

        report = getattr(runtime, "request_report", None)
        before_transport = dict(report() or {}) if callable(report) else {"failed": 0, "pending_writes": 0}

        # G04: the click waits for its target; a person taking the page meanwhile stops it before it is sent.
        page_guard = browser_bridge.dispatch_guard(registry, binding, admission)
        token = _AGENT_DISPATCH_GUARD.set(_compose_agent_dispatch_guard(
            lambda: page_guard() and local_consents.effect_allowed(effect.effect_id)))
        # C09: and at the moment of sending, the target and the form values must still be the ones approved.
        check_token = _AGENT_TARGET_CHECK.set(_still_approved(approved_live, approved_hash) if approved_live is not None else None)
        try:
            if operation == "press_enter":
                await runtime.press_enter(selector=selector)
            else:
                await runtime.click(selector=selector)
        except BrowserControlLost as exc:
            browser_bridge.settle(binding, admission, ok=False, before_effect=True)
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"control moved to a person before the click; nothing was sent: {exc}"[:300])
        except BrowserElementNotFound as exc:
            # The target is not on the page any more: nothing was sent.
            browser_bridge.settle(binding, admission, ok=False, before_effect=True)
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=f"target no longer on the page: {exc}"[:300])
        except BaseException:
            # The click was sent and we cannot say what the page did: the Effect
            # Gateway records outcome_unknown from the raised exception.
            browser_bridge.settle(binding, admission, ok=False)
            raise
        finally:
            _AGENT_TARGET_CHECK.reset(check_token)
            _AGENT_DISPATCH_GUARD.reset(token)
        after_transport = await _wait_for_write_completion(runtime, before_transport)
        try:
            after = await runtime.snapshot(max_chars=4000)
        except Exception:  # noqa: BLE001 - clicked, then lost sight of the page
            browser_bridge.settle(binding, admission, ok=False)
            return ProviderResult(ProviderOutcome.UNKNOWN, detail="clicked; the page after the click could not be observed")
        failed_after_send = max(0, int(after_transport.get("failed", 0) or 0) - int(before_transport.get("failed", 0) or 0))
        pending_writes = int(after_transport.get("pending_writes", 0) or 0)
        if failed_after_send or pending_writes or str(after.get("url", "") or "").startswith("chrome-error:"):
            transport = {"request_failed_after_send": failed_after_send, "pending_writes": pending_writes}
            browser_bridge.settle(binding, admission, ok=False, evidence=transport)
            return ProviderResult(ProviderOutcome.UNKNOWN, receipt={
                "observation_before": approved[:16], "observation_after": str(after.get("snapshot_digest", ""))[:16],
                "url_after": _host_path(str(after.get("url", ""))), "transport": transport,
            }, detail="clicked; request completion is unknown -- look back, do not click again")
        api_result = await read_api_expectation(payload.get("expect"), registry, binding, runtime)
        postcondition = await check_postconditions_live(payload.get("expect"), after, runtime, api_result=api_result)
        if preexisting:
            postcondition = check_postconditions(payload.get("expect"), after, runtime, api_result=api_result,
                                                 not_discriminating=preexisting)
        receipt = {
            "observation_before": approved[:16], "observation_after": str(after.get("snapshot_digest", ""))[:16],
            "url_after": _host_path(str(after.get("url", ""))), "title_after": str(after.get("title", ""))[:120],
            "postcondition": postcondition,
        }
        unanswered = set(postcondition.get("unanswered", []) or [])
        if postcondition["checked"] and set(postcondition["failed"]) - unanswered:
            # The click happened and the page did not do what was expected: not a
            # success, not unknown -- failed after the effect (C11).
            browser_bridge.settle(binding, admission, ok=False, evidence={"postcondition": postcondition}, after_effect=True)
            return ProviderResult(ProviderOutcome.APPLIED, receipt=receipt, detail="clicked, but the expected result did not appear")
        evidence = {"observation_after": str(after.get("snapshot_digest", ""))[:16], "url_after": _host_path(str(after.get("url", ""))),
                    "postcondition": postcondition}
        # Held checks are the verification, named by the sources they read; nothing checked is a person's call (§5.3),
        # recorded awaiting_human while the Effect journal says verification is unsupported.
        browser_bridge.settle(binding, admission, ok=True, evidence=evidence, after_hash=str(after.get("snapshot_digest", "") or ""),
                              before_hash=digest_now, verification=verification_of(postcondition) if postcondition["checked"] else None)
        if unanswered:
            # The click happened and the source that would say what it did could not answer: unknown, not a pass (C11).
            return ProviderResult(ProviderOutcome.APPLIED, receipt=receipt,
                                  detail=f"clicked; the expected result could not be read ({', '.join(sorted(unanswered))})")
        return ProviderResult(ProviderOutcome.APPLIED, receipt=receipt)

    async def verify(self, effect: EffectRecord) -> tuple[VerificationStatus, dict[str, Any]]:
        """Look at the page again and hold the caller's expectations against it. No expectation: unsupported, said so."""
        try:
            delegate, ego_binding = self._select_ego(effect, readback=True)
        except Exception:
            return VerificationStatus.INCOMPLETE, {"reason": "ego_effect_binding_unavailable"}
        if ego_binding is not None:
            return await delegate.verify(effect, binding=ego_binding)
        payload = effect.payload
        excluded = set(str(key) for key in list(payload.get("expect_held_before", []) or []))
        expect = {k: v for k, v in dict(payload.get("expect", {}) or {}).items()
                  if k in EXPECT_KEYS and k not in excluded and str(v or "").strip()}
        registry = self._registry()
        binding = registry.binding_for_key(str(payload.get("binding_key", "") or ""))
        if binding is None:
            return VerificationStatus.INCOMPLETE, {"reason": "browser session is gone"}
        try:
            runtime = await registry.runtime_for(binding)
            now = await runtime.snapshot(max_chars=4000)
        except Exception as exc:  # noqa: BLE001 - verification never retries the effect
            return VerificationStatus.INCOMPLETE, {"reason": f"page could not be observed: {type(exc).__name__}"}
        # C10: whether the page is still the one the click was approved on is a
        # hint for the person deciding, never evidence that the submit happened.
        approved = str(payload.get("observation_hash", "") or "")
        hint = {"page_changed_since_approval": str(now.get("snapshot_digest", "") or "") != approved,
                "url_now": _host_path(str(now.get("url", "")))}
        if not expect:
            return VerificationStatus.UNSUPPORTED, {"detail": "no post-condition was stated for this submit; a person decides from the page", **hint}
        api_result = await read_api_expectation(payload.get("expect"), registry, binding, runtime)
        result = await check_postconditions_live(dict(payload.get("expect", {}) or {}), now, runtime, api_result=api_result)
        return (VerificationStatus.COMPLETE if result["holds"] else VerificationStatus.INCOMPLETE), {**result, **hint}


def _host_path(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.hostname or ''}{parts.path or ''}"


EXPECT_KEYS = ("url_contains", "text_contains", "element", "business_event", "api")
# The independent source each expectation reads (§5.4).
_SOURCE_OF = {"url_contains": "url", "text_contains": "element", "element": "element", "api": "api", "business_event": "db_event"}


def planned_sources(expect: dict[str, Any] | None) -> tuple[str, ...]:
    """The sources a submit's stated expectation will read: its verification plan. Empty when nothing is checkable."""
    return tuple(dict.fromkeys(_SOURCE_OF[key] for key in clean_expectation(expect) if key in _SOURCE_OF))


async def page_expectations_held(expect: dict[str, Any] | None, snapshot: dict[str, Any], runtime: Any = None) -> list[str]:
    """Page facts already true before a click are not evidence that it worked."""
    page_expect = {key: value for key, value in clean_expectation(expect).items()
                   if key in {"url_contains", "text_contains", "element"}}
    if not page_expect:
        return []
    result = await check_postconditions_live(page_expect, snapshot, runtime)
    return [key for key in result.get("checked", []) if key not in result.get("failed", [])]


def verification_of(postcondition: dict[str, Any]) -> dict[str, Any]:
    """Checks that ran, as verification evidence: the sources they read, what was checked, what failed, whether it held."""
    checked = [str(name) for name in postcondition.get("checked", []) or []]
    out: dict[str, Any] = {"sources": list(dict.fromkeys(_SOURCE_OF[name] for name in checked if name in _SOURCE_OF)),
                           "checked": checked, "failed": [str(name) for name in postcondition.get("failed", []) or []],
                           "holds": postcondition.get("holds") is True}
    if postcondition.get("unanswered"):
        out["unanswered"] = [str(name) for name in postcondition["unanswered"]]
    return out
API_KEYS = ("url", "json_path", "equals")

# C10: a source the page cannot fake -- an order's state in the business store, an API the product
# owns -- registered by the layer that owns it; the provider only asks. {"source": name, ...args}.
_VERIFICATION_SOURCES: dict[str, Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]]] = {}


def register_verification_source(name: str, checker: Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]]) -> None:
    _VERIFICATION_SOURCES[str(name)] = checker


def unregister_verification_source(name: str) -> None:
    _VERIFICATION_SOURCES.pop(str(name), None)


def verification_sources() -> list[str]:
    return sorted(_VERIFICATION_SOURCES)


def clean_expectation(expect: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only what a submit may expect: short strings for the page checks, a small dict for a business event."""
    out: dict[str, Any] = {}
    for key, value in dict(expect or {}).items():
        if key == "business_event":
            if isinstance(value, dict) and str(value.get("source", "") or "").strip():
                out[key] = {str(k)[:64]: str(v)[:200] for k, v in value.items()}
        elif key == "api":
            # X06: an API back-check names a GET (url), a path, and how to hold the value there. url and
            # json_path are always required; equals is required to EQUAL (default) or CONTAIN, but not to
            # assert the path is merely PRESENT. Anything else is dropped, not guessed.
            api_val = value if isinstance(value, dict) else {}
            url = str(api_val.get("url", "") or "").strip()
            path = str(api_val.get("json_path", "") or "").strip()
            match = str(api_val.get("match", "") or "").strip().lower()
            equals = api_val.get("equals")
            if url and path and match == "present":
                out[key] = {"url": url[:500], "json_path": path[:120], "equals": "", "match": "present"}
            elif url and path and isinstance(equals, str) and equals.strip():
                api = {"url": url[:500], "json_path": path[:120], "equals": equals[:200]}
                if match == "contains":
                    api["match"] = "contains"
                out[key] = api
        elif key in EXPECT_KEYS and str(value or "").strip():
            out[key] = str(value)[:200]
    return out


def json_path_value(data: Any, path: str) -> tuple[bool, Any]:
    """The value at a dotted path (`data.items.0.status`), and whether it was there."""
    current = data
    for part in [p for p in str(path or "").split(".") if p != ""]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _as_expected_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


async def read_api_expectation(expect: dict[str, Any] | None, registry: Any, binding: Any, runtime: Any) -> dict[str, Any] | None:
    """X06: the API back-check -- a channel the page's DOM cannot fake.

    One GET with the session's own login, on a host the order's domain gate admits (the same gate as
    browser_navigate), redirects not followed; the JSON value at the path must equal what was stated.
    The result says whether it held and why not; the value itself is never kept. None when no api
    expectation was stated.
    """
    from ampuly.layer4_tools.browser_sessions import BrowserSessionRefused

    api = clean_expectation(expect).get("api")
    if not api:
        return None
    url = api["url"]
    try:
        # The host only (P2 护栏一): an API back-check is a request the flow named, not a page. Judging it as a
        # page returned holds=False AFTER the click had already gone out, so an executed submit could not be
        # verified -- the worst possible moment to refuse.
        registry.check_host(binding, url)
    except BrowserSessionRefused as exc:
        return {"holds": False, "reason": exc.reason}
    # X06 (adversarial review): the read happens on apply and again on verify. verify does not go through
    # the Gateway's admit, so the runtime's request-level egress policy is refreshed here rather than
    # trusted to be whatever the last tool call left -- check_navigation and _egress_allows then both gate it.
    try:
        from ampuly.computer_use import browser_bridge as _bridge

        lease = _bridge.sync_lease(registry, binding)
        setter = getattr(runtime, "set_egress_hosts", None)
        if callable(setter):
            setter(getattr(lease, "allowed_hosts", ()) or ())
    except Exception:  # noqa: BLE001 - if the lease cannot be read, check_navigation above still gates the url
        pass
    reader = getattr(runtime, "api_read", None)
    if not callable(reader):
        return {"holds": False, "reason": "api_read_unavailable"}
    try:
        response = dict(await reader(url) or {})
    except PermissionError as exc:
        return {"holds": False, "reason": str(exc) or "egress_not_allowed"}
    except Exception as exc:  # noqa: BLE001 - a source that cannot answer is not a pass
        return {"holds": False, "reason": type(exc).__name__}
    status = int(response.get("status", 0) or 0)
    if not 200 <= status < 300:
        return {"holds": False, "reason": "http_status", "status": status}
    found, value = json_path_value(response.get("json"), api["json_path"])
    if not found:
        return {"holds": False, "reason": "path_not_found", "status": status}
    if api.get("match") == "present":
        return {"holds": True, "status": status}
    text = _as_expected_text(value)
    holds = api["equals"] in text if api.get("match") == "contains" else text == api["equals"]
    if not holds:
        return {"holds": False, "reason": "value_differs", "status": status}
    return {"holds": True, "status": status}


_ELEMENT_NOT_QUERIED = object()


async def check_postconditions_live(expect: dict[str, Any] | None, snapshot: dict[str, Any], runtime: Any = None,
                                    api_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check a page expectation with a live selector query when the runtime provides one.

    Snapshots are bounded observations and may omit a perfectly real element.
    Conversely, an old snapshot must not verify an element that disappeared.
    ``target_count`` is therefore the authority for selector presence.  A
    query error is unavailable evidence, never a pass and never proof of
    absence.
    """
    cleaned = clean_expectation(expect)
    live_element: Any = _ELEMENT_NOT_QUERIED
    element_unanswered = False
    counter = getattr(runtime, "target_count", None)
    if "element" in cleaned and callable(counter):
        try:
            live_element = int(await counter(cleaned["element"])) > 0
        except Exception:  # noqa: BLE001 - an unreadable selector is unknown, not absent
            live_element = False
            element_unanswered = True
    return check_postconditions(cleaned, snapshot, runtime, api_result=api_result,
                                live_element=live_element, element_unanswered=element_unanswered)


def check_postconditions(expect: dict[str, Any] | None, snapshot: dict[str, Any], runtime: Any = None,
                         api_result: dict[str, Any] | None = None, *, live_element: Any = _ELEMENT_NOT_QUERIED,
                         element_unanswered: bool = False, not_discriminating: Any = ()) -> dict[str, Any]:
    """What the caller said should be true after the submit, against the page as it is now (C08, C11)
    and, for a business event, against the source that owns the fact (C10).

    Returns {"checked": [...], "failed": [...], "holds": bool} plus "unanswered" for checks whose source could not
    answer (a back-check that never reached the site, a page text cut before the phrase could be looked for): those are
    a miss nobody established, not a page that contradicted the expectation. No expectation means nothing was
    established -- said as such, not as a pass.
    """
    excluded = {str(key) for key in list(not_discriminating or [])}
    expect = {key: value for key, value in clean_expectation(expect).items() if key not in excluded}
    checked: list[str] = []
    failed: list[str] = []
    unanswered: list[str] = []
    detail: dict[str, Any] = {}
    if "url_contains" in expect:
        checked.append("url_contains")
        if expect["url_contains"] not in str(snapshot.get("url", "") or ""):
            failed.append("url_contains")
    if "text_contains" in expect:
        checked.append("text_contains")
        if expect["text_contains"] not in str(snapshot.get("text", "") or ""):
            failed.append("text_contains")
            if snapshot.get("text_truncated"):
                unanswered.append("text_contains")  # the page's text was cut before the phrase could be looked for
    if "element" in expect:
        checked.append("element")
        if live_element is _ELEMENT_NOT_QUERIED:
            lookup = getattr(runtime, "element_for", None)
            present = bool(lookup(expect["element"])) if callable(lookup) else any(
                e.get("selector") == expect["element"] for e in snapshot.get("interactive_elements", []) or [])
        else:
            present = live_element is True
        if not present:
            failed.append("element")
            if element_unanswered:
                unanswered.append("element")
                detail["element"] = {"reason": "source_unavailable"}
    if "business_event" in expect:
        checked.append("business_event")
        args = dict(expect["business_event"])
        checker = _VERIFICATION_SOURCES.get(str(args.get("source", "")))
        if checker is None:
            failed.append("business_event")
            unanswered.append("business_event")
            detail["business_event"] = {"reason": "source_unavailable", "source": str(args.get("source", ""))}
        else:
            answered = True
            try:
                holds, found = checker(args)
            except Exception as exc:  # noqa: BLE001 - a source that cannot answer is not a pass
                holds, found, answered = False, {"reason": f"{type(exc).__name__}: {exc}"[:160]}, False
            detail["business_event"] = {"source": str(args.get("source", "")), **dict(found or {})}
            if not holds:
                failed.append("business_event")
                if not answered:
                    unanswered.append("business_event")
    if "api" in expect:
        # X06: read through the session by the async caller; an expectation nobody read is not a pass.
        checked.append("api")
        api = dict(api_result or {"holds": False, "reason": "api_not_read"})
        detail["api"] = {k: v for k, v in api.items() if k in {"holds", "reason", "status"}}
        if not api.get("holds"):
            failed.append("api")
            # Only the site's own answer is a miss; anything else (never read, refused, an HTTP error) is unanswered.
            if str(api.get("reason", "")) not in {"value_differs", "path_not_found"}:
                unanswered.append("api")
    result = {"checked": checked, "failed": failed, "holds": bool(checked) and not failed}
    if unanswered:
        result["unanswered"] = unanswered
    if detail:
        result["detail"] = detail
    if excluded:
        result["not_discriminating"] = sorted(excluded)
    return result


def register_browser_submit(effect_gateway: Any, registry_getter: Callable[[], Any] | None = None, *, ego_delegate: Any | None = None) -> BrowserSubmitEffectProvider:
    provider = BrowserSubmitEffectProvider(registry_getter, ego_delegate=ego_delegate)
    effect_gateway.register(provider)
    return provider
