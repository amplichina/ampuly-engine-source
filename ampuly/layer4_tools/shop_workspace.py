"""Per-business / per-shop workspace + shop rules (engine-owned).

Humans first梳理店规; engine reads accepted rules then plans listing paths.
Limited self-learn writes *candidate* rules only; Owner must
``ampuly shop rules accept-candidate`` before入库.

Layout under AMPULY_HOME::

    workspaces/{business_id}/{shop_id}/
      WORKSPACE.json
      rules/shop-rules.json          # accepted | stub (never auto-promote)
      rules/candidates/*.json        # learn-from-listing proposals
      catalog/
      knowledge/
      receipts/
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
SCHEMA_VERSION = 1
DEFAULT_BUSINESS_ID = "ampuly"
DEFAULT_SHOP_ID = "sandbox-shop"
DEFAULT_SHOP_NAME = "Example shop"
# Marketplace listing UI placeholder — NEVER auto-add to authorized_brands.
MARKETPLACE_BRAND_PLACEHOLDER = "无品牌"
DEFAULT_EGO_PROFILE = "sandbox-shop"
DEFAULT_REQUIRED_FIELDS = ["title", "price", "stock", "category", "brand"]
DEFAULT_FORBIDDEN = [
    "blind_category_guess",
    "mass_publish",
    "auto_accept_candidate",
    "delete_all_listings",
]

STATUS_STUB = "stub"
STATUS_CANDIDATE = "candidate"
STATUS_ACCEPTED = "accepted"


def _now() -> str:
    return datetime.now(tz=SHANGHAI).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(tz=SHANGHAI).strftime("%Y%m%d-%H%M%S")


def ampuly_home(env: Mapping[str, str] | None = None) -> Path:
    source = dict(env) if env is not None else dict(os.environ)
    raw = (source.get("AMPULY_HOME") or "").strip()
    if not raw:
        raise ValueError("AMPULY_HOME_required")
    return Path(raw)


def workspaces_root(home: Path) -> Path:
    return Path(home) / "workspaces"


def workspace_dir(home: Path, business_id: str, shop_id: str) -> Path:
    return workspaces_root(home) / _safe_id(business_id) / _safe_id(shop_id)


def _safe_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("id_required")
    if ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"id_unsafe:{value!r}")
    return text


def rules_path(ws: Path) -> Path:
    return ws / "rules" / "shop-rules.json"


def candidates_dir(ws: Path) -> Path:
    return ws / "rules" / "candidates"


def stub_shop_rules(
    *,
    business_id: str = DEFAULT_BUSINESS_ID,
    shop_id: str = DEFAULT_SHOP_ID,
    shop_display_name: str = DEFAULT_SHOP_NAME,
    ego_profile_id: str = DEFAULT_EGO_PROFILE,
    reference_listing_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Minimal stub — NOT accepted; Owner must fill brands/categories then accept."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_STUB,
        "business_id": business_id,
        "shop_id": shop_id,
        "shop_display_name": shop_display_name,
        "brand_id": None,
        "ego_profile_id": ego_profile_id,
        "allowed_categories": [],
        "authorized_brands": [],
        "required_fields": list(DEFAULT_REQUIRED_FIELDS),
        "reference_listing_ids": list(reference_listing_ids or []),
        "forbidden_actions": list(DEFAULT_FORBIDDEN),
        "notes": (
            "Unaccepted shop-rules stub. "
            "Fill allowed_categories + authorized_brands (or learn-from-listing "
            "→ accept-candidate) before shop-listing-draft will run."
        ),
        "source": "seed_stub",
        "accepted_at": None,
        "accepted_by": None,
        "learned_from": None,
    }


def validate_shop_rules(data: Any) -> list[str]:
    """Lightweight validator (no jsonschema dep). Returns list of errors."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["not_an_object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_must_be_1")
    status = data.get("status")
    if status not in (STATUS_STUB, STATUS_CANDIDATE, STATUS_ACCEPTED):
        errors.append("status_invalid")
    for key in ("business_id", "shop_id"):
        if not isinstance(data.get(key), str) or not str(data.get(key)).strip():
            errors.append(f"{key}_required")
    for key in (
        "allowed_categories",
        "authorized_brands",
        "required_fields",
        "reference_listing_ids",
        "forbidden_actions",
    ):
        if key not in data:
            errors.append(f"{key}_required")
        elif not isinstance(data.get(key), list):
            errors.append(f"{key}_must_be_array")
    cats = data.get("allowed_categories")
    if isinstance(cats, list):
        for i, item in enumerate(cats):
            if not isinstance(item, dict):
                errors.append(f"allowed_categories[{i}]_not_object")
            elif not isinstance(item.get("keyword"), str) or not item["keyword"].strip():
                errors.append(f"allowed_categories[{i}].keyword_required")
    if status == STATUS_ACCEPTED:
        if not isinstance(cats, list) or not cats:
            errors.append("accepted_requires_allowed_categories")
        brands = data.get("authorized_brands")
        if not isinstance(brands, list) or not brands:
            errors.append("accepted_requires_authorized_brands")
    return errors


def load_shop_rules(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"shop_rules_missing:{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    errs = validate_shop_rules(data)
    if errs:
        raise ValueError("shop_rules_invalid:" + ",".join(errs))
    return data


def write_json(path: Path, data: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@dataclass
class WorkspaceInitResult:
    ok: bool
    business_id: str
    shop_id: str
    workspace_dir: str
    rules_path: str
    created: bool = False
    seeded_stub: bool = False
    error: str = ""
    sealed_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def init_workspace(
    home: Path,
    *,
    business_id: str = DEFAULT_BUSINESS_ID,
    shop_id: str = DEFAULT_SHOP_ID,
    shop_display_name: str = DEFAULT_SHOP_NAME,
    ego_profile_id: str = DEFAULT_EGO_PROFILE,
    seed_stub: bool = True,
    reference_listing_ids: list[str] | None = None,
) -> WorkspaceInitResult:
    ws = workspace_dir(home, business_id, shop_id)
    created = not ws.exists()
    for sub in ("rules", "rules/candidates", "catalog", "catalog/inbox", "catalog/inbox/processed", "catalog/items", "knowledge", "receipts"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    meta = {
        "kind": "ampuly.shop_workspace",
        "schema_version": 1,
        "business_id": business_id,
        "shop_id": shop_id,
        "shop_display_name": shop_display_name,
        "ego_profile_id": ego_profile_id,
        "created_at": _now(),
        "layout": ["rules", "catalog", "knowledge", "receipts"],
    }
    meta_path = ws / "WORKSPACE.json"
    if not meta_path.exists():
        write_json(meta_path, meta)
    rp = rules_path(ws)
    seeded = False
    if seed_stub and not rp.exists():
        write_json(
            rp,
            stub_shop_rules(
                business_id=business_id,
                shop_id=shop_id,
                shop_display_name=shop_display_name,
                ego_profile_id=ego_profile_id,
                reference_listing_ids=reference_listing_ids,
            ),
        )
        seeded = True
    for readme_name, body in (
        (
            "catalog/README.md",
            (
                "# catalog\n\n商品库 / 在架与草稿索引。引擎与 Owner 可读；勿放密钥。\n\n"
                "## 傻瓜路径（拖拽进仓）\n\n"
                "1. 把商品资料 JSON 拖进 `inbox/`\n"
                "2. 运行：`ampuly shop catalog ingest-drop`\n"
                "3. 引擎写入 `index.json`；全自动 watcher 为后续目标\n"
            ),
        ),
        (
            "catalog/inbox/README.md",
            (
                "# catalog/inbox — 拖拽投放区\n\n"
                "拖入 listing JSON 后执行 `ampuly shop catalog ingest-drop`。\n"
            ),
        ),
        (
            "knowledge/README.md",
            "# knowledge\n\n店规说明、类目备注、品牌授权说明等人类知识。\n",
        ),
        (
            "receipts/README.md",
            "# receipts\n\n本店 Owner 操作回执副本（learn / accept / listing gate）。\n",
        ),
    ):
        p = ws / readme_name
        if not p.exists():
            p.write_text(body, encoding="utf-8")
    return WorkspaceInitResult(
        ok=True,
        business_id=business_id,
        shop_id=shop_id,
        workspace_dir=str(ws),
        rules_path=str(rp),
        created=created,
        seeded_stub=seeded,
    )


def show_rules(home: Path, business_id: str, shop_id: str) -> dict[str, Any]:
    ws = workspace_dir(home, business_id, shop_id)
    rp = rules_path(ws)
    if not rp.exists():
        return {
            "ok": False,
            "error": "shop_rules_missing",
            "workspace_dir": str(ws),
            "hint": "ampuly shop workspace init",
        }
    data = load_shop_rules(rp)
    return {
        "ok": True,
        "workspace_dir": str(ws),
        "rules_path": str(rp),
        "status": data.get("status"),
        "rules": data,
        "listing_unlocked": data.get("status") == STATUS_ACCEPTED,
    }


def set_rules(
    home: Path,
    business_id: str,
    shop_id: str,
    rules: Mapping[str, Any],
    *,
    as_accepted: bool = False,
) -> dict[str, Any]:
    ws = workspace_dir(home, business_id, shop_id)
    init_workspace(
        home,
        business_id=business_id,
        shop_id=shop_id,
        shop_display_name=str(rules.get("shop_display_name") or shop_id),
        ego_profile_id=str(rules.get("ego_profile_id") or DEFAULT_EGO_PROFILE),
        seed_stub=False,
    )
    payload = dict(rules)
    payload["schema_version"] = SCHEMA_VERSION
    payload["business_id"] = business_id
    payload["shop_id"] = shop_id
    if as_accepted:
        payload["status"] = STATUS_ACCEPTED
        payload["accepted_at"] = payload.get("accepted_at") or _now()
        payload["accepted_by"] = payload.get("accepted_by") or "owner"
        payload["source"] = payload.get("source") or "owner_manual"
    errs = validate_shop_rules(payload)
    if errs:
        return {"ok": False, "error": "shop_rules_invalid:" + ",".join(errs)}
    if payload.get("status") == STATUS_CANDIDATE:
        return {
            "ok": False,
            "error": "refuse_write_candidate_as_active",
            "hint": "use learn-from-listing then accept-candidate",
        }
    path = write_json(rules_path(ws), payload)
    receipt = {
        "kind": "shop.rules_set",
        "at": _now(),
        "business_id": business_id,
        "shop_id": shop_id,
        "status": payload.get("status"),
        "rules_path": str(path),
    }
    write_json(ws / "receipts" / f"rules-set-{_stamp()}.json", receipt)
    return {"ok": True, **receipt, "listing_unlocked": payload.get("status") == STATUS_ACCEPTED}



def is_marketplace_brand_placeholder(name: str | None) -> bool:
    """True when name is the Taobao/listing UI placeholder, not a shop-authorized brand."""
    return bool(name) and str(name).strip() == MARKETPLACE_BRAND_PLACEHOLDER


def prefer_authorized_brands(
    *,
    candidates: list[str] | None = None,
    blob: str = "",
    shop_display_name: str = "",
    brand_id: str | None = None,
) -> list[str]:
    """Build authorized_brands for learn/candidate heuristics.

    Rules:
    - Never include marketplace placeholder 「无品牌」.
    - Keep explicit brand candidates.
    - If still empty, use an explicitly supplied brand_id or leave empty for Owner input.
    - Display names and page text never imply a distribution-specific brand.
    """
    brands: list[str] = []
    for raw in candidates or []:
        if not isinstance(raw, str):
            continue
        b = raw.strip()
        if not b or is_marketplace_brand_placeholder(b):
            continue
        if b not in brands:
            brands.append(b)
    if brands:
        return brands
    bid = str(brand_id).strip() if brand_id else ""
    if bid and not is_marketplace_brand_placeholder(bid):
        return [bid]
    # Otherwise leave empty for Owner fill — never invent 无品牌.
    return []


def propose_candidate_from_structure(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    listing_id: str,
    structure: Mapping[str, Any],
    method: str = "structure_json",
) -> dict[str, Any]:
    """Write a candidate rules file from extracted listing structure. Never auto-applies."""
    ws = workspace_dir(home, business_id, shop_id)
    init_workspace(home, business_id=business_id, shop_id=shop_id, seed_stub=True)
    cats: list[dict[str, Any]] = []
    raw_cats = structure.get("allowed_categories") or structure.get("categories") or []
    if isinstance(raw_cats, list):
        for item in raw_cats:
            if isinstance(item, str) and item.strip():
                cats.append({"keyword": item.strip(), "path": []})
            elif isinstance(item, dict) and item.get("keyword"):
                cats.append(
                    {
                        "keyword": str(item["keyword"]).strip(),
                        "path": list(item.get("path") or []),
                        "notes": str(item.get("notes") or ""),
                    }
                )
    keyword = structure.get("category_keyword") or structure.get("category")
    if keyword and not cats:
        cats.append({"keyword": str(keyword).strip(), "path": list(structure.get("category_path") or [])})
    raw_brands: list[str] = []
    for b in structure.get("authorized_brands") or structure.get("brands") or []:
        if isinstance(b, str) and b.strip():
            raw_brands.append(b.strip())
    brand = structure.get("brand")
    if isinstance(brand, str) and brand.strip() and brand.strip() not in raw_brands:
        raw_brands.append(brand.strip())
    display_name = str(structure.get("shop_display_name") or DEFAULT_SHOP_NAME)
    brand_id = structure.get("brand_id")
    brands = prefer_authorized_brands(
        candidates=raw_brands,
        shop_display_name=display_name,
        brand_id=str(brand_id) if brand_id else None,
    )
    # If Owner left brand_id empty but we resolved an own brand, seed brand_id too.
    if not brand_id and brands:
        brand_id = brands[0]
    title = structure.get("title")
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_CANDIDATE,
        "business_id": business_id,
        "shop_id": shop_id,
        "shop_display_name": display_name,
        "brand_id": brand_id,
        "ego_profile_id": structure.get("ego_profile_id") or DEFAULT_EGO_PROFILE,
        "allowed_categories": cats,
        "authorized_brands": brands,
        "required_fields": list(structure.get("required_fields") or DEFAULT_REQUIRED_FIELDS),
        "reference_listing_ids": [str(listing_id)],
        "forbidden_actions": list(DEFAULT_FORBIDDEN),
        "notes": (
            f"Candidate learned from listing_id={listing_id} via {method}. "
            "Owner must review and run accept-candidate before listing unlock."
        ),
        "source": "learn_from_listing",
        "accepted_at": None,
        "accepted_by": None,
        "learned_from": {
            "listing_id": str(listing_id),
            "method": method,
            "at": _now(),
            "title": title,
            "raw_keys": sorted(str(k) for k in structure.keys()),
        },
    }
    errs = validate_shop_rules(candidate)
    # candidate may lack cats/brands; still allow write but flag incomplete
    soft = [e for e in errs if e not in ("accepted_requires_allowed_categories", "accepted_requires_authorized_brands")]
    if soft:
        return {"ok": False, "error": "candidate_invalid:" + ",".join(soft), "candidate": candidate}
    cdir = candidates_dir(ws)
    cdir.mkdir(parents=True, exist_ok=True)
    out = cdir / f"candidate-{_stamp()}-listing-{_safe_id(str(listing_id))}.json"
    write_json(out, candidate)
    receipt = {
        "kind": "shop.rules_learn_candidate",
        "at": _now(),
        "business_id": business_id,
        "shop_id": shop_id,
        "listing_id": str(listing_id),
        "candidate_path": str(out),
        "incomplete": {
            "categories_empty": not bool(cats),
            "brands_empty": not bool(brands),
        },
        "auto_applied": False,
    }
    write_json(ws / "receipts" / f"learn-{_stamp()}.json", receipt)
    return {"ok": True, **receipt, "candidate": candidate}


def ego_account_gate(
    *,
    home: Path | None = None,
    business_id: str | None = None,
    shop_id: str | None = None,
    profile_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> SimpleNamespace:
    """Shop-rules view for ``_account_for_shop``. No rules file means presence only."""
    resolved = home
    if resolved is None:
        try:
            resolved = ampuly_home(env)
        except ValueError:
            return SimpleNamespace(rules=None)
    if business_id and shop_id:
        bid, sid = business_id, shop_id
    else:
        bid, sid = resolve_shop_ids_for_profile(profile_id or DEFAULT_EGO_PROFILE)
        if business_id:
            bid = business_id
        if shop_id:
            sid = shop_id
    path = rules_path(workspace_dir(resolved, bid, sid))
    if not path.exists():
        return SimpleNamespace(rules=None)
    return SimpleNamespace(rules=load_shop_rules(path))


def learn_from_listing(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    listing_id: str,
    structure: Mapping[str, Any] | None = None,
    use_ego: bool = False,
    profile_id: str = DEFAULT_EGO_PROFILE,
    env: Mapping[str, str] | None = None,
    runner=None,
    require_windows: bool = True,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Propose candidate rules. Ego is optional and best-effort; never auto-applies.

    ``use_ego`` keeps the bound local Ego profile. A missing profile refuses
    before any browser script.
    """
    if structure is not None:
        return propose_candidate_from_structure(
            home,
            business_id=business_id,
            shop_id=shop_id,
            listing_id=listing_id,
            structure=structure,
            method="structure_json",
        )
    if use_ego:
        extracted = _extract_structure_via_ego(
            home=home,
            business_id=business_id,
            shop_id=shop_id,
            listing_id=listing_id,
            profile_id=profile_id,
            env=env,
            runner=runner,
            require_windows=require_windows,
            task_id=task_id,
        )
        if not extracted.get("ok"):
            return {
                "ok": False,
                "error": extracted.get("error") or "ego_extract_failed",
                "hint": "pass --from-json with category/brand fields, or fix Ego session",
            }
        return propose_candidate_from_structure(
            home,
            business_id=business_id,
            shop_id=shop_id,
            listing_id=listing_id,
            structure=extracted["structure"],
            method="ego_snapshot",
        )
    # Offline fallback: seed candidate pointing at listing id only (Owner fills)
    return propose_candidate_from_structure(
        home,
        business_id=business_id,
        shop_id=shop_id,
        listing_id=listing_id,
        structure={
            "notes": "offline learn stub — Owner must fill categories/brands before accept",
            "allowed_categories": [],
            "authorized_brands": [],
        },
        method="listing_id_only",
    )


def _extract_structure_via_ego(
    *,
    home: Path,
    business_id: str,
    shop_id: str,
    listing_id: str,
    profile_id: str,
    env: Mapping[str, str] | None,
    runner=None,
    require_windows: bool = True,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Best-effort: open seller manage / draft search and scrape snapshot text."""
    try:
        from ampuly.layer4_tools.browser_backend import Action
        from ampuly.layer4_tools.ego_backend import open_ego_backend
        from ampuly.layer4_tools.shop_listing_owner import _account_for_shop
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ego_import:{exc}"}
    source = dict(env) if env is not None else dict(os.environ)
    source["AMPULY_BROWSER_BACKEND"] = "ego"
    backend = None
    try:
        backend = open_ego_backend(
            profile_id=profile_id,
            task_id=task_id or f"shop-learn-{_stamp()}",
            env=source,
            runner=runner,
            require_windows=require_windows,
        )
        account, account_error = _account_for_shop(
            ego_account_gate(
                home=home,
                business_id=business_id,
                shop_id=shop_id,
                env=source,
            ),
            backend,
        )
        if account_error:
            return {"ok": False, "error": account_error}
        url = "https://myseller.taobao.com/home.htm/SellManage/"
        backend.act(Action("navigate", url))
        backend.act(Action("wait", meta={"ms": 2000}))
        for sel in (
            'css:input[name="queryTitle"]',
            'css:input[placeholder*="宝贝"]',
            'css:input[placeholder*="标题"]',
        ):
            if backend.act(Action("fill", sel, str(listing_id))).get("ok"):
                backend.act(Action("press", "Enter"))
                backend.act(Action("wait", meta={"ms": 2000}))
                break
        snap = backend.snapshot()
        blob = json.dumps(snap, ensure_ascii=False) if snap else ""
        structure: dict[str, Any] = {
            "listing_id": listing_id,
            "ego_profile_id": account,
            "allowed_categories": [],
            "authorized_brands": [],
            "snapshot_chars": len(blob),
        }
        # Categories and brands require explicit owner input; a page snapshot
        # does not authorize a business category or a default shop brand.
        structure["title_hint"] = listing_id
        return {"ok": True, "structure": structure}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass


def list_candidates(home: Path, business_id: str, shop_id: str) -> list[Path]:
    cdir = candidates_dir(workspace_dir(home, business_id, shop_id))
    if not cdir.is_dir():
        return []
    return sorted(cdir.glob("candidate-*.json"), key=lambda p: p.stat().st_mtime)


def accept_candidate(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    candidate_path: Path | None = None,
    accepted_by: str = "owner",
) -> dict[str, Any]:
    ws = workspace_dir(home, business_id, shop_id)
    if candidate_path is None:
        cands = list_candidates(home, business_id, shop_id)
        if not cands:
            return {"ok": False, "error": "no_candidates"}
        candidate_path = cands[-1]
    path = Path(candidate_path)
    if not path.is_file():
        return {"ok": False, "error": f"candidate_missing:{path}"}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = STATUS_ACCEPTED
    data["accepted_at"] = _now()
    data["accepted_by"] = accepted_by
    data["source"] = "accept_candidate"
    data["business_id"] = business_id
    data["shop_id"] = shop_id
    errs = validate_shop_rules(data)
    if errs:
        return {
            "ok": False,
            "error": "accept_blocked:" + ",".join(errs),
            "hint": "edit candidate to add allowed_categories + authorized_brands",
            "candidate_path": str(path),
        }
    out = write_json(rules_path(ws), data)
    receipt = {
        "kind": "shop.rules_accept_candidate",
        "at": _now(),
        "business_id": business_id,
        "shop_id": shop_id,
        "candidate_path": str(path),
        "rules_path": str(out),
        "accepted_by": accepted_by,
    }
    write_json(ws / "receipts" / f"accept-{_stamp()}.json", receipt)
    return {"ok": True, **receipt, "listing_unlocked": True}


@dataclass
class RulesGateResult:
    ok: bool
    error: str = ""
    business_id: str = DEFAULT_BUSINESS_ID
    shop_id: str = DEFAULT_SHOP_ID
    rules_path: str | None = None
    status: str | None = None
    rules: dict[str, Any] | None = None
    category_keyword: str | None = None
    preferred_brand: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_shop_ids_for_profile(profile_id: str) -> tuple[str, str]:
    """Map Ego profile → (business_id, shop_id). Extend as more shops onboard."""
    pid = (profile_id or "").strip() or DEFAULT_EGO_PROFILE
    if pid == DEFAULT_EGO_PROFILE or pid == "sandbox-shop":
        return DEFAULT_BUSINESS_ID, DEFAULT_SHOP_ID
    # Convention: profile id doubles as shop_id under default business.
    return DEFAULT_BUSINESS_ID, pid


def require_accepted_rules_for_listing(
    *,
    profile_id: str = DEFAULT_EGO_PROFILE,
    business_id: str | None = None,
    shop_id: str | None = None,
    env: Mapping[str, str] | None = None,
    allow_without_rules: bool = False,
) -> RulesGateResult:
    """Gate for shop-listing-draft. Refuses unless accepted rules exist."""
    bid, sid = resolve_shop_ids_for_profile(profile_id)
    if business_id:
        bid = business_id
    if shop_id:
        sid = shop_id
    try:
        home = ampuly_home(env)
    except ValueError as exc:
        if allow_without_rules:
            return RulesGateResult(ok=True, error="ampuly_home_missing_but_allowed", business_id=bid, shop_id=sid)
        return RulesGateResult(ok=False, error=str(exc), business_id=bid, shop_id=sid)
    ws = workspace_dir(home, bid, sid)
    rp = rules_path(ws)
    if not rp.exists():
        if allow_without_rules:
            return RulesGateResult(
                ok=True,
                error="shop_rules_missing_but_allowed",
                business_id=bid,
                shop_id=sid,
                rules_path=str(rp),
            )
        return RulesGateResult(
            ok=False,
            error="shop_rules_required:missing",
            business_id=bid,
            shop_id=sid,
            rules_path=str(rp),
        )
    try:
        data = load_shop_rules(rp)
    except ValueError as exc:
        return RulesGateResult(
            ok=False,
            error=str(exc),
            business_id=bid,
            shop_id=sid,
            rules_path=str(rp),
        )
    status = str(data.get("status"))
    if status != STATUS_ACCEPTED:
        if allow_without_rules:
            return RulesGateResult(
                ok=True,
                error=f"shop_rules_not_accepted_but_allowed:{status}",
                business_id=bid,
                shop_id=sid,
                rules_path=str(rp),
                status=status,
                rules=data,
            )
        return RulesGateResult(
            ok=False,
            error=f"shop_rules_required:status={status}",
            business_id=bid,
            shop_id=sid,
            rules_path=str(rp),
            status=status,
            rules=data,
        )
    # Allowlists validate a product. They do not choose one: the first category
    # or brand is not this item's fact, and 无品牌 is never an authorized brand.
    return RulesGateResult(
        ok=True,
        business_id=bid,
        shop_id=sid,
        rules_path=str(rp),
        status=status,
        rules=data,
        category_keyword=None,
        preferred_brand=None,
    )


def catalog_product_facts(listing: Mapping[str, Any] | None, rules: Mapping[str, Any] | None) -> dict[str, Any]:
    """Category and brand come from the catalog item. Shop rules only check them.

    A missing value stays missing. An allowlist does not fill it, and the
    marketplace placeholder 无品牌 is never treated as authorized. A shop's
    brand must be explicitly present on the catalog item.
    """
    item = dict(listing or {})
    rule_set = dict(rules or {})
    allowed: list[str] = []
    for row in rule_set.get("allowed_categories") or []:
        if isinstance(row, dict):
            keyword = row.get("keyword") or row.get("name")
            if keyword and str(keyword).strip():
                allowed.append(str(keyword).strip())
        elif isinstance(row, str) and row.strip():
            allowed.append(row.strip())
    raw_brands = [str(name).strip() for name in (rule_set.get("authorized_brands") or []) if isinstance(name, str) and str(name).strip()]
    brands = [name for name in raw_brands if not is_marketplace_brand_placeholder(name)]
    category = str(item.get("category") or item.get("category_keyword") or "").strip()
    brand = str(item.get("brand") or "").strip()
    title = item.get("title") or item.get("name")
    title = str(title).strip() if title else None

    def pending(error: str, sentence: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": error,
            "sentence_zh": sentence,
            "category_keyword": category or None,
            "preferred_brand": brand or None,
            "title": title,
            "allowed_categories": allowed,
            "authorized_brands": brands,
        }

    if not category:
        return pending(
            "category_missing_from_catalog",
            "这个商品还没有类目。店规里的允许名单不能拿第一项来填，补上这条商品自己的类目后再上架。",
        )
    if allowed and category not in allowed:
        return pending(
            f"category_not_in_allowed_rules:{category}",
            f"类目「{category}」不在店规允许的名单里，不能改成名单里的另一项。",
        )
    if not brand:
        return pending(
            "brand_missing_from_catalog",
            "这个商品还没有品牌。允许名单不能代替商品事实；请在这条商品上明确填写品牌。",
        )
    if is_marketplace_brand_placeholder(brand):
        return pending(
            "brand_placeholder_not_authorized",
            "无品牌是市场页面的占位，不会自动授权，也不能当作这条商品的品牌。",
        )
    if brands and brand not in brands:
        return pending(
            f"brand_not_in_authorized_rules:{brand}",
            f"品牌「{brand}」不在店规允许的名单里，不能改成名单里的第一项。",
        )
    if not brands and any(is_marketplace_brand_placeholder(name) for name in raw_brands):
        return pending(
            f"brand_not_in_authorized_rules:{brand}",
            "店规里没有可用品牌（无品牌不会自动授权）。",
        )
    return {
        "ok": True,
        "error": "",
        "sentence_zh": "",
        "category_keyword": category,
        "preferred_brand": brand,
        "title": title,
        "allowed_categories": allowed,
        "authorized_brands": brands,
    }


def describe_shop_workspace_capability() -> dict[str, Any]:
    return {
        "id": "shop.workspace_rules",
        "title": "Per-shop workspace + 店规 (rules-first listing)",
        "owner_triggerable": True,
        "external_agent_required": False,
        "cli": [
            "ampuly shop workspace init",
            "ampuly shop rules show",
            "ampuly shop rules set",
            "ampuly shop rules learn-from-listing",
            "ampuly shop rules accept-candidate",
            "ampuly shop catalog list",
            "ampuly shop catalog upsert",
            "ampuly shop catalog ingest-drop",
            "ampuly shop catalog watch-inbox --once --then-draft --draft-mode dry-run",
            "ampuly shop master show",
            "ampuly shop master link",
        ],
        "layout": "AMPULY_HOME/workspaces/{business_id}/{shop_id}/ (+ catalog/inbox drop path)",
        "gate": "shop-listing-draft refuses without status=accepted rules",
        "does_not": [
            "auto-accept learn candidates",
            "blind category guess",
            "share workspaces across shops",
            "print secrets",
        ],
    }
