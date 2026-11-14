"""Owner-triggerable sandbox-shop listing draft cycle (engine-owned).

`ampuly browser shop-listing-draft` may read back an existing draft intent.
New submissions require an explicitly configured merchant verification adapter
that produces entity/merchant/state evidence from the actual Ego snapshot.
Uses `listing_controlled_contract`; the generic snapshot is not business proof.

Does NOT elevate A21 YAML, claim mass publish, or flip IM wire_ready.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ampuly.channels.listing_controlled_contract import assess_listing_controlled
from ampuly.layer4_tools.browser_backend import Action
from ampuly.layer4_tools.ego_backend import EgoBackend, EgoUnavailable, open_ego_backend
from ampuly.layer4_tools.shop_ego_owner import DEFAULT_AMPULY_PROFILE_ID
from ampuly.layer4_tools.shop_listing_intents import (
    INTENT_SCHEMA, LOCKED_PHASES as _LOCKED_PHASES, ListingIntentRefused,
    claim_intent as _claim_intent, effect_key as _effect_key,
    load_intent as _load_intent, update_intent as _update_intent,
)
from ampuly.layer4_tools.space_artifacts import SpaceArtifactLayout
from ampuly.layer4_tools.shop_merchant_readback import (
    FORM_FIELDS, SAVE_SELECTOR, MerchantContract, MerchantReadbackRefused,
    configured_contract, exact_entity, observe,
)

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
DRAFT = "保存草稿"
WAREHOUSE_BTN = "放入仓库"


def _now() -> str:
    return datetime.now(tz=SHANGHAI).isoformat(timespec="seconds")


def _task_id(prefix: str = "shop-listing") -> str:
    return f"{prefix}-{datetime.now(tz=SHANGHAI).strftime('%Y%m%d-%H%M%S')}"


def _intent_file(home: Path, business_id: str, shop_id: str, key: str) -> Path:
    from ampuly.layer4_tools.shop_workspace import workspace_dir

    return workspace_dir(home, business_id, shop_id) / "effects" / f"{key}.json"


def _account_for_shop(gate: Any, backend: EgoBackend) -> tuple[str, str]:
    """Shop scope keeps the Ego profile. A missing or foreign account writes nothing."""
    account = str(getattr(backend, "ego_profile_id", "") or "").strip()
    rules = gate.rules if isinstance(getattr(gate, "rules", None), dict) else None
    scope = str((rules or {}).get("ego_profile_id") or "").strip()
    if not account:
        return "", "ego_profile_missing"
    if rules is not None and not scope:
        return "", "shop_scope_account_missing"
    if scope and account != scope:
        return "", "ego_profile_not_in_shop_scope"
    return account, ""


def _read_merchant(backend: EgoBackend, contract: MerchantContract, *, profile: str, url: str):
    navigated = backend.act(Action("navigate", url))
    if navigated.get("ok") is not True or navigated.get("unknown") is True:
        raise MerchantReadbackRefused("merchant_navigation_unavailable")
    return observe(backend.snapshot(merchant=True), contract, profile=profile, url=url)


def _submit_original_draft(
    backend_ref: EgoBackend, result: ListingDraftResult, evidence: dict[str, bool], *,
    intent_path: Path, effect_key: str, account: str, profile_id: str,
    business_id: str, shop_id: str, task_id: str, title: str, category_keyword: str,
    contract: MerchantContract, brand: str,
) -> dict[str, Any]:
    """One durable claim, one guarded click, then only independent reads."""
    request_key = secrets.token_hex(24)
    before = _read_merchant(backend_ref, contract, profile=account, url=contract.readback_url)
    if any(row.request_key == request_key for row in before.entities):
        raise MerchantReadbackRefused("merchant_request_already_present")
    form = _read_merchant(backend_ref, contract, profile=account, url=contract.form_url)
    fields = {"title": title, "category": category_keyword, "brand": brand, "request": request_key}
    for key, value in fields.items():
        filled = backend_ref.act(Action("fill", FORM_FIELDS[key], value))
        if filled.get("ok") is not True or filled.get("unknown") is True:
            raise MerchantReadbackRefused("merchant_form_unavailable")
    # No default shop, category, price, or inventory is guessed by this protocol.
    intent = {
        "schema": INTENT_SCHEMA, "idempotency_key": effect_key, "task_id": task_id,
        "attempt_id": f"{effect_key}:submit:1", "account": account, "profile_id": profile_id,
        "business_id": business_id, "shop_id": shop_id, "title": title,
        "category_keyword": category_keyword, "action": DRAFT, "phase": "submit_started",
        "entity_id": "", "target_state": "draft", "merchant_id": contract.merchant_id,
        "readback_contract": contract.digest, "request_key": request_key,
        "preflight_action_id": form.action_id,
    }
    _claim_intent(intent_path, intent)
    # Claim before the sole effect. Even a before-effect refusal requires explicit
    # recovery; it never rotates selectors or opens a second submission attempt.
    click = backend_ref.act(Action("click", SAVE_SELECTOR, meta={"merchant_guard": contract.submit_guard(fields)}))
    unknown = {**intent, "phase": "unknown"}
    _update_intent(intent_path, unknown, expected=intent)
    result.log.append({"submit": SAVE_SELECTOR, "transport_ok": click.get("ok") is True,
                       "unknown": True, "contract": contract.schema})
    evidence.update(seller_session_logged_in=True, publish_router_reachable=True, category_selected=True)
    return unknown


def _read_back_draft(
    backend_ref: EgoBackend, result: ListingDraftResult, evidence: dict[str, bool], *,
    account: str, contract: MerchantContract, intent: Mapping[str, Any],
):
    if (intent.get("merchant_id") != contract.merchant_id or intent.get("readback_contract") != contract.digest
            or not isinstance(intent.get("request_key"), str)
            or not re.fullmatch(r"[a-f0-9]{48}", intent["request_key"])):
        raise MerchantReadbackRefused("merchant_intent_contract_mismatch")
    observed = _read_merchant(backend_ref, contract, profile=account, url=contract.readback_url)
    if observed.action_id == intent.get("preflight_action_id"):
        raise MerchantReadbackRefused("merchant_readback_not_independent")
    found = exact_entity(observed, request_key=str(intent["request_key"]),
                         entity_id=str(intent.get("entity_id") or ""), state=str(intent["target_state"]))
    evidence["warehouse_or_draft_readback"] = found is not None
    result.log.append({"readback_url": observed.url, "verified": found is not None,
                       "snapshot_digest": observed.snapshot_digest, "action_id": observed.action_id,
                       "contract": contract.schema, "entity_id": found.entity_id if found else ""})
    return found


@dataclass
class ListingDraftResult:
    ok: bool
    capability: str = "browser.shop_listing_draft"
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID
    task_id: str = ""
    title: str = ""
    category_keyword: str = "平面广告设计"
    space_dir: str | None = None
    receipt_path: str | None = None
    capture_path: str | None = None
    evidence_ticks: dict[str, bool] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    owner_assist_needed: bool = False
    owner_window: str = "Ampuly Engine · 安普里引擎 - ego"
    owner_step: str = "核对商家读回合同和页面证据"
    error: str = ""
    sealed_at: str = field(default_factory=_now)
    log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def describe_shop_listing_capability() -> dict[str, Any]:
    return {
        "id": "browser.shop_listing_draft",
        "title": "Sandbox shop Ego listing draft + readback",
        "owner_triggerable": True,
        "external_agent_required": False,
        "cli": "ampuly browser shop-listing-draft",
        "default_profile_id": DEFAULT_AMPULY_PROFILE_ID,
        "default_url": "",
        "merchant_contract": "ampuly.merchant.links.v1",
        "requires": [
            "ego-browser binary",
            "AMPULY_HOME / AMPULY_EGO_SPACES_ROOT",
            "Ego profile sandbox-shop logged in",
            "Accepted shop rules under AMPULY_HOME/workspaces/.../rules/shop-rules.json",
            "AMPULY_SHOP_READBACK_CONTRACT: host-bound ampuly.merchant.links.v1 site contract",
        ],
        "writes": ["Task Space receipt", "captures"],
        "does_not": [
            "chat QR",
            "A21 YAML elevate",
            "IM wire_ready",
            "mass publish",
            "run without accepted shop rules (unless --allow-without-rules)",
        ],
    }


def run_shop_listing_draft(
    *,
    profile_id: str = DEFAULT_AMPULY_PROFILE_ID,
    category_keyword: str = "平面广告设计",
    brand: str | None = None,
    title: str | None = None,
    wait_category_seconds: int = 300,
    task_id: str | None = None,
    env: Mapping[str, str] | None = None,
    runner=None,
    require_windows: bool = True,
    capture: bool = True,
    close: bool = True,
    backend: EgoBackend | None = None,
    allow_without_rules: bool = False,
    business_id: str | None = None,
    shop_id: str | None = None,
) -> ListingDraftResult:
    """Read back existing effects; refuse a new write without a verification adapter.

    Rules-first: refuses unless AMPULY_HOME workspaces/.../rules/shop-rules.json
    is status=accepted (unless allow_without_rules).
    """
    source = dict(env) if env is not None else dict(os.environ)
    tid = task_id or _task_id()
    stamp = datetime.now(tz=SHANGHAI).strftime("%H%M%S")
    item_title = title or f"平面设计服务A21t{stamp}"
    result = ListingDraftResult(
        ok=False,
        profile_id=profile_id,
        task_id=tid,
        title=item_title,
        category_keyword=category_keyword,
    )
    # --- shop rules gate (人定规) ---
    from ampuly.layer4_tools.shop_workspace import require_accepted_rules_for_listing

    gate = require_accepted_rules_for_listing(
        profile_id=profile_id,
        business_id=business_id,
        shop_id=shop_id,
        env=source,
        allow_without_rules=allow_without_rules,
    )
    result.log.append({"rules_gate": gate.to_dict()})
    if not gate.ok:
        result.error = gate.error or "shop_rules_required"
        result.owner_assist_needed = True
        result.owner_step = (
            "先跑 ampuly shop workspace init / rules set|accept-candidate，"
            "店规 status=accepted 后再 shop-listing-draft"
        )
        return result
    from ampuly.layer4_tools.shop_workspace import catalog_product_facts

    facts = catalog_product_facts(
        {"category": category_keyword, "category_keyword": category_keyword, "brand": brand, "title": item_title},
        gate.rules or {},
    )
    if not facts["ok"]:
        result.error = str(facts["error"])
        result.owner_assist_needed = True
        result.owner_step = str(facts["sentence_zh"])
        result.log.append({"product_facts": facts["error"]})
        return result
    category_keyword = str(facts["category_keyword"])
    brand_fill = str(facts["preferred_brand"])
    result.category_keyword = category_keyword
    result.log.append({"product_facts": {"category_keyword": category_keyword, "brand": brand_fill}})

    evidence = {
        "seller_session_logged_in": False,
        "publish_router_reachable": False,
        "category_selected": False,
        "draft_or_warehouse_submit": False,
        "warehouse_or_draft_readback": False,
    }
    owned = backend is None
    backend_ref: EgoBackend | None = backend
    verified = False
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
        account, account_error = _account_for_shop(gate, backend_ref)
        if account_error:
            result.error = account_error
            return result
        from ampuly.layer4_tools.shop_workspace import ampuly_home

        try:
            home = ampuly_home(source)
        except ValueError as exc:
            result.error = str(exc)
            return result
        effect_key = _effect_key(
            business_id=gate.business_id,
            shop_id=gate.shop_id,
            title=item_title,
            category=category_keyword,
        )
        intent_path = _intent_file(home, gate.business_id, gate.shop_id, effect_key)
        prior = _load_intent(intent_path)
        if prior and str(prior.get("account") or "") != account:
            result.error = "ego_account_changed"
            return result
        readback_only = bool(prior) and str(prior.get("phase") or "") in _LOCKED_PHASES
        result.space_dir = str(Path(backend_ref.space.directory))
        contract = configured_contract(source, business_id=gate.business_id, shop_id=gate.shop_id)
        if not readback_only:
            required = set((gate.rules or {}).get("required_fields") or [])
            if required - {"title", "category", "brand"}:
                raise MerchantReadbackRefused("merchant_required_fields_unavailable")
            prior = _submit_original_draft(
                backend_ref, result, evidence, intent_path=intent_path, effect_key=effect_key,
                account=account, profile_id=profile_id, business_id=gate.business_id, shop_id=gate.shop_id,
                task_id=tid, title=item_title, category_keyword=category_keyword, contract=contract, brand=brand_fill,
            )
        result.log.append({"readback_only": readback_only, "idempotency_key": effect_key, "phase": prior.get("phase")})
        found = _read_back_draft(
            backend_ref, result, evidence, account=account, contract=contract, intent=prior,
        )
        verified = found is not None
        if verified:
            updated = {**prior, "phase": "verified", "entity_id": found.entity_id}
            _update_intent(intent_path, updated, expected=prior)
            evidence["draft_or_warehouse_submit"] = True
        else:
            result.error = "merchant_readback_unverified"

        ready = assess_listing_controlled(evidence, detail={"title": item_title, "task_id": tid, "idempotency_key": effect_key})
        result.evidence_ticks = evidence
        result.readiness = ready.to_payload()
        result.ok = bool(verified)

        layout = SpaceArtifactLayout(space=backend_ref.space)
        result.receipt_path = str(layout.write_receipt(result.to_dict()))
    except EgoUnavailable as exc:
        result.error = f"ego_unavailable:{exc.reason}"
    except ListingIntentRefused as exc:
        result.error = exc.reason
    except MerchantReadbackRefused as exc:
        result.error = exc.reason
        result.owner_assist_needed = True
        result.owner_step = "需要匹配的商家读回合同及可核验页面证据；不会补猜商品身份或重提。"
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}:{exc}"
    finally:
        result.evidence_ticks = evidence
        if owned and backend_ref is not None and close:
            try:
                backend_ref.close()
            except Exception:  # noqa: BLE001
                pass
    return result
