"""Per-shop product catalog index + inbox drop ingest (engine-owned).

Foolproof path for employees/Owner:

1. Drop a listing JSON into `catalog/inbox/`
2. Run `ampuly shop catalog ingest-drop` (or pass `--file`)
3. Engine upserts `catalog/index.json` (+ optional `catalog/items/{id}.json`)
4. Writes a receipt under `receipts/`

This slice adds one-shot / poll watch + optional post-ingest listing-draft attempt
(dry_run offline or live Ego). Still no vector RAG, no auto-accept rules, no mass_publish.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from ampuly.layer4_tools.shop_workspace import (
    DEFAULT_BUSINESS_ID,
    DEFAULT_SHOP_ID,
    _now,
    _safe_id,
    _stamp,
    init_workspace,
    workspace_dir,
    write_json,
)

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
CATALOG_SCHEMA = 1
INDEX_NAME = "index.json"
INBOX_NAME = "inbox"
ITEMS_NAME = "items"
PROCESSED_NAME = "processed"

DRAFT_MODE_OFF = "off"
DRAFT_MODE_DRY_RUN = "dry_run"
DRAFT_MODE_LIVE = "live"
DraftMode = Literal["off", "dry_run", "live"]




def catalog_dir(ws: Path) -> Path:
    return Path(ws) / "catalog"


def catalog_index_path(ws: Path) -> Path:
    return catalog_dir(ws) / INDEX_NAME


def catalog_inbox_dir(ws: Path) -> Path:
    return catalog_dir(ws) / INBOX_NAME


def catalog_items_dir(ws: Path) -> Path:
    return catalog_dir(ws) / ITEMS_NAME


def catalog_processed_dir(ws: Path) -> Path:
    return catalog_inbox_dir(ws) / PROCESSED_NAME


def empty_index(*, business_id: str, shop_id: str) -> dict[str, Any]:
    return {
        "schema_version": CATALOG_SCHEMA,
        "kind": "ampuly.shop_catalog_index",
        "business_id": business_id,
        "shop_id": shop_id,
        "updated_at": _now(),
        "items": [],
    }


def ensure_catalog_layout(
    home: Path,
    *,
    business_id: str = DEFAULT_BUSINESS_ID,
    shop_id: str = DEFAULT_SHOP_ID,
) -> Path:
    """Ensure workspace + catalog/inbox/items/processed + README contract."""
    init_workspace(home, business_id=business_id, shop_id=shop_id, seed_stub=True)
    ws = workspace_dir(home, business_id, shop_id)
    for d in (
        catalog_dir(ws),
        catalog_inbox_dir(ws),
        catalog_items_dir(ws),
        catalog_processed_dir(ws),
    ):
        d.mkdir(parents=True, exist_ok=True)
    idx = catalog_index_path(ws)
    if not idx.exists():
        write_json(idx, empty_index(business_id=business_id, shop_id=shop_id))
    readme = catalog_dir(ws) / "README.md"
    body = (
        "# catalog\n\n"
        "商品库 / 在架与草稿索引。引擎与 Owner 只读；勿放密钥。\n\n"
        "## 傻瓜路径（拖拽进仓）\n\n"
        "1. 把商品资料 JSON 拖进 `inbox/`\n"
        "2. 一条命令（推荐）：\n"
        "   `ampuly shop catalog watch-inbox --once --then-draft --draft-mode dry-run`\n"
        "   （等价：先 `ingest-drop`，再按店规尝试上架草稿计划；dry_run 不打开 Ego）\n"
        "3. 引擎写入 `index.json` + `items/{listing_id}.json`，并落回执；"
        "店规已 accepted 时写入 then-draft 回执（品牌用店规授权品牌，如安普里）\n"
        "4. 真上架草稿：`--draft-mode live`（需 Ego 已登录卖家工作台；"
        "可能要在真窗点类目叶子）\n\n"
        "禁止：自动 accept 店规、臆造类目、mass_publish、向量 RAG。\n"
    )
    if not readme.exists() or "watch-inbox" not in readme.read_text(encoding="utf-8", errors="ignore"):
        readme.write_text(body, encoding="utf-8")
    inbox_readme = catalog_inbox_dir(ws) / "README.md"
    inbox_body = (
        "# catalog/inbox — 拖拽投放区\n\n"
        "把一份商品 JSON（至少含 `listing_id` 或 `id`，建议含 `title`/`brand`/`category`）"
        "拖到本目录，然后执行：\n\n"
        "```bat\n"
        "set AMPULY_HOME=C:\\w\\ampuly-home\n"
        "set PATH=C:\\w\\venv-ampuly\\Scripts;%PATH%\n"
        "ampuly shop catalog watch-inbox --business ampuly --shop sandbox-shop "
        "--once --then-draft --draft-mode dry-run\n"
        "```\n\n"
        "处理成功的文件会移到 `processed/`。\n"
        "`--draft-mode live` 才会打开 Ego 跑 `shop-listing-draft`；"
        "默认 dry_run 只验证店规门闸并写回执。\n"
        "不会自动 accept 店规，不会臆造类目，不会 mass_publish。\n"
    )
    if (not inbox_readme.exists()) or "watch-inbox" not in inbox_readme.read_text(
        encoding="utf-8", errors="ignore"
    ):
        inbox_readme.write_text(inbox_body, encoding="utf-8")
    return ws


def load_index(ws: Path) -> dict[str, Any]:
    path = catalog_index_path(ws)
    if not path.is_file():
        return empty_index(
            business_id=str((json.loads((ws / "WORKSPACE.json").read_text(encoding="utf-8")) if (ws / "WORKSPACE.json").exists() else {}).get("business_id") or DEFAULT_BUSINESS_ID),
            shop_id=str((json.loads((ws / "WORKSPACE.json").read_text(encoding="utf-8")) if (ws / "WORKSPACE.json").exists() else {}).get("shop_id") or DEFAULT_SHOP_ID),
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("catalog_index_not_object")
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def _normalize_item(raw: Mapping[str, Any], *, business_id: str, shop_id: str) -> dict[str, Any]:
    listing_id = (
        raw.get("listing_id")
        or raw.get("id")
        or raw.get("item_id")
        or raw.get("num_iid")
    )
    if listing_id is None or not str(listing_id).strip():
        raise ValueError("listing_id_required")
    lid = str(listing_id).strip()
    _safe_id(lid)
    title = raw.get("title") or raw.get("name") or f"listing-{lid}"
    category = raw.get("category") or raw.get("category_keyword")
    brand = raw.get("brand")
    item = {
        "listing_id": lid,
        "title": str(title).strip(),
        "brand": str(brand).strip() if brand else None,
        "category": str(category).strip() if category else None,
        "status": str(raw.get("status") or "indexed"),
        "source": str(raw.get("source") or "owner_upsert"),
        "business_id": business_id,
        "shop_id": shop_id,
        "updated_at": _now(),
        "extra": {
            k: v
            for k, v in dict(raw).items()
            if k
            not in {
                "listing_id",
                "id",
                "item_id",
                "num_iid",
                "title",
                "name",
                "brand",
                "category",
                "category_keyword",
                "status",
                "source",
                "business_id",
                "shop_id",
                "updated_at",
                "extra",
            }
        },
    }
    return item


def upsert_listing(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    listing: Mapping[str, Any],
    write_item_file: bool = True,
) -> dict[str, Any]:
    ws = ensure_catalog_layout(home, business_id=business_id, shop_id=shop_id)
    try:
        item = _normalize_item(listing, business_id=business_id, shop_id=shop_id)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "business_id": business_id,
            "shop_id": shop_id,
        }
    index = load_index(ws)
    index["business_id"] = business_id
    index["shop_id"] = shop_id
    items = [i for i in index.get("items") or [] if isinstance(i, dict)]
    replaced = False
    for i, existing in enumerate(items):
        if str(existing.get("listing_id")) == item["listing_id"]:
            items[i] = {**existing, **item}
            replaced = True
            break
    if not replaced:
        items.append(item)
    index["items"] = items
    index["updated_at"] = _now()
    index["schema_version"] = CATALOG_SCHEMA
    index["kind"] = "ampuly.shop_catalog_index"
    write_json(catalog_index_path(ws), index)
    item_path = None
    if write_item_file:
        item_path = catalog_items_dir(ws) / f"{item['listing_id']}.json"
        write_json(item_path, item)
    receipt = {
        "kind": "shop.catalog_upsert",
        "at": _now(),
        "business_id": business_id,
        "shop_id": shop_id,
        "listing_id": item["listing_id"],
        "replaced": replaced,
        "index_path": str(catalog_index_path(ws)),
        "item_path": str(item_path) if item_path else None,
    }
    write_json(ws / "receipts" / f"catalog-upsert-{_stamp()}.json", receipt)
    return {"ok": True, **receipt, "item": item, "count": len(items)}


def list_catalog(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
) -> dict[str, Any]:
    ws = ensure_catalog_layout(home, business_id=business_id, shop_id=shop_id)
    index = load_index(ws)
    return {
        "ok": True,
        "business_id": business_id,
        "shop_id": shop_id,
        "index_path": str(catalog_index_path(ws)),
        "inbox_path": str(catalog_inbox_dir(ws)),
        "count": len(index.get("items") or []),
        "items": list(index.get("items") or []),
        "updated_at": index.get("updated_at"),
    }


def _candidate_drop_files(inbox: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(inbox.iterdir(), key=lambda x: x.stat().st_mtime):
        if not p.is_file():
            continue
        if p.name.lower() == "readme.md":
            continue
        if p.suffix.lower() in {".json", ".jsonl", ".md", ".txt"}:
            files.append(p)
    return files


def _load_drop_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("drop_json_must_be_object")
        return data
    # md/txt: try fenced JSON or whole-file JSON; else title-from-filename stub
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    # minimal: require Owner to use JSON for now
    raise ValueError("drop_must_be_json_object_for_this_slice")


def ingest_drop(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    file: Path | None = None,
    move_processed: bool = True,
    then_draft: bool = False,
    draft_mode: DraftMode | str = DRAFT_MODE_DRY_RUN,
    profile_id: str = "sandbox-shop",
    allow_without_rules: bool = False,
    env: Mapping[str, str] | None = None,
    draft_runner: Callable[..., Any] | None = None,
    wait_category_seconds: int = 300,
) -> dict[str, Any]:
    """Ingest one dropped file (or oldest inbox file) into the catalog index.

    When then_draft=True, after a successful upsert attempt listing-draft via
    attempt_listing_draft_after_ingest (dry_run default; live needs Ego).
    """
    ws = ensure_catalog_layout(home, business_id=business_id, shop_id=shop_id)
    inbox = catalog_inbox_dir(ws)
    target: Path | None = Path(file) if file is not None else None
    if target is None:
        candidates = _candidate_drop_files(inbox)
        if not candidates:
            return {
                "ok": False,
                "error": "inbox_empty",
                "inbox_path": str(inbox),
                "hint": "Drop a listing JSON into catalog/inbox then re-run",
            }
        target = candidates[0]
    if not target.is_file():
        return {"ok": False, "error": f"drop_missing:{target}"}
    try:
        payload = _load_drop_payload(target)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"drop_parse:{type(exc).__name__}:{exc}", "file": str(target)}
    payload = dict(payload)
    payload.setdefault("source", "inbox_drop")
    result = upsert_listing(
        home,
        business_id=business_id,
        shop_id=shop_id,
        listing=payload,
    )
    processed_to = None
    if move_processed and result.get("ok"):
        catalog_processed_dir(ws).mkdir(parents=True, exist_ok=True)
        dest = catalog_processed_dir(ws) / f"{_stamp()}-{target.name}"
        try:
            # Only move if file lives under inbox (or explicitly requested)
            shutil.move(str(target), str(dest))
            processed_to = str(dest)
        except Exception as exc:  # noqa: BLE001
            processed_to = f"move_failed:{exc}"
    receipt = {
        "kind": "shop.catalog_ingest_drop",
        "at": _now(),
        "business_id": business_id,
        "shop_id": shop_id,
        "file": str(target),
        "processed_to": processed_to,
        "listing_id": result.get("listing_id"),
        "ok": bool(result.get("ok")),
    }
    write_json(ws / "receipts" / f"catalog-ingest-{_stamp()}.json", receipt)
    out = {
        **result,
        **receipt,
        "next_hint": (
            "Foolproof: ampuly shop catalog watch-inbox --once --then-draft "
            "--draft-mode dry-run  (live needs Ego login)."
        ),
    }
    if then_draft and out.get("ok"):
        listing_obj = out.get("item")
        if not isinstance(listing_obj, dict):
            listing_obj = {
                "listing_id": out.get("listing_id"),
            }
        draft = attempt_listing_draft_after_ingest(
            home,
            business_id=business_id,
            shop_id=shop_id,
            listing=listing_obj,
            draft_mode=draft_mode,
            profile_id=profile_id,
            allow_without_rules=allow_without_rules,
            env=env,
            draft_runner=draft_runner,
            wait_category_seconds=wait_category_seconds,
        )
        out["draft"] = draft
        if draft.get("skipped"):
            out["next_hint"] = (
                f"Ingest OK; draft skipped ({draft.get('skip_reason')}). "
                "Accept shop rules first, then re-run with --then-draft."
            )
        elif draft.get("draft_mode") == DRAFT_MODE_DRY_RUN and draft.get("ok"):
            out["next_hint"] = draft.get("hint") or out["next_hint"]
        elif not draft.get("ok"):
            out["next_hint"] = (
                f"Ingest OK; draft attempt failed: {draft.get('error')}. "
                "Check Ego login / Owner category pick."
            )
    return out



def resolve_draft_plan(
    *,
    home: Path,
    business_id: str,
    shop_id: str,
    listing: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
    allow_without_rules: bool = False,
    profile_id: str = "sandbox-shop",
) -> dict[str, Any]:
    """Build a listing-draft plan from accepted shop rules + catalog item.

    Never invents categories. Never auto-accepts rules. Never mass_publish.
    Category/brand come from the rules gate first; listing fields only if they
    already appear in authorized/allowed lists.
    """
    from ampuly.layer4_tools.shop_workspace import require_accepted_rules_for_listing

    gate = require_accepted_rules_for_listing(
        profile_id=profile_id,
        business_id=business_id,
        shop_id=shop_id,
        env=env or {"AMPULY_HOME": str(home)},
        allow_without_rules=allow_without_rules,
    )
    plan: dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "gate": gate.to_dict(),
        "business_id": business_id,
        "shop_id": shop_id,
        "listing_id": (listing or {}).get("listing_id"),
        "category_keyword": None,
        "preferred_brand": None,
        "title": None,
        "error": "",
    }
    if not gate.ok:
        plan["skipped"] = True
        plan["error"] = gate.error or "shop_rules_required"
        plan["skip_reason"] = plan["error"]
        return plan

    from ampuly.layer4_tools.shop_workspace import catalog_product_facts

    facts = catalog_product_facts(listing, gate.rules or {})
    if not facts["ok"]:
        plan["skipped"] = True
        plan["error"] = facts["error"]
        plan["skip_reason"] = facts["error"]
        plan["sentence_zh"] = facts["sentence_zh"]
        plan["allowed_categories"] = facts["allowed_categories"]
        plan["authorized_brands"] = facts["authorized_brands"]
        return plan

    plan.update(
        {
            "ok": True,
            "category_keyword": facts["category_keyword"],
            "preferred_brand": facts["preferred_brand"],
            "title": facts["title"],
            "allowed_categories": facts["allowed_categories"],
            "authorized_brands": facts["authorized_brands"],
            "would_call": "ampuly browser shop-listing-draft",
            "does_not": [
                "auto-accept shop rules",
                "invent categories",
                "mass_publish",
            ],
        }
    )
    return plan


def attempt_listing_draft_after_ingest(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    listing: Mapping[str, Any] | None,
    draft_mode: DraftMode | str = DRAFT_MODE_DRY_RUN,
    profile_id: str = "sandbox-shop",
    allow_without_rules: bool = False,
    env: Mapping[str, str] | None = None,
    draft_runner: Callable[..., Any] | None = None,
    wait_category_seconds: int = 300,
) -> dict[str, Any]:
    """After catalog ingest: plan (and optionally run) shop-listing-draft.

    draft_mode:
      - off: no-op
      - dry_run: gate + plan receipt only (no Ego) — default for CI/Owner smoke
      - live: call run_shop_listing_draft (needs Ego login)
    """
    mode = str(draft_mode or DRAFT_MODE_OFF).strip().lower()
    if mode in {"", "0", "false", "none", "off"}:
        mode = DRAFT_MODE_OFF
    if mode not in {DRAFT_MODE_OFF, DRAFT_MODE_DRY_RUN, DRAFT_MODE_LIVE}:
        return {
            "ok": False,
            "error": f"invalid_draft_mode:{draft_mode}",
            "draft_mode": str(draft_mode),
        }
    if mode == DRAFT_MODE_OFF:
        return {
            "ok": True,
            "draft_mode": DRAFT_MODE_OFF,
            "attempted": False,
            "skipped": True,
            "skip_reason": "draft_mode_off",
        }

    plan = resolve_draft_plan(
        home=home,
        business_id=business_id,
        shop_id=shop_id,
        listing=listing,
        env=env,
        allow_without_rules=allow_without_rules,
        profile_id=profile_id,
    )
    receipt: dict[str, Any] = {
        "kind": "shop.catalog_then_draft",
        "at": _now(),
        "business_id": business_id,
        "shop_id": shop_id,
        "draft_mode": mode,
        "attempted": False,
        "skipped": bool(plan.get("skipped")),
        "skip_reason": plan.get("skip_reason") or plan.get("error") or "",
        "plan": plan,
        "live_result": None,
    }
    if not plan.get("ok"):
        receipt["ok"] = False
        receipt["error"] = plan.get("error") or "draft_plan_failed"
        write_json(
            workspace_dir(home, business_id, shop_id)
            / "receipts"
            / f"catalog-then-draft-{_stamp()}.json",
            receipt,
        )
        return receipt

    receipt["attempted"] = True
    receipt["category_keyword"] = plan.get("category_keyword")
    receipt["preferred_brand"] = plan.get("preferred_brand")
    receipt["title"] = plan.get("title")

    if mode == DRAFT_MODE_DRY_RUN:
        receipt["ok"] = True
        receipt["live_ego_required"] = True
        receipt["hint"] = (
            "dry_run only — catalog indexed + rules gate OK. "
            "Owner: ampuly shop catalog ingest-drop --then-draft --draft-mode live "
            "(or watch-inbox --once --then-draft --draft-mode live) after Ego login."
        )
        write_json(
            workspace_dir(home, business_id, shop_id)
            / "receipts"
            / f"catalog-then-draft-{_stamp()}.json",
            receipt,
        )
        return receipt

    runner = draft_runner
    if runner is None:
        from ampuly.layer4_tools.shop_listing_owner import run_shop_listing_draft

        def _default_runner(**kwargs):
            return run_shop_listing_draft(**kwargs)

        runner = _default_runner

    live = runner(
        profile_id=profile_id,
        category_keyword=str(plan["category_keyword"]),
        brand=plan.get("preferred_brand"),
        title=plan.get("title"),
        wait_category_seconds=wait_category_seconds,
        business_id=business_id,
        shop_id=shop_id,
        allow_without_rules=allow_without_rules,
        env=env or {"AMPULY_HOME": str(home)},
    )
    live_dict = live.to_dict() if hasattr(live, "to_dict") else dict(live)
    receipt["live_result"] = live_dict
    receipt["ok"] = bool(live_dict.get("ok"))
    if not receipt["ok"]:
        receipt["error"] = live_dict.get("error") or "live_draft_failed"
        if live_dict.get("owner_assist_needed"):
            receipt["owner_assist_needed"] = True
            receipt["owner_step"] = live_dict.get("owner_step")
    write_json(
        workspace_dir(home, business_id, shop_id)
        / "receipts"
        / f"catalog-then-draft-{_stamp()}.json",
        receipt,
    )
    return receipt


def poll_inbox(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    then_draft: bool = False,
    draft_mode: DraftMode | str = DRAFT_MODE_DRY_RUN,
    max_files: int | None = None,
    profile_id: str = "sandbox-shop",
    allow_without_rules: bool = False,
    env: Mapping[str, str] | None = None,
    draft_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One-shot poll: ingest each current inbox drop (oldest first)."""
    ws = ensure_catalog_layout(home, business_id=business_id, shop_id=shop_id)
    inbox = catalog_inbox_dir(ws)
    candidates = _candidate_drop_files(inbox)
    if max_files is not None:
        candidates = candidates[: max(0, int(max_files))]
    results: list[dict[str, Any]] = []
    for target in candidates:
        one = ingest_drop(
            home,
            business_id=business_id,
            shop_id=shop_id,
            file=target,
            then_draft=then_draft,
            draft_mode=draft_mode,
            profile_id=profile_id,
            allow_without_rules=allow_without_rules,
            env=env,
            draft_runner=draft_runner,
        )
        results.append(one)
    card = {
        "kind": "shop.catalog_poll_inbox",
        "at": _now(),
        "ok": True,
        "business_id": business_id,
        "shop_id": shop_id,
        "inbox_path": str(inbox),
        "seen": len(candidates),
        "processed": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "then_draft": then_draft,
        "draft_mode": str(draft_mode) if then_draft else DRAFT_MODE_OFF,
        "results": results,
    }
    if candidates and card["failed"] and not card["processed"]:
        card["ok"] = False
        card["error"] = "all_ingest_failed"
    elif not candidates:
        card["ok"] = True
        card["error"] = "inbox_empty"
        card["hint"] = (
            f"Drop listing JSON into {inbox} then re-run "
            "ampuly shop catalog watch-inbox --once"
        )
    write_json(ws / "receipts" / f"catalog-poll-{_stamp()}.json", card)
    return card


def watch_inbox(
    home: Path,
    *,
    business_id: str,
    shop_id: str,
    once: bool = True,
    poll_seconds: float = 2.0,
    max_cycles: int = 1,
    then_draft: bool = False,
    draft_mode: DraftMode | str = DRAFT_MODE_DRY_RUN,
    max_files_per_cycle: int | None = None,
    profile_id: str = "sandbox-shop",
    allow_without_rules: bool = False,
    env: Mapping[str, str] | None = None,
    draft_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Watch/poll catalog/inbox. Default once=True (no long-running daemon required)."""
    import time

    cycles = 1 if once else max(1, int(max_cycles))
    cycle_cards: list[dict[str, Any]] = []
    total_processed = 0
    for i in range(cycles):
        card = poll_inbox(
            home,
            business_id=business_id,
            shop_id=shop_id,
            then_draft=then_draft,
            draft_mode=draft_mode,
            max_files=max_files_per_cycle,
            profile_id=profile_id,
            allow_without_rules=allow_without_rules,
            env=env,
            draft_runner=draft_runner,
        )
        cycle_cards.append(card)
        total_processed += int(card.get("processed") or 0)
        if once:
            break
        if i + 1 < cycles:
            time.sleep(max(0.0, float(poll_seconds)))
    summary = {
        "kind": "shop.catalog_watch_inbox",
        "at": _now(),
        "ok": True,
        "business_id": business_id,
        "shop_id": shop_id,
        "once": once,
        "cycles": len(cycle_cards),
        "processed_total": total_processed,
        "then_draft": then_draft,
        "draft_mode": str(draft_mode) if then_draft else DRAFT_MODE_OFF,
        "inbox_path": cycle_cards[0].get("inbox_path") if cycle_cards else None,
        "cycles_detail": cycle_cards,
        "foolproof": {
            "drop_folder": "AMPULY_HOME/workspaces/{business}/{shop}/catalog/inbox/",
            "one_command": (
                "ampuly shop catalog watch-inbox --once --then-draft --draft-mode dry-run"
            ),
            "live_after_ego": (
                "ampuly shop catalog watch-inbox --once --then-draft --draft-mode live"
            ),
        },
    }
    if cycle_cards and all(c.get("error") == "inbox_empty" for c in cycle_cards):
        summary["error"] = "inbox_empty"
        summary["hint"] = cycle_cards[0].get("hint")
    ws = workspace_dir(home, business_id, shop_id)
    write_json(ws / "receipts" / f"catalog-watch-{_stamp()}.json", summary)
    return summary




def describe_catalog_capability() -> dict[str, Any]:
    return {
        "id": "shop.catalog_inbox",
        "title": "Shop catalog index + inbox watch/ingest + then-draft",
        "owner_triggerable": True,
        "cli": [
            "ampuly shop catalog list",
            "ampuly shop catalog upsert",
            "ampuly shop catalog ingest-drop",
            "ampuly shop catalog ingest-drop --then-draft --draft-mode dry-run",
            "ampuly shop catalog watch-inbox --once --then-draft --draft-mode dry-run",
        ],
        "drop_path": "AMPULY_HOME/workspaces/{business_id}/{shop_id}/catalog/inbox/",
        "index_path": "AMPULY_HOME/workspaces/{business_id}/{shop_id}/catalog/index.json",
        "foolproof": (
            "Drop JSON into catalog/inbox then: "
            "ampuly shop catalog watch-inbox --once --then-draft --draft-mode dry-run"
        ),
        "product_target": "drag-drop into inbox → auto listing/sales chain",
        "this_slice": (
            "inbox + index + ingest-drop + watch/poll + then-draft "
            "(dry_run offline / live Ego); no daemon required; no RAG; "
            "no auto-accept rules; no mass_publish"
        ),
        "does_not": [
            "auto-accept shop rules",
            "invent categories",
            "mass_publish",
            "vector RAG",
            "flip CRM REAL_DATA / clean_machine",
        ],
    }
