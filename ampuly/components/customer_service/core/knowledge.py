"""Bounded, configurable local knowledge; no network, model or Engine imports.

All approval metadata is local authoring evidence, not host authorization.
source.reference is a label: it is never fetched, imported or executed.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

from .business import BusinessAssistant, _response
from .contracts import MAX_INBOUND_TEXT_CHARS, bounded_text, canonical_json

SCHEMA = "ampuly.cs.knowledge/1"
CHANNELS = frozenset({"console", "qianniu", "wecom_cs", "wechat_official", "dingtalk", "email"})
KINDS = frozenset({"product", "faq", "policy", "company"})
MAX_BYTES = 1024 * 1024
TOP = {"schema_version", "knowledge_base_id", "name", "tenant_id", "shop_id", "version", "mode", "records"}
COMMON = {"id", "version", "kind", "title", "status", "channels", "valid_from", "valid_until", "source", "review"}
PRODUCT = {"sku", "name", "aliases", "facts"}
ANSWER = {"questions", "keywords", "answer"}


def local_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def _day(value, field):
    if value is None:
        return None
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field}: expected YYYY-MM-DD or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}: invalid date") from exc


def _id(value, field):
    bounded_text(value, field, 128)
    if value != value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"{field}: invalid identity")
    return value


def _keys(value, required, label):
    if type(value) is not dict or set(value) != required:
        raise ValueError(f"{label}: fields must be {', '.join(sorted(required))}")


def _strings(value, label, *, minimum=0, maximum=32, max_length=200):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label}: unexpected item count")
    for item in value:
        bounded_text(item, label, max_length)
    if len(set(value)) != len(value):
        raise ValueError(f"{label}: duplicate items")


def record_review_hash(record):
    """Bind a local review to all content/routing fields, excluding review itself."""
    if type(record) is not dict:
        raise ValueError("Record required")
    return sha256(canonical_json({k: v for k, v in record.items() if k != "review"})).hexdigest()


def _query(value):
    return "".join(c.casefold() for c in value if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class KnowledgeBase:
    _allowed_modes = frozenset({"demo"})

    def __init__(self, config: dict, *, today: date | None = None):
        if today is not None and type(today) is not date:
            raise ValueError("today must be datetime.date")
        self._date = today
        _keys(config, TOP, "knowledge configuration")
        if config["schema_version"] != SCHEMA or config["mode"] not in self._allowed_modes:
            raise ValueError("Knowledge mode is not supported by this reader")
        for field in ("knowledge_base_id", "name", "tenant_id", "shop_id", "version"):
            _id(config[field], field)
        if type(config["records"]) is not list or not 1 <= len(config["records"]) <= 200:
            raise ValueError("Knowledge base must have 1..200 records")
        ids, skus = set(), set()
        for record in config["records"]:
            self._validate_record(record)
            if record["id"] in ids:
                raise ValueError("Duplicate record id")
            ids.add(record["id"])
            if record["kind"] == "product":
                sku = record["sku"].casefold()
                if sku in skus:
                    raise ValueError("Duplicate product SKU; keep history in separate versioned files")
                skus.add(sku)
        encoded = canonical_json(config)
        if len(encoded) > MAX_BYTES:
            raise ValueError("Configuration exceeds 1 MiB")
        self._config = deepcopy(config)
        self.config_hash = sha256(encoded).hexdigest()

    @property
    def today(self):
        return self._date or local_today()

    def export(self):
        return deepcopy(self._config)

    def _validate_record(self, record):
        if type(record) is not dict or type(record.get("kind")) is not str or record["kind"] not in KINDS:
            raise ValueError("Unsupported knowledge kind")
        _keys(record, COMMON | (PRODUCT if record["kind"] == "product" else ANSWER), "record")
        for field in ("id", "version", "title"):
            _id(record[field], field)
        if type(record["status"]) is not str or record["status"] not in {"draft", "approved", "retired"}:
            raise ValueError("Invalid record status")
        _strings(record["channels"], "channels", minimum=1, maximum=6)
        if set(record["channels"]) - CHANNELS:
            raise ValueError("Unsupported channel in knowledge record")
        start, end = _day(record["valid_from"], "valid_from"), _day(record["valid_until"], "valid_until")
        if start is not None and end is not None and start >= end:
            raise ValueError("valid_until must be later than valid_from")
        source = record["source"]
        _keys(source, {"title", "reference", "content"}, "source")
        bounded_text(source["title"], "source.title", 200)
        bounded_text(source["reference"], "source.reference", 500)
        bounded_text(source["content"], "source.content", 12000)
        if record["kind"] == "product":
            _id(record["sku"], "sku")
            _id(record["name"], "name")
            _strings(record["aliases"], "aliases", max_length=128)
            facts = record["facts"]
            if type(facts) is not dict or not 1 <= len(facts) <= 24:
                raise ValueError("Product requires 1..24 facts")
            if record["name"] not in source["content"]:
                raise ValueError("Product name must be present in source.content")
            for key, value in facts.items():
                bounded_text(key, "fact name", 40)
                bounded_text(value, "fact value", 300)
                if value not in source["content"]:
                    raise ValueError("Every fact value must be present in source.content")
            if sum(len(k) + len(v) + 2 for k, v in facts.items()) > 6500:
                raise ValueError("Product facts exceed a safe reply size")
        else:
            _strings(record["questions"], "questions", minimum=1, maximum=20, max_length=500)
            _strings(record["keywords"], "keywords", maximum=12, max_length=40)
            if any(len(_query(keyword)) < 2 for keyword in record["keywords"]):
                raise ValueError("Keywords need at least two non-punctuation characters")
            bounded_text(record["answer"], "answer", 2000)
            if record["answer"] not in source["content"]:
                raise ValueError("Curated answer must be present in source.content")
        review = record["review"]
        _keys(review, {"by", "at", "record_hash"}, "review")
        review_date = _day(review["at"], "review.at")
        if type(review["by"]) is not str or len(review["by"]) > 128:
            raise ValueError("Invalid review.by")
        if review["record_hash"] is not None and (type(review["record_hash"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", review["record_hash"])):
            raise ValueError("Invalid review.record_hash")
        if record["status"] == "draft" and review != {"by": "", "at": None, "record_hash": None}:
            raise ValueError("Draft review fields must be cleared")
        if record["status"] == "approved":
            answer_content = (record["name"] + "\n" + "\n".join(record["facts"].values())
                              if record["kind"] == "product" else record["answer"])
            if "【待填写】" in answer_content:
                raise ValueError("Replace template placeholders before local review")
            _id(review["by"], "review.by")
            if review_date is None or review_date > self.today:
                raise ValueError("Approved record requires a non-future review date")
            if review["record_hash"] != record_review_hash(record):
                raise ValueError("Reviewed record changed; return to draft and review again")

    @classmethod
    def from_path(cls, path, *, today=None):
        path = Path(path).absolute()
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError("Knowledge file must not traverse linked paths")
        if path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("Expected a regular JSON file no larger than 1 MiB")
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Knowledge file exceeds size limit")
        try:
            config = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            return cls(config, today=today)
        except (UnicodeError, RecursionError, TypeError) as exc:
            raise ValueError("Invalid bounded knowledge configuration") from exc

    def _scoped(self, context):
        return (type(context) is dict and context.get("tenant_id") == self._config["tenant_id"]
                and context.get("shop_id") == self._config["shop_id"] and context.get("channel") in CHANNELS)

    def _active(self, context):
        if not self._scoped(context):
            return []
        return [record for record in self._config["records"]
                if record["status"] == "approved" and context["channel"] in record["channels"]
                and (record["valid_from"] is None or _day(record["valid_from"], "valid_from") <= self.today)
                and (record["valid_until"] is None or self.today < _day(record["valid_until"], "valid_until"))]

    def _source(self, record):
        source = {"id": f"kb:{self._config['knowledge_base_id']}:{record['id']}",
                  "title": record["source"]["title"], "kind": "knowledge",
                  "tenant_id": self._config["tenant_id"], "shop_id": self._config["shop_id"],
                  "category": record["kind"], "record_id": record["id"],
                  "knowledge_base_id": self._config["knowledge_base_id"],
                  "source_location": record["source"]["reference"] + "#record=" + record["id"],
                  "version": record["version"], "content_hash": sha256(record["source"]["content"].encode("utf-8")).hexdigest(),
                  "evidence_scope": {"access": "unknown", "coverage": "record", "version_status": "pinned",
                                     "limitations": ["local_demo_configuration_only", "review_hash_is_not_authenticated_human_approval",
                                                     "source_authenticity_and_engine_access_not_verified"]}}
        if record["kind"] == "product":
            source["sku"] = record["sku"]
        return source

    def resolve_source_evidence(self, source, context):
        for record in self._active(context):
            verified = self._source(record)
            if source.get("id") == verified["id"]:
                if any(source.get(key) != verified[key] for key in ("tenant_id", "shop_id", "kind")):
                    raise ValueError("Citation scope mismatch")
                return deepcopy(verified)
        return None

    def catalog(self, context):
        result = []
        for record in self._active(context):
            if record["kind"] == "product":
                source = self._source(record)
                result.append({"tenant_id": source["tenant_id"], "shop_id": source["shop_id"],
                               "sku": record["sku"], "name": record["name"], "aliases": deepcopy(record["aliases"]),
                               "facts": deepcopy(record["facts"]), "source_id": source["id"], "source_title": source["title"]})
        return result

    def model_evidence(self, context, text, *, limit=6):
        """Bounded local retrieval for a candidate model request, not semantic RAG.

        Expose only curated answer/fact text, not source instructions or draft
        policies. A host still has to authorize release to its chosen provider.
        A short catalog is offered on vague requests; never imply completeness.
        """
        bounded_text(text, "retrieval text", 24016)
        if type(limit) is not int or not 1 <= limit <= 6:
            raise ValueError("Evidence limit must be 1..6")
        query = _query(text)
        ranked = []
        for record in self._active(context):
            terms = ([record["sku"], record["name"], *record["aliases"]]
                     if record["kind"] == "product" else [*record["questions"], *record["keywords"]])
            score = sum(1 for term in terms if _query(term) in query)
            if score or record["kind"] == "product":
                content = (record["name"] + "\n" + "\n".join(f"{k}：{v}" for k, v in record["facts"].items())
                           if record["kind"] == "product" else record["answer"])
                ranked.append((score, record["id"], {"source": self._source(record), "content": content}))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        # Keep a strict character budget for a single model turn.
        result, size = [], 0
        for _, _, item in ranked:
            if len(result) >= limit or size + len(item["content"]) > 14000:
                continue
            result.append(item)
            size += len(item["content"])
        return deepcopy(result)

    def lookup(self, context, text):
        if not self._scoped(context):
            return None
        bounded_text(text, "query", MAX_INBOUND_TEXT_CHARS)
        query = _query(text)
        if not query:
            return None
        business = BusinessAssistant(self.catalog(context), [])
        if any(term in text for term in ("破损", "坏了", "漏发", "少件", "投诉")):
            return business.reply(context, text)
        if business._match_product(context, text) is not None:
            result = business.reply(context, text)
            if result["intent"] == "product":
                result["sources"] = [self.resolve_source_evidence(source, context) for source in result["sources"]]
                return result
            if result["intent"] in {"identity_required", "after_sales", "order_status"}:
                return result
        candidates = self._curated_candidates(context, text)
        if len(candidates) > 1:
            return _response("当前资料存在多个适用答案，需要人工核对后回复。", "unknown", requires_human=True)
        if not candidates:
            return None
        record = candidates[0]
        return _response(record["answer"], "knowledge", [self._source(record)])

    def has_answer_conflict(self, context, text):
        """Known deterministic overlap only; not a semantic contradiction test."""
        bounded_text(text, "query", MAX_INBOUND_TEXT_CHARS)
        return len(self._curated_candidates(context, text)) > 1

    def _curated_candidates(self, context, text):
        query = _query(text)
        candidates = []
        for record in self._active(context):
            if record["kind"] == "product":
                continue
            exact = query in {_query(question) for question in record["questions"]}
            spans = []
            for keyword in sorted(record["keywords"], key=len, reverse=True):
                key = _query(keyword)
                index = query.find(key)
                if index >= 0 and all(index + len(key) <= a or index >= b for a, b in spans):
                    spans.append((index, index + len(key)))
            if exact or len(spans) >= 2:
                candidates.append(record)
        return candidates

    def summary(self):
        return {"knowledge_base_id": self._config["knowledge_base_id"], "name": self._config["name"],
                "version": self._config["version"], "config_hash": self.config_hash,
                "mode": "demo", "record_count": len(self._config["records"]),
                "by_status": {status: sum(r["status"] == status for r in self._config["records"])
                              for status in ("draft", "approved", "retired")}}


def approve_record(config, record_id, reviewer, *, today=None):
    """Explicit LOCAL review of a candidate; returns a new value, never writes."""
    _id(reviewer, "reviewer")
    current = today or local_today()
    if type(current) is not date:
        raise ValueError("today must be datetime.date")
    result = deepcopy(config)
    matches = [r for r in result.get("records", []) if r.get("id") == record_id]
    if len(matches) != 1 or matches[0].get("status") == "retired":
        raise ValueError("Select one non-retired record")
    record = matches[0]
    record["status"] = "approved"
    record["review"] = {"by": reviewer, "at": current.isoformat(), "record_hash": record_review_hash(record)}
    KnowledgeBase(result, today=current)
    return result


class KnowledgeAssistant:
    """Configured facts + curated FAQ/policy answers, with the existing order gate.

    This remains deterministic retrieval; no LLM, embeddings or semantic quality
    claim. It never executes document instructions or performs order writes.
    """
    def __init__(self, knowledge: KnowledgeBase, orders: list):
        if not isinstance(knowledge, KnowledgeBase):
            raise ValueError("KnowledgeBase required")
        self.knowledge = knowledge
        # Keep order validation and isolation exactly in the existing processor.
        BusinessAssistant([], orders)
        self._orders = deepcopy(orders)

    def resolve_source_evidence(self, source, context):
        return self.knowledge.resolve_source_evidence(source, context)

    def reply(self, context, text):
        if not self.knowledge._scoped(context):
            return _response("无法确认当前店铺与渠道可用的知识，请转人工核对。", "identity_required", requires_human=True)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INBOUND_TEXT_CHARS:
            return _response("请提供 2000 字以内的具体问题。", "unknown", requires_human=True)
        business = BusinessAssistant(self.knowledge.catalog(context), self._orders)
        # Actual order requests or requested actions must never be answered by a
        # public policy match that happens to share keywords.
        order_ids = business._order_ids(text)
        explicit_action = any(term in text for term in ("帮我退款", "我要退款", "申请退款", "给我退款", "处理退款", "请退款",
                                                       "申请退货", "我要退货", "帮我退货", "申请换货", "我要换货", "帮我换货",
                                                       "取消订单", "撤销订单", "改地址", "修改地址", "更改地址", "地址写错"))
        product = business._match_product(context, text)
        # A SKU can resemble an order ID; known product references are handled
        # as products unless the message explicitly names an order or an action.
        concrete_issue = any(term in text for term in ("破损", "坏了", "漏发", "少件", "投诉"))
        if concrete_issue or explicit_action or "订单号" in text or (order_ids and (product is None or "订单" in text)):
            return business.reply(context, text)
        if product is not None:
            result = business.reply(context, text)
            if result["intent"] == "product":
                return self._enrich(result, context)
            if result["intent"] in {"identity_required", "after_sales", "order_status"}:
                return self._enrich(result, context)
        curated = self.knowledge.lookup(context, text)
        if curated is not None:
            return curated
        if any(term in text for term in ("退货规则", "退换货政策", "退款政策", "售后政策", "售后规则")):
            return _response("当前没有已审核且有效的适用政策，请转人工核对。", "unknown", requires_human=True)
        result = business.reply(context, text)
        if result["intent"] == "unknown":
            return _response("目前没有找到已审核、有效且适用于当前渠道的明确答案，请转人工确认。", "unknown", requires_human=True)
        return self._enrich(result, context)

    def _enrich(self, result, context):
        for i, source in enumerate(result["sources"]):
            evidence = self.resolve_source_evidence(source, context)
            if evidence is not None:
                result["sources"][i] = evidence
        return result
