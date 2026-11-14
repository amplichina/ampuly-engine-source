"""Owner-triggerable sandbox-shop Ego probe (engine-owned; no external agent).

Vertical slice proving Ampuly can open the dedicated Ego Ampuly profile
``sandbox-shop`` (Ego Chromium Profile 2 / 沙箱店), navigate the seller
home path, and write Task Space receipts — runnable from ``ampuly browser
shop-home`` without Grok/Cursor driving each session.

Does NOT: scan chat QR, submit listings, flip IM ``wire_ready``, elevate A21
YAML, or claim C01/C03. Listing gaps stay in ``listing_controlled_contract``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ampuly.layer4_tools.browser_backend import Action, run_batch
from ampuly.layer4_tools.browser_provider import describe_browser_backend
from ampuly.layer4_tools.ego_backend import EgoBackend, EgoUnavailable, open_ego_backend
from ampuly.layer4_tools.space_artifacts import SpaceArtifactLayout

# Ampuly-managed Ego partition used for E1 shop session (not the owner's daily Chrome).
DEFAULT_AMPULY_PROFILE_ID = "sandbox-shop"
DEFAULT_SELLER_HOME = "https://myseller.taobao.com/"
DEFAULT_CS_HOME = "https://myseller.taobao.com/home.htm/_firstmenuid_/2416"
# Fixed offset — avoids requiring tzdata on Windows product images.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _now_shanghai() -> str:
    return datetime.now(tz=SHANGHAI).isoformat(timespec="seconds")


def _task_id(prefix: str = "shop-home") -> str:
    stamp = datetime.now(tz=SHANGHAI).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}"


@dataclass
class ShopHomeProbeResult:
    """Structured receipt payload for Owner / Desktop / CLI."""

    ok: bool
    capability: str = "browser.shop_home"
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID
    task_id: str = ""
    url: str = DEFAULT_SELLER_HOME
    backend: str = "ego"
    selection_reason: str = ""
    space_dir: str | None = None
    space_id: str | None = None
    receipt_path: str | None = None
    capture_path: str | None = None
    snapshot_ok: bool = False
    navigate_ok: bool = False
    batch_ok: bool = False
    seller_home_hint: bool = False
    error: str = ""
    sealed_at: str = field(default_factory=_now_shanghai)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _snapshot_hint(snapshot: Mapping[str, Any] | None) -> bool:
    """Cheap text hint that seller workbench chrome is present (not a login QR)."""
    if not snapshot:
        return False
    blob = json.dumps(snapshot, ensure_ascii=False).casefold()
    positives = ("千牛", "myseller", "工作台", "商家", "seller")
    negatives = ("扫码登录", "qrcode", "scan to log")
    if any(n in blob for n in negatives):
        return False
    return any(p in blob for p in positives)


def run_shop_home_probe(
    *,
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID,
    url: str = DEFAULT_SELLER_HOME,
    task_id: str | None = None,
    env: Mapping[str, str] | None = None,
    runner=None,
    require_windows: bool = True,
    capture: bool = True,
    close: bool = True,
    backend: EgoBackend | None = None,
) -> ShopHomeProbeResult:
    """Open Ego sandbox-shop, navigate seller home, write Space receipt.

    Prefers an injected ``backend`` (tests). Otherwise opens Ego via
    ``open_ego_backend`` — this leaf refuses the Playwright path because the
    live shop session lives on the dedicated Ego profile.
    """
    source = dict(env) if env is not None else dict(os.environ)
    tid = task_id or _task_id()
    desc = describe_browser_backend(source)
    result = ShopHomeProbeResult(
        ok=False,
        profile_id=profile_id,
        task_id=tid,
        url=url,
        backend=str(desc.get("backend") or ""),
        selection_reason=str(desc.get("selection_reason") or ""),
    )

    owned = backend is None
    backend_ref: EgoBackend | None = backend
    try:
        if backend_ref is None:
            # Force Ego for this leaf even if auto-selection would pick playwright.
            force_env = dict(source)
            force_env["AMPULY_BROWSER_BACKEND"] = "ego"
            backend_ref = open_ego_backend(
                profile_id=profile_id,
                task_id=tid,
                env=force_env,
                runner=runner,
                require_windows=require_windows,
            )
        result.backend = "ego"
        result.space_dir = str(Path(backend_ref.space.directory))
        result.space_id = backend_ref.space.space_id

        actions: list[Action] = [
            Action("navigate", url),
            Action("wait", meta={"ms": 1500}),
        ]
        batch = run_batch(backend_ref, actions)
        result.batch_ok = batch.ok
        result.navigate_ok = bool(batch.results and batch.results[0].ok)
        if not batch.ok:
            result.error = batch.error or "batch_failed"
            result.evidence["batch"] = batch.as_dict()
        else:
            snap = backend_ref.snapshot()
            result.snapshot_ok = bool(snap.get("ok", True))
            result.seller_home_hint = _snapshot_hint(snap)
            result.evidence["snapshot_keys"] = sorted(snap.keys())[:20]
            nodes = snap.get("snapshot") or snap.get("nodes") or snap
            if isinstance(nodes, dict):
                result.evidence["snapshot_node_hint"] = len(
                    json.dumps(nodes, ensure_ascii=False)
                )

            capture_path: str | None = None
            if capture:
                cap = backend_ref.act(Action("capture", ""))
                capture_path = str(cap.get("path") or "") or None
                result.capture_path = capture_path

            layout = SpaceArtifactLayout(space=backend_ref.space)
            receipt_body = {
                "capability": "browser.shop_home",
                "owner_trigger": True,
                "external_agent": False,
                "profile_id": profile_id,
                "task_id": tid,
                "url": url,
                "navigate_ok": result.navigate_ok,
                "snapshot_ok": result.snapshot_ok,
                "seller_home_hint": result.seller_home_hint,
                "capture_path": capture_path,
                "sealed_at": _now_shanghai(),
                "zone": "Asia/Shanghai",
                "selection": {
                    "backend": result.backend,
                    "selection_reason": result.selection_reason,
                    "describe": {
                        k: desc.get(k)
                        for k in (
                            "backend",
                            "selection_reason",
                            "binary_found",
                            "ego_profile_id_set",
                            "hybrid_strategy",
                        )
                    },
                },
            }
            receipt_path = layout.write_receipt(receipt_body)
            result.receipt_path = str(receipt_path)
            result.ok = bool(result.navigate_ok and result.snapshot_ok)
            if result.ok and not result.seller_home_hint:
                result.evidence["note"] = (
                    "navigate+snapshot ok; seller_home_hint false — "
                    "session may need Owner login assist in Ego Profile, not chat QR"
                )
    except EgoUnavailable as exc:
        result.error = f"ego_unavailable:{exc.reason}"
    except Exception as exc:  # noqa: BLE001 — surface to Owner receipt
        result.error = f"{type(exc).__name__}:{exc}"
    finally:
        if owned and backend_ref is not None and close:
            try:
                backend_ref.close()
            except Exception:  # noqa: BLE001
                pass

    if not result.receipt_path and result.space_dir and backend_ref is not None:
        try:
            layout = SpaceArtifactLayout(space=backend_ref.space)
            path = layout.write_receipt(result.to_dict())
            result.receipt_path = str(path)
        except Exception:  # noqa: BLE001
            pass
    return result


def describe_shop_ego_capability() -> dict[str, Any]:
    """Static capability card for Desktop / diagnostics."""
    return {
        "id": "browser.shop_home",
        "title": "Sandbox shop Ego seller-home probe",
        "owner_triggerable": True,
        "external_agent_required": False,
        "cli": "ampuly browser shop-home",
        "default_profile_id": DEFAULT_AMPULY_PROFILE_ID,
        "default_url": DEFAULT_SELLER_HOME,
        "requires": [
            "ego-browser binary",
            "AMPULY_EGO_SPACES_ROOT or AMPULY_HOME",
            "Ego profile sandbox-shop",
        ],
        "writes": ["Task Space receipt-NNNN.json", "captures/*.png"],
        "does_not": [
            "chat QR login",
            "listing submit",
            "IM wire_ready",
            "A21 YAML elevate",
        ],
    }


def run_shop_cs_home_probe(
    *,
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID,
    url: str = DEFAULT_CS_HOME,
    task_id: str | None = None,
    env: Mapping[str, str] | None = None,
    runner=None,
    require_windows: bool = True,
    capture: bool = True,
    close: bool = True,
    backend: EgoBackend | None = None,
) -> ShopHomeProbeResult:
    """Open Ego sandbox-shop, navigate 客服/千牛入口, write Space receipt.

    Same Owner leaf pattern as ``shop-home``. Does not send IM messages.
    """
    tid = task_id or _task_id(prefix="shop-cs-home")
    result = run_shop_home_probe(
        profile_id=profile_id,
        url=url,
        task_id=tid,
        env=env,
        runner=runner,
        require_windows=require_windows,
        capture=capture,
        close=close,
        backend=backend,
    )
    result.capability = "browser.shop_cs_home"
    return result


def describe_shop_cs_ego_capability() -> dict[str, Any]:
    """Static capability card for Desktop / diagnostics (客服 entry)."""
    return {
        "id": "browser.shop_cs_home",
        "title": "Sandbox shop Ego 客服/千牛 entry probe",
        "owner_triggerable": True,
        "external_agent_required": False,
        "cli": "ampuly browser shop-cs-home",
        "default_profile_id": DEFAULT_AMPULY_PROFILE_ID,
        "default_url": DEFAULT_CS_HOME,
        "requires": [
            "ego-browser binary",
            "AMPULY_EGO_SPACES_ROOT or AMPULY_HOME",
            "Ego profile sandbox-shop",
        ],
        "writes": ["Task Space receipt-NNNN.json", "captures/*.png"],
        "does_not": [
            "send IM messages",
            "chat QR login",
            "listing submit",
            "A21 YAML elevate",
        ],
    }
