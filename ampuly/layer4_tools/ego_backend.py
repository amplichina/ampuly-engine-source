"""Ego BrowserBackend: optional Windows-oriented backend (Playwright stays default).

Invokes a *local* `ego-browser` CLI with per-action scripts sent over stdin;
only content-free evidence is retained in its Ampuly Task Space. Does not migrate the user's main Chrome
profile; callers pass AMPULY_EGO_PROFILE_ID for a dedicated Ego profile when
creating spaces. The ego-browser Skill must never be called from product code
outside this leaf — all actions stay on BrowserBackend.act / run_batch so
permission, Effect, and receipt layers remain in Ampuly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from ampuly.layer4_tools.browser_backend import ALLOWED_KINDS, Action
from ampuly.layer4_tools.browser_spaces import BrowserSpaces, Space
from ampuly.layer4_tools.ego_config import (
    EgoLaunchConfig,
    load_ego_launch_config,
    resolve_ego_binary,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
EGO_RESULT_SCHEMA = "ampuly.ego.result.v1"
EGO_SPACE_SCHEMA = "ampuly.ego.space.v1"
_EFFECT_MARKER = "const ampulyEffect = "


def effect_identity_in_script(source: str) -> dict[str, Any] | None:
    """Identity object embedded in a generated Ego script, if this script has one."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(_EFFECT_MARKER):
            raw = stripped[len(_EFFECT_MARKER):].rstrip(";")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


class EgoUnavailable(RuntimeError):
    """Binary missing, OS unsupported for live run, or config incomplete."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"ego backend unavailable: {reason}")


class EgoActionFailed(RuntimeError):
    def __init__(self, reason: str, *, detail: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.detail = dict(detail or {})
        super().__init__(f"ego action failed: {reason}")


Runner = Callable[[list[str], bytes, float], subprocess.CompletedProcess[bytes]]


def _default_runner(cmd: list[str], stdin: bytes, timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _action_locator(value: str) -> str:
    """Snapshot loc values are data; use Ego's documented action spelling.

    Newer upstream parsers also accept bare css:, but installed runtimes need
    the canonical loc=css: form. Keep stored refs and host contracts unchanged.
    """
    return "loc=" + value if value.startswith("css:") else value


def _effect_preamble(*, action_id: str, attempt_id: str, account: str | None) -> str:
    payload = {
        "schema": EGO_RESULT_SCHEMA,
        "action_id": action_id,
        "attempt_id": attempt_id,
        "account": account or "",
    }
    return _EFFECT_MARKER + json.dumps(payload, ensure_ascii=False) + ";\n"


def _success_envelope_log() -> str:
    return (
        "console.log(JSON.stringify({...ampulyEffect, ok: true, phase: \"completed\", snapshot: snap }));\n"
    )


def _space_setup_script(*, space_name: str, ego_profile_id: str | None, space_id: int | None,
                        closing: bool = False) -> str:
    """Only create once; names and default profiles are never recovery locators."""
    if (type(ego_profile_id) is not str or not ego_profile_id.strip()
            or ego_profile_id != ego_profile_id.strip() or ego_profile_id == "default"
            or len(ego_profile_id) > 256 or "\x00" in ego_profile_id):
        raise EgoActionFailed("ego_profile_required")
    if space_id is not None and (type(space_id) is not int or not 0 <= space_id <= 9007199254740991):
        raise EgoActionFailed("ego_space_id_invalid")
    prelude = (
        f"const ampulyProfile = {_js_string(ego_profile_id)};\n"
        f"const ampulySpaceName = {_js_string(space_name)};\n"
        f"const ampulyExpectedSpaceId = {json.dumps(space_id)};\n"
        "function ampulyRefuse(code) { const error = new Error(code); error.ampulyCode = code; throw error; }\n"
        "async function ampulySpaces() {\n"
        "  const result = await ego.listTaskSpaces();\n"
        "  if (!result || result.error || !Array.isArray(result.taskSpaces) ||\n"
        "      result.taskSpaces.some(s => !s || !Number.isSafeInteger(s.id) || s.id < 0)) ampulyRefuse('ego_space_list_invalid');\n"
        "  return result.taskSpaces;\n"
        "}\n"
        "function ampulyBound(rows, id) {\n"
        "  const matches = rows.filter(s => s.id === id);\n"
        "  if (matches.length !== 1 || matches[0].profileId !== ampulyProfile ||\n"
        "      matches[0].name !== ampulySpaceName || matches[0].ownership !== 'agent') ampulyRefuse('ego_space_identity_mismatch');\n"
        "  return matches[0];\n"
        "}\n"
        "const knownProfiles = await ego.listProfiles();\n"
        "if (!knownProfiles || knownProfiles.error || !Array.isArray(knownProfiles.profiles) ||\n"
        "    knownProfiles.profiles.filter(p => p && p.id === ampulyProfile).length !== 1) ampulyRefuse('ego_profile_unavailable');\n"
        "const beforeSpaces = await ampulySpaces();\n"
    )
    if space_id is None:
        return prelude + (
            "if (beforeSpaces.some(s => s.name === ampulySpaceName)) ampulyRefuse('ego_space_name_collision');\n"
            "ampulyPhase = 'space_possible';\n"
            "const task = await taskSpace(ampulySpaceName, { profileId: ampulyProfile });\n"
            "if (!Number.isSafeInteger(task.spaceId) || task.spaceId < 0) ampulyRefuse('ego_space_id_invalid');\n"
            "ampulySpaceId = task.spaceId;\n"
            "ampulyBound(await ampulySpaces(), ampulySpaceId);\n"
        )
    if closing:
        return prelude + "ampulyBound(beforeSpaces, ampulyExpectedSpaceId);\n"
    return prelude + (
        "ampulyBound(beforeSpaces, ampulyExpectedSpaceId);\n"
        "const task = await taskSpace(ampulyExpectedSpaceId);\n"
        "if (task.spaceId !== ampulyExpectedSpaceId) ampulyRefuse('ego_space_identity_mismatch');\n"
        "ampulySpaceId = task.spaceId;\n"
        "ampulyBound(await ampulySpaces(), ampulySpaceId);\n"
    )


def _bound_script(*, space_name: str, page_label: str, ego_profile_id: str | None,
                  space_id: int | None, action_id: str, attempt_id: str, body: str) -> str:
    setup = _space_setup_script(space_name=space_name, ego_profile_id=ego_profile_id, space_id=space_id)
    return (
        _effect_preamble(action_id=action_id, attempt_id=attempt_id, account=ego_profile_id)
        + "let ampulyPhase = 'before_effect';\nlet ampulySpaceId = null;\ntry {\n"
        + setup
        + f"console.log(JSON.stringify({{...ampulyEffect, schema: {_js_string(EGO_SPACE_SCHEMA)}, state: 'active', space_id: ampulySpaceId, profile_id: ampulyProfile, space_name: ampulySpaceName}}));\n"
        + f"const page = task.page({_js_string(page_label)});\n"
        + "ampulyPhase = 'effect_possible';\n" + body
        + "} catch (error) {\n"
        + "console.log(JSON.stringify({...ampulyEffect, ok: false, phase: ampulyPhase, error: error.ampulyCode || 'ego_operation_failed', snapshot: null}));\n}\n"
    )


def _build_close_script(*, space_name: str, ego_profile_id: str, space_id: int,
                        action_id: str, attempt_id: str) -> str:
    # Reuse identity checks, but do not instantiate a Page or create a space.
    setup = _space_setup_script(space_name=space_name, ego_profile_id=ego_profile_id, space_id=space_id, closing=True)
    return (
        _effect_preamble(action_id=action_id, attempt_id=attempt_id, account=ego_profile_id)
        + "let ampulyPhase = 'before_effect';\ntry {\n" + setup
        + "if (ego.useTaskSpace(ampulyExpectedSpaceId) !== ampulyExpectedSpaceId) ampulyRefuse('ego_space_identity_mismatch');\nampulyPhase = 'effect_possible';\n"
        + "const closeResult = await ego.closeTaskSpace();\n"
        + "if (typeof closeResult !== 'string' || !closeResult) ampulyRefuse('ego_close_unconfirmed');\n"
        + "ampulyPhase = 'after_effect';\n"
        + "if ((await ampulySpaces()).some(s => s.id === ampulyExpectedSpaceId)) ampulyRefuse('ego_space_still_present');\n"
        + f"console.log(JSON.stringify({{...ampulyEffect, schema: {_js_string(EGO_SPACE_SCHEMA)}, state: 'closed', space_id: ampulyExpectedSpaceId, profile_id: ampulyProfile, space_name: ampulySpaceName}}));\n"
        + "console.log(JSON.stringify({...ampulyEffect, ok: true, phase: 'completed', operation: 'close', closed: true}));\n"
        + "} catch (error) {\nconsole.log(JSON.stringify({...ampulyEffect, ok: false, phase: ampulyPhase, error: error.ampulyCode || 'ego_close_failed'}));\n}\n"
    )


def _build_action_script(
    *,
    space_name: str,
    page_label: str,
    action: Action,
    ego_profile_id: str | None,
    action_id: str,
    attempt_id: str,
    capture_path: str | None = None,
    space_id: int | None = None,
) -> str:
    kind = action.kind
    target = _action_locator(action.target) if kind in {"click", "fill", "wait", "select"} else action.target
    if kind == "navigate":
        body = f"await page.goto({_js_string(action.target)});\n"
    elif kind == "click":
        # The click and the following snapshot share one process. A missing
        # envelope, or a failure after the click, cannot prove the click did
        # not happen. The call itself can send input before rejecting, so its
        # catch must never claim before_effect merely because await raised.
        guard = action.meta.get("merchant_guard") if isinstance(action.meta, dict) else None
        guard_script = ""
        if guard is not None:
            if (type(guard) is not dict or set(guard) != {"url", "merchant_selector", "merchant_url", "submit_selector", "submit_url", "fields"}
                    or any(type(guard[key]) is not str or not guard[key] for key in ("url", "merchant_selector", "merchant_url", "submit_selector", "submit_url"))
                    or type(guard["fields"]) is not dict or not guard["fields"]
                    or any(type(key) is not str or type(value) is not str for key, value in guard["fields"].items())
                    or target != _action_locator("css:" + guard["submit_selector"])):
                raise EgoActionFailed("merchant_guard_invalid")
            # Fixed read-only expression. The host supplies data, never executable code.
            guard_script = (
                "  const guardHolds = await page.evaluate((g) => {\n"
                "    if (location.href !== g.url) return false;\n"
                "    const shops = document.querySelectorAll(g.merchant_selector);\n"
                "    if (shops.length !== 1 || shops[0].href !== g.merchant_url) return false;\n"
                "    const buttons = document.querySelectorAll(g.submit_selector);\n"
                "    if (buttons.length !== 1 || buttons[0].disabled || buttons[0].type !== 'submit') return false;\n"
                "    const button = buttons[0], form = button.form;\n"
                "    if (!form || form.action !== g.submit_url || form.method.toLowerCase() !== 'post' || form.target) return false;\n"
                "    if (button.hasAttribute('formaction') || button.hasAttribute('formmethod') || button.hasAttribute('formtarget')) return false;\n"
                "    return Object.entries(g.fields).every(([selector, value]) => {\n"
                "      const nodes = document.querySelectorAll(selector);\n"
                "      return nodes.length === 1 && nodes[0].form === form && nodes[0].value === value && !nodes[0].disabled;\n"
                "    });\n"
                f"  }}, {json.dumps(guard, ensure_ascii=False)});\n"
                "  if (!guardHolds) throw new Error('merchant_form_changed');\n"
                "  phase = \"effect_possible\";\n"
            )
        body = (
            f"let phase = {_js_string('before_effect' if guard is not None else 'effect_possible')};\n"
            "try {\n"
            + guard_script
            + f"  await page.click({_js_string(target)});\n"
            "  phase = \"after_effect\";\n"
            "  const snap = await page.snapshot();\n"
            "  console.log(JSON.stringify({...ampulyEffect, ok: true, phase: \"completed\", snapshot: snap }));\n"
            "} catch (error) {\n"
            "  console.log(JSON.stringify({...ampulyEffect, ok: false, phase, snapshot: null }));\n"
            "}\n"
        )
    elif kind == "fill":
        body = f"await page.fill({_js_string(target)}, {_js_string(action.value)});\n"
    elif kind == "press":
        key = action.value or action.target
        body = f"await page.press({_js_string(key)});\n"
    elif kind == "wait":
        if action.target:
            body = f"await page.waitForSelector({_js_string(target)});\n"
        else:
            ms = int(action.meta.get("ms", 500)) if isinstance(action.meta, dict) else 500
            # Ego lite has no page.wait; use a timer Promise (still receipts as wait).
            body = f"await new Promise((resolve) => setTimeout(resolve, {ms}));\n"
    elif kind == "scroll":
        dy = int(action.meta.get("dy", 600)) if isinstance(action.meta, dict) else 600
        body = f"await page.mouse.wheel(0, {dy});\n"
    elif kind == "select":
        body = (
            f"await page.selectOption({_js_string(target)}, "
            f"{{ label: {_js_string(action.value)} }});\n"
        )
    elif kind == "capture":
        out = capture_path or action.target or "capture.png"
        body = f"await page.screenshot({{ path: {_js_string(out)} }});\n"
    else:
        raise EgoActionFailed(f"unsupported_kind:{kind}")
    if kind != "click":
        body += "const snap = await page.snapshot();\n" + _success_envelope_log()
    return _bound_script(space_name=space_name, page_label=page_label, ego_profile_id=ego_profile_id,
                         space_id=space_id, action_id=action_id, attempt_id=attempt_id, body=body)


def _build_snapshot_script(
    *,
    space_name: str,
    page_label: str,
    ego_profile_id: str | None,
    action_id: str,
    attempt_id: str,
    merchant: bool = False,
    space_id: int | None = None,
) -> str:
    read = (
        'const urlBefore = await page.url();\n'
        'const snap = await page.snapshot({scope: "full_page", includeStableLocator: true});\n'
        'const observedUrl = await page.url();\n'
        'if (urlBefore !== observedUrl) throw new Error("merchant_page_changed");\n'
        'console.log(JSON.stringify({...ampulyEffect, ok: true, phase: "completed", url: observedUrl, snapshot: snap}));\n'
    ) if merchant else "const snap = await page.snapshot();\n" + _success_envelope_log()
    return _bound_script(space_name=space_name, page_label=page_label, ego_profile_id=ego_profile_id,
                         space_id=space_id, action_id=action_id, attempt_id=attempt_id, body=read)


def _envelopes_in(stdout: str, stderr: str, *, schema: str = EGO_RESULT_SCHEMA) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for stream in (stdout, stderr):
        for line in stream.splitlines():
            text = line.strip()
            if not (text.startswith("{") and text.endswith("}")):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("schema") == schema:
                found.append(parsed)
    return found


def _accept_envelope(envelopes: list[dict[str, Any]], *, action_id: str, attempt_id: str,
                     closing: bool = False) -> dict[str, Any]:
    """One schema envelope for this action and attempt. Anything else is unknown."""
    matched = [
        item for item in envelopes
        if item.get("action_id") == action_id and item.get("attempt_id") == attempt_id
    ]
    if len(matched) != 1:
        return {
            "ok": False,
            "unknown": True,
            "error": "ego_envelope_missing" if not matched else "ego_envelope_ambiguous",
            "schema": EGO_RESULT_SCHEMA,
            "action_id": action_id,
            "attempt_id": attempt_id,
        }
    env = dict(matched[0])
    phase = str(env.get("phase") or "")
    if env.get("ok") is True:
        completed = (env.get("operation") == "close" and env.get("closed") is True) if closing else isinstance(env.get("snapshot"), (str, dict))
        if phase != "completed" or not completed:
            return {**env, "ok": False, "unknown": True, "error": "ego_envelope_invalid"}
        env["unknown"] = False
        return env
    env["ok"] = False
    env["unknown"] = phase != "before_effect"
    return env


class EgoBackend:
    """BrowserBackend backed by a local ego-browser process (opt-in)."""

    def __init__(
        self,
        *,
        spaces: BrowserSpaces,
        profile_id: str,
        task_id: str,
        binary: str,
        ego_profile_id: str | None = None,
        page_label: str = "p1",
        timeout_sec: float = 120.0,
        runner: Runner | None = None,
        require_windows: bool = True,
    ) -> None:
        if not _SAFE_ID.match(profile_id or ""):
            raise EgoUnavailable(f"invalid_profile_id:{profile_id!r}")
        if not _SAFE_ID.match(task_id or ""):
            raise EgoUnavailable(f"invalid_task_id:{task_id!r}")
        if require_windows and os.name != "nt":
            raise EgoUnavailable("windows_only")
        if not binary:
            raise EgoUnavailable("binary_missing")
        self.spaces = spaces
        self.profile_id = profile_id
        self.task_id = task_id
        self.binary = binary
        self.ego_profile_id = ego_profile_id
        self.page_label = page_label
        self.timeout_sec = timeout_sec
        self._runner = runner or _default_runner
        self._ampuly_profile = spaces.ensure_profile(
            profile_id,
            notes="Ampuly task evidence; actual Ego profile bound separately (no Chrome migrate)",
        )
        open_spaces = [
            s
            for s in spaces.list_spaces(profile_id=profile_id)
            if s.task_id == task_id and s.state == "open"
        ]
        self._space = open_spaces[0] if open_spaces else spaces.open_space(profile_id, task_id)
        self._round = 0
        self._closed = False
        self._ego_space_name = f"ampuly-{profile_id}-{task_id}"
        self._operation_lock = threading.RLock()
        self._ego_space_id: int | None = None
        self._lifecycle_state = "unknown" if open_spaces else "unopened"
        self._binding_path = Path(self._space.directory) / "ego-space-binding.json"
        self._load_binding()

    @property
    def lifecycle_state(self) -> str:
        return self._lifecycle_state

    def _load_binding(self) -> None:
        if self._binding_path.is_symlink():
            raise EgoUnavailable("ego_binding_link_refused")
        if not self._binding_path.exists():
            # An older/local-only open record does not prove that no native
            # space exists. Never recover it by name or create a replacement.
            return
        try:
            if self._binding_path.stat().st_size > 8192:
                raise ValueError("oversized binding")
            row = json.loads(self._binding_path.read_text(encoding="utf-8"))
            if (type(row) is not dict or row.get("schema") != EGO_SPACE_SCHEMA
                    or row.get("local_space_id") != self._space.space_id
                    or row.get("profile_id") != self.ego_profile_id
                    or row.get("space_name") != self._ego_space_name
                    or row.get("state") not in {"creating", "active", "closing", "unknown", "closed"}):
                raise ValueError("foreign binding")
            native_id = row.get("space_id")
            if native_id is not None and (type(native_id) is not int or not 0 <= native_id <= 9007199254740991):
                raise ValueError("invalid native id")
            if row["state"] in {"active", "closed"} and native_id is None:
                raise ValueError("missing native id")
            self._ego_space_id = native_id
            self._lifecycle_state = row["state"] if row["state"] in {"active", "closed"} else "unknown"
            # A native-close receipt can precede local release. Keep close()
            # callable so a new instance finishes only the local release.
            self._closed = False
        except (OSError, ValueError, TypeError) as exc:
            raise EgoUnavailable("ego_binding_invalid") from exc

    def _save_binding(self, state: str) -> None:
        if self._binding_path.is_symlink():
            raise EgoActionFailed("ego_binding_link_refused", detail={"unknown": True})
        row = {
            "schema": EGO_SPACE_SCHEMA, "local_space_id": self._space.space_id,
            "space_name": self._ego_space_name, "profile_id": self.ego_profile_id,
            "space_id": self._ego_space_id, "state": state,
        }
        temp = self._binding_path.with_name(f".ego-binding-{uuid.uuid4().hex}.json")
        try:
            with temp.open("x", encoding="utf-8") as stream:
                json.dump(row, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._binding_path)
            self._lifecycle_state = state
        except OSError as exc:
            self._lifecycle_state = "unknown"
            raise EgoActionFailed("ego_binding_write_failed", detail={"unknown": True}) from exc

    def _require_active_or_new(self) -> None:
        if self._closed:
            raise EgoActionFailed("backend_closed")
        if self._lifecycle_state not in {"unopened", "active"}:
            raise EgoActionFailed("ego_space_recovery_required", detail={"unknown": True})

    @property
    def space(self) -> Space:
        return self._space

    def _mint_attempt(self) -> tuple[str, str]:
        self._round += 1
        action_id = f"{self.task_id}:r{self._round}"
        return action_id, f"{action_id}:{uuid.uuid4().hex}"

    def _run_script(self, source: str, *, closing: bool = False, recovery_readonly: bool = False) -> dict[str, Any]:
        # Only the fixed, exact-space readback adapter may select this mode.
        # Never stamp an unknown resource active, even for one write window.
        if type(recovery_readonly) is not bool or recovery_readonly and (
                closing or self._lifecycle_state != "unknown"
                or type(self._ego_space_id) is not int
                or not 0 <= self._ego_space_id <= 9007199254740991):
            raise EgoActionFailed("ego_readonly_recovery_refused")
        if self._closed:
            raise EgoActionFailed("backend_closed")
        if self._round < 1:
            self._round = 1
        stem = f"round-{self._round:04d}-{uuid.uuid4().hex}"
        identity = effect_identity_in_script(source) or {}
        # Scripts can contain passwords typed with fill(); observations can
        # contain private page data. Persist metadata only, never either body.
        def record(accepted: dict[str, Any], returncode: int | None) -> None:
            allowed_errors = {
                "ego_envelope_missing", "ego_envelope_ambiguous", "ego_envelope_invalid",
                "ego_process_failed", "ego_process_outcome_unknown", "ego_space_binding_unverified",
                "ego_result_identity_mismatch", "ego_operation_failed", "ego_close_failed",
                "ego_space_list_invalid", "ego_space_identity_mismatch", "ego_profile_unavailable",
                "ego_space_name_collision", "ego_space_id_invalid", "ego_close_unconfirmed",
                "ego_space_still_present",
            }
            error = accepted.get("error")
            receipt = {
                "ok": accepted.get("ok") is True and accepted.get("unknown") is not True,
                "unknown": bool(accepted.get("unknown")), "returncode": returncode,
                "script_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "action_id": identity.get("action_id"), "attempt_id": identity.get("attempt_id"),
                "account": self.ego_profile_id, "space_id": self._ego_space_id,
                "state": self._lifecycle_state,
                "error": error if isinstance(error, str) and error in allowed_errors else ("ego_operation_failed" if error else None),
            }
            with (Path(self._space.directory) / f"receipt-{stem}.json").open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False)
        # Persist intent before any native creation/close. A lost process or
        # reply must not turn into a new space on the next instance.
        if closing:
            self._save_binding("closing")
        elif self._ego_space_id is None:
            self._save_binding("creating")
        try:
            completed = self._runner(
                [self.binary, "nodejs"], source.encode("utf-8"), self.timeout_sec,
            )
        except (subprocess.TimeoutExpired, OSError):
            self._save_binding("unknown")
            accepted = {"ok": False, "unknown": True, "error": "ego_process_outcome_unknown"}
            record(accepted, None)
            return accepted
        stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
        accepted = _accept_envelope(
            _envelopes_in(stdout, stderr),
            action_id=str(identity.get("action_id") or ""),
            attempt_id=str(identity.get("attempt_id") or ""),
            closing=closing,
        )
        if accepted.get("ok") is True and accepted.get("account") != self.ego_profile_id:
            accepted = {**accepted, "ok": False, "unknown": True, "error": "ego_result_identity_mismatch"}
        if completed.returncode != 0 and accepted.get("phase") != "before_effect":
            # The script bundles the action and its snapshot. A non-zero exit
            # does not prove the action never reached the page.
            accepted = {
                **accepted,
                "ok": False,
                "unknown": True,
                "error": accepted.get("error") or "ego_process_failed",
            }
        bindings = [row for row in _envelopes_in(stdout, stderr, schema=EGO_SPACE_SCHEMA)
                    if row.get("action_id") == identity.get("action_id")
                    and row.get("attempt_id") == identity.get("attempt_id")]
        binding = bindings[0] if len(bindings) == 1 else {}
        native_id = binding.get("space_id")
        verified = (
            type(native_id) is int and 0 <= native_id <= 9007199254740991
            and binding.get("state") == ("closed" if closing else "active")
            and binding.get("space_name") == self._ego_space_name
            and binding.get("profile_id") == self.ego_profile_id
            and binding.get("account") == self.ego_profile_id
            and (self._ego_space_id is None or native_id == self._ego_space_id)
        )
        # Closure needs both the checked disappearance and the successful
        # matching operation receipt; a non-zero process cannot release it.
        if verified and (not closing or accepted.get("ok") is True):
            self._ego_space_id = native_id
            self._save_binding("unknown" if recovery_readonly else ("closed" if closing else "active"))
        else:
            self._save_binding("unknown")
            accepted = {**accepted, "ok": False, "unknown": True,
                        "error": accepted.get("error") or "ego_space_binding_unverified"}
        record(accepted, completed.returncode)
        return accepted

    def snapshot(self, *, merchant: bool = False) -> dict[str, Any]:
        with self._operation_lock:
            self._require_active_or_new()
            action_id, attempt_id = self._mint_attempt()
            return self._run_script(
                _build_snapshot_script(
                    space_name=self._ego_space_name,
                    page_label=self.page_label,
                    ego_profile_id=self.ego_profile_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                    merchant=merchant,
                    space_id=self._ego_space_id,
                )
            )

    def act(self, action: Action) -> dict[str, Any]:
        with self._operation_lock:
            return self._act_locked(action)

    def _act_locked(self, action: Action) -> dict[str, Any]:
        if not isinstance(action, Action):
            raise EgoActionFailed("not_an_action")
        if action.kind not in ALLOWED_KINDS:
            raise EgoActionFailed(f"kind_not_allowed:{action.kind}")
        capture_path = None
        if action.kind == "capture":
            capture_path = str(
                Path(self._space.directory) / "captures" / f"{int(time.time() * 1000)}.png"
            )
            Path(capture_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._require_active_or_new()
            action_id, attempt_id = self._mint_attempt()
            result = self._run_script(
                _build_action_script(
                    space_name=self._ego_space_name,
                    page_label=self.page_label,
                    action=action,
                    ego_profile_id=self.ego_profile_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                    capture_path=capture_path,
                    space_id=self._ego_space_id,
                )
            )
        except EgoActionFailed as exc:
            return {"ok": False, "unknown": bool(exc.detail.get("unknown")), "error": exc.reason, "detail": exc.detail, "result": {}}
        out: dict[str, Any] = {
            "ok": result.get("ok") is True and result.get("unknown") is not True,
            "unknown": bool(result.get("unknown")),
            "result": result,
        }
        if capture_path:
            out["path"] = capture_path
            wanted = str(action.target or "").strip()
            if wanted and Path(wanted).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                try:
                    dest = Path(wanted)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(Path(capture_path).read_bytes())
                    out["path"] = str(dest)
                except OSError:
                    pass
        return out

    def capture(self, path: str) -> str:
        target = str(Path(path))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        result = self.act(Action("capture", target))
        if not result.get("ok"):
            raise EgoActionFailed(str(result.get("error") or "capture_failed"), detail=result)
        return target

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            if self._lifecycle_state != "closed":
                self._require_active_or_new()
            if self._lifecycle_state == "active":
                action_id, attempt_id = self._mint_attempt()
                result = self._run_script(_build_close_script(
                    space_name=self._ego_space_name, ego_profile_id=self.ego_profile_id,
                    space_id=self._ego_space_id, action_id=action_id, attempt_id=attempt_id,
                ), closing=True)
                if result.get("ok") is not True:
                    raise EgoActionFailed("ego_close_outcome_unknown", detail=result)
            # Unopened means this instance has never issued a native request.
            # Unknown/creating/closing records cannot reach this local release.
            try:
                self._space = self.spaces.close_space(self._space.space_id)
            except Exception as exc:
                if self._ego_space_id is not None:
                    self._save_binding("unknown")
                else:
                    self._lifecycle_state = "unknown"
                raise EgoActionFailed("ego_local_release_failed", detail={"unknown": True}) from exc
            self._closed = True
            self._lifecycle_state = "closed"


def open_ego_backend(
    *,
    profile_id: str,
    task_id: str,
    env: Mapping[str, str] | None = None,
    spaces: BrowserSpaces | None = None,
    runner: Runner | None = None,
    require_windows: bool = True,
) -> EgoBackend:
    """Factory used by browser_provider when AMPULY_BROWSER_BACKEND=ego."""
    cfg: EgoLaunchConfig = load_ego_launch_config(env)
    binary = cfg.binary or resolve_ego_binary(env)
    if not binary:
        raise EgoUnavailable("binary_missing")
    root = cfg.spaces_root
    if root is None:
        raise EgoUnavailable("spaces_root_unset")
    root.mkdir(parents=True, exist_ok=True)
    store = spaces or BrowserSpaces(root)
    return EgoBackend(
        spaces=store,
        profile_id=profile_id,
        task_id=task_id,
        binary=binary,
        ego_profile_id=cfg.profile_id,
        runner=runner,
        require_windows=require_windows,
    )
