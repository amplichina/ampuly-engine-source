"""Owner-triggerable Ego UI IM wire probe for 旺旺/千牛客服.

`ampuly browser shop-im-wire` opens the CS entry already proven by
`shop-cs-home`, checks session list / composer chrome, optionally dry-runs
a fill into the reply box (never sends unless `send=True` which stays off by
default), and records `wire_ready` for wire_kind=ego_browser_ui.

Does NOT implement Alibaba push protocol, does not register inbound adapter,
does not put secrets in receipts.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ampuly.channels.ego_im_wire import assess_ego_im_wire
from ampuly.layer4_tools.browser_backend import Action
from ampuly.layer4_tools.ego_backend import EgoBackend, EgoUnavailable, open_ego_backend
from ampuly.layer4_tools.shop_ego_owner import DEFAULT_AMPULY_PROFILE_ID, DEFAULT_CS_HOME
from ampuly.layer4_tools.space_artifacts import SpaceArtifactLayout

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")

_IM_CHROME_HINTS = ("客服", "接待", "会话", "消息", "千牛", "旺旺", "聊天", "咨询")
_COMPOSER_HINTS = ("请输入", "输入消息", "发送", "回复", "textarea", "contenteditable")


def _now() -> str:
    return datetime.now(tz=SHANGHAI).isoformat(timespec="seconds")


def _task_id(prefix: str = "shop-im") -> str:
    return f"{prefix}-{datetime.now(tz=SHANGHAI).strftime('%Y%m%d-%H%M%S')}"


def _blob(snap: Mapping[str, Any] | None) -> str:
    if not snap:
        return ""
    inner = snap.get("result") if isinstance(snap.get("result"), dict) else snap
    if isinstance(inner, dict):
        s = inner.get("snapshot")
        if isinstance(s, str):
            return s
        if s is not None:
            return json.dumps(s, ensure_ascii=False)
    return json.dumps(snap, ensure_ascii=False)


@dataclass
class ShopImWireResult:
    ok: bool
    capability: str = "browser.shop_im_wire"
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID
    task_id: str = ""
    url: str = DEFAULT_CS_HOME
    space_dir: str | None = None
    receipt_path: str | None = None
    capture_path: str | None = None
    evidence_ticks: dict[str, bool] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    wire_ready: bool = False
    wire_kind: str = "ego_browser_ui"
    alibaba_push_protocol: bool = False
    dry_run_fill_ok: bool = False
    error: str = ""
    sealed_at: str = field(default_factory=_now)
    log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def describe_shop_im_capability() -> dict[str, Any]:
    return {
        "id": "browser.shop_im_wire",
        "title": "Sandbox shop Ego IM UI wire probe",
        "owner_triggerable": True,
        "external_agent_required": False,
        "cli": "ampuly browser shop-im-wire",
        "default_profile_id": DEFAULT_AMPULY_PROFILE_ID,
        "default_url": DEFAULT_CS_HOME,
        "wire_kind": "ego_browser_ui",
        "requires": [
            "ego-browser binary",
            "AMPULY_HOME / AMPULY_EGO_SPACES_ROOT",
            "Ego profile sandbox-shop logged in",
        ],
        "writes": ["Task Space receipt", "captures"],
        "does_not": [
            "Alibaba push protocol",
            "accept_inbound for alibaba_shop",
            "chat QR",
            "send customer messages by default",
            "A21 YAML elevate",
        ],
    }


def run_shop_im_wire_probe(
    *,
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID,
    url: str = DEFAULT_CS_HOME,
    dry_run_text: str = "[ampuly-im-dry-run]",
    do_dry_run_fill: bool = True,
    send: bool = False,
    task_id: str | None = None,
    env: Mapping[str, str] | None = None,
    runner=None,
    require_windows: bool = True,
    capture: bool = True,
    close: bool = True,
    backend: EgoBackend | None = None,
) -> ShopImWireResult:
    """Probe CS page for Ego UI IM control; set wire_ready only for ego_browser_ui.

    The local Ego profile from the launch config is kept. A missing profile
    refuses before any browser script.
    """
    if send:
        # Hard refuse auto-send in this leaf — Owner must use a future gated command.
        raise ValueError("shop-im-wire refuses send=True; dry-run fill only")

    source = dict(env) if env is not None else dict(os.environ)
    tid = task_id or _task_id()
    result = ShopImWireResult(ok=False, profile_id=profile_id, task_id=tid, url=url)
    evidence = {
        "seller_session_logged_in": False,
        "cs_home_reachable": False,
        "im_chrome_visible": False,
        "composer_visible": False,
    }
    owned = backend is None
    backend_ref: EgoBackend | None = backend
    try:
        if backend_ref is None:
            force = dict(source)
            force["AMPULY_BROWSER_BACKEND"] = "ego"
            backend_ref = open_ego_backend(
                profile_id=profile_id,
                task_id=tid,
                env=force,
                runner=runner,
                require_windows=require_windows,
            )
        from ampuly.layer4_tools.shop_listing_owner import _account_for_shop
        from ampuly.layer4_tools.shop_workspace import ego_account_gate

        _account, account_error = _account_for_shop(
            ego_account_gate(profile_id=profile_id, env=source),
            backend_ref,
        )
        if account_error:
            result.error = account_error
            return result
        result.space_dir = str(Path(backend_ref.space.directory))

        backend_ref.act(Action("navigate", "https://myseller.taobao.com/"))
        backend_ref.act(Action("wait", meta={"ms": 1200}))
        t = _blob(backend_ref.snapshot())
        evidence["seller_session_logged_in"] = (
            ("商品" in t or "常用" in t or "工作台" in t) and ("扫码登录" not in t)
        )

        backend_ref.act(Action("navigate", url))
        backend_ref.act(Action("wait", meta={"ms": 2500}))
        evidence["cs_home_reachable"] = True
        t = _blob(backend_ref.snapshot())
        evidence["im_chrome_visible"] = any(h in t for h in _IM_CHROME_HINTS)
        evidence["composer_visible"] = any(h in t for h in _COMPOSER_HINTS) or (
            "textbox" in t and evidence["im_chrome_visible"]
        )
        result.log.append(
            {
                "cs_len": len(t),
                "im_chrome": evidence["im_chrome_visible"],
                "composer": evidence["composer_visible"],
            }
        )

        dry_ok = False
        if do_dry_run_fill and evidence["composer_visible"]:
            for sel in (
                'css:textarea',
                'css:[contenteditable="true"]',
                'css:input[placeholder*="输入"]',
                "textbox >> nth=-1",
            ):
                r = backend_ref.act(Action("fill", sel, dry_run_text))
                result.log.append({"dry_fill": sel, "ok": bool(r.get("ok"))})
                if r.get("ok"):
                    dry_ok = True
                    break
        result.dry_run_fill_ok = dry_ok

        if capture:
            cap = backend_ref.act(Action("capture", ""))
            result.capture_path = str(cap.get("path") or "") or None

        ready = assess_ego_im_wire(
            evidence, detail={"task_id": tid, "url": url}, dry_run_fill_ok=dry_ok
        )
        result.evidence_ticks = evidence
        result.readiness = ready.to_payload()
        result.wire_ready = bool(ready.wire_ready)
        result.ok = bool(ready.wire_ready)
        layout = SpaceArtifactLayout(space=backend_ref.space)
        result.receipt_path = str(layout.write_receipt(result.to_dict()))
    except EgoUnavailable as exc:
        result.error = f"ego_unavailable:{exc.reason}"
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}:{exc}"
    finally:
        if owned and backend_ref is not None and close:
            try:
                backend_ref.close()
            except Exception:  # noqa: BLE001
                pass
    return result
