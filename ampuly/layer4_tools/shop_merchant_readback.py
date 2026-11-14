"""Versioned, host-selected merchant protocol over Ego's real content/refs.

``ampuly.merchant.links.v1`` is an opt-in protocol for sites implementing it,
NOT a Taobao adapter. The site renders a top-document #ampuly-shop anchor to
``/ampuly/shops/<merchant>`` and #ampuly-draft-<id> anchors to
``/ampuly/listings/<id>?merchant=...&state=draft&request=...``. The request is
a client-generated correlation token which the site must persist with the
draft, uniquely per merchant. Form inputs are inert until #ampuly-save posts
to /ampuly/listings; that operation creates at most one draft per request.
Neither titles, DOM ref IDs, nor browser profiles identify a draft.

Native snapshot syntax/provenance: ego-lite aecc5831, snapshot-result.ts and
public-api-schema.ts. Unknown layouts/versions are refused, never guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

SCHEMA = "ampuly.merchant.links.v1"
CONFIG_ENV = "AMPULY_SHOP_READBACK_CONTRACT"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_ANCHOR = re.compile(r' *anchor(?: "(?:[^"\\]|\\.)*")? \[([^\r\n]+)\]\Z')
FORM_FIELDS = {"title": "css:#ampuly-title", "category": "css:#ampuly-category",
               "brand": "css:#ampuly-brand", "request": "css:#ampuly-request"}
SAVE_SELECTOR = "css:#ampuly-save"


class MerchantReadbackRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MerchantContract:
    origin: str
    merchant_id: str
    business_id: str
    shop_id: str
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        p = urlsplit(self.origin)
        if (self.schema != SCHEMA or p.scheme != "https" or not p.hostname
                or p.username is not None or p.password is not None or p.query or p.fragment
                or p.path or self.origin != f"https://{p.netloc}"
                or any(not _ID.fullmatch(value) for value in (self.merchant_id, self.business_id, self.shop_id))):
            raise MerchantReadbackRefused("verification_contract_invalid")

    @property
    def form_url(self) -> str:
        return self.origin + "/ampuly/listing/new"

    @property
    def readback_url(self) -> str:
        return self.origin + "/ampuly/listings"

    @property
    def merchant_url(self) -> str:
        return self.origin + "/ampuly/shops/" + self.merchant_id

    @property
    def digest(self) -> str:
        raw = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def submit_guard(self, fields: Mapping[str, str]) -> dict[str, Any]:
        return {"url": self.form_url, "merchant_selector": "#ampuly-shop",
                "merchant_url": self.merchant_url,
                "submit_selector": "#ampuly-save", "submit_url": self.readback_url,
                "fields": {FORM_FIELDS[key].removeprefix("css:"): value for key, value in fields.items()}}


def configured_contract(env: Mapping[str, str], *, business_id: str, shop_id: str) -> MerchantContract:
    """Only host configuration selects the protocol; no page/request discovery."""
    raw = env.get(CONFIG_ENV, "")
    if not raw:
        raise MerchantReadbackRefused("verification_contract_unavailable")
    try:
        data = json.loads(raw) if len(raw) <= 8192 else None
        if type(data) is not dict or set(data) != {"schema", "origin", "merchant_id", "business_id", "shop_id"}:
            raise ValueError
        if any(type(value) is not str for value in data.values()):
            raise ValueError
        contract = MerchantContract(**data)
    except (ValueError, TypeError):
        raise MerchantReadbackRefused("verification_contract_invalid") from None
    if (contract.business_id, contract.shop_id) != (business_id, shop_id):
        raise MerchantReadbackRefused("verification_contract_scope_mismatch")
    return contract


@dataclass(frozen=True)
class MerchantEntity:
    entity_id: str
    merchant_id: str
    state: str
    request_key: str
    ref_id: str


@dataclass(frozen=True)
class MerchantObservation:
    merchant_id: str
    entities: tuple[MerchantEntity, ...]
    action_id: str
    attempt_id: str
    url: str
    snapshot_digest: str


def observe(snapshot: Mapping[str, Any], contract: MerchantContract, *, profile: str, url: str) -> MerchantObservation:
    """Parse only selected, same-document anchors from a fresh accepted envelope."""
    env = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else snapshot
    snap = env.get("snapshot")
    if (env.get("schema") != "ampuly.ego.result.v1" or env.get("ok") is not True
            or env.get("unknown") is not False or env.get("phase") != "completed"
            or env.get("account") != profile or env.get("url") != url
            or url not in {contract.form_url, contract.readback_url}
            or not env.get("action_id") or not env.get("attempt_id")
            or type(snap) is not dict or type(snap.get("content")) is not str
            or type(snap.get("refs")) is not list):
        raise MerchantReadbackRefused("merchant_snapshot_unavailable")
    content, refs = snap["content"], snap["refs"]
    if len(content) > 1_000_000 or len(refs) > 10000:
        raise MerchantReadbackRefused("merchant_snapshot_invalid")
    by_id: dict[str, dict[str, Any]] = {}
    for ref in refs:
        if type(ref) is not dict or type(ref.get("refId")) not in (str, int):
            raise MerchantReadbackRefused("merchant_snapshot_invalid")
        key = str(ref["refId"]).removeprefix("@")
        if not key or key in by_id:
            raise MerchantReadbackRefused("merchant_snapshot_ambiguous")
        by_id[key] = ref
    merchants: list[str] = []
    entities: list[MerchantEntity] = []
    seen_refs: set[str] = set()
    selected_refs = {key for key, ref in by_id.items()
                     if ref.get("loc") == "css:#ampuly-shop" or str(ref.get("loc", "")).startswith("css:#ampuly-draft-")}
    for line in content.splitlines():
        matched = _ANCHOR.fullmatch(line)
        if not matched:
            continue
        pairs = [part.partition("=") for part in matched[1].split(", ")]
        meta = {key: value for key, sep, value in pairs if sep}
        if len(meta) != len(pairs):
            raise MerchantReadbackRefused("merchant_snapshot_ambiguous")
        key = meta.get("ref", "").removeprefix("@")
        ref = by_id.get(key, {})
        loc = ref.get("loc", "")
        if loc != "css:#ampuly-shop" and not str(loc).startswith("css:#ampuly-draft-"):
            continue
        if (key in seen_refs or ref.get("frameProvenance") != "page"
                or meta.get("loc") != loc or ref.get("role") not in ("anchor", "link")):
            raise MerchantReadbackRefused("merchant_snapshot_ambiguous")
        seen_refs.add(key)
        link = meta.get("url", "")
        if loc == "css:#ampuly-shop":
            merchants.append(link)
            continue
        parsed = urlsplit(link)
        entity = parsed.path.removeprefix("/ampuly/listings/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (f"{parsed.scheme}://{parsed.netloc}" != contract.origin or parsed.fragment
                or not parsed.path.startswith("/ampuly/listings/") or not _ID.fullmatch(entity)
                or loc != f"css:#ampuly-draft-{entity}"
                or set(query) != {"merchant", "state", "request"}
                or any(len(values) != 1 or not values[0] for values in query.values())):
            raise MerchantReadbackRefused("merchant_entity_invalid")
        merchant, state, request = (query[key][0] for key in ("merchant", "state", "request"))
        if merchant != contract.merchant_id or state not in {"draft", "warehouse"} or not _ID.fullmatch(request):
            raise MerchantReadbackRefused("merchant_entity_invalid")
        entities.append(MerchantEntity(entity, merchant, state, request, key))
    if seen_refs != selected_refs:
        raise MerchantReadbackRefused("merchant_snapshot_incomplete")
    if merchants != [contract.merchant_url]:
        raise MerchantReadbackRefused("merchant_identity_mismatch")
    if len({row.entity_id for row in entities}) != len(entities):
        raise MerchantReadbackRefused("merchant_snapshot_ambiguous")
    return MerchantObservation(contract.merchant_id, tuple(entities), str(env["action_id"]),
                               str(env["attempt_id"]), url, hashlib.sha256(content.encode()).hexdigest())


def exact_entity(observation: MerchantObservation, *, request_key: str, entity_id: str = "",
                 state: str = "draft") -> MerchantEntity | None:
    candidates = [row for row in observation.entities if row.request_key == request_key]
    if len(candidates) != 1:
        return None
    found = candidates[0]
    return found if found.state == state and (not entity_id or found.entity_id == entity_id) else None
