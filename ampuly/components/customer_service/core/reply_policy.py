"""Eligibility for exact, static public replies; never a sending authorization.

This leaf reads the local knowledge snapshot only. A trusted host must separately
authenticate its current actor/channel, approve the knowledge version, reserve
budget, check stop/revocation, and authorize any external effect. Local review
hashes, PULY decisions and customer identity booleans do not satisfy those checks.
Free model prose always stays in review mode; no regex claims semantic safety.
"""
from copy import deepcopy
from hashlib import sha256
import re

from .contracts import Context, bounded_text, canonical_json
from .conversation import _gates, _signals, _turns
from .knowledge import KnowledgeBase, _query


_STATIC_FACTS = frozenset({"材质", "尺寸", "容量", "颜色"})
# A supplementary exclusion for the narrow static mode, not a language verifier.
_REVIEW_TOPICS = ("退款", "退货", "换货", "赔偿", "赔付", "补发", "订单", "物流", "发货", "送达", "到货",
                  "库存", "价格", "售价", "优惠", "折扣", "促销", "投诉", "维权", "改址", "地址", "承重",
                  "用药", "诊断", "保证", "承诺", "保修", "质保", "起火", "触电", "受伤", "漏电")


class PublicReplyPolicy:
    """Select a bounded static reply and optionally verify an exact candidate.

    ``eligible`` means candidate selection succeeded. It never means knowledge
    approval or channel sending is authorized. Keep ``review_required`` until
    the host adopts the static mode under its own real approval contract.
    """
    def __init__(self, knowledge: KnowledgeBase):
        if type(knowledge) is not KnowledgeBase:
            raise ValueError("Exact KnowledgeBase required")
        self.knowledge = knowledge

    def evaluate(self, context: Context, text, candidate=None, *, signals=None, history=None):
        if type(context) is not Context:
            raise ValueError("Trusted Context required")
        bounded_text(text, "public reply query", 2000)
        signals = _signals(signals)
        turns = ([{"id": "current", "role": "customer", "text": text}]
                 if history is None else _turns(history))
        if turns[-1]["text"] != text:
            raise ValueError("Current query must match latest scoped turn")
        scope = {"tenant_id": context.tenant_id, "shop_id": context.shop_id, "channel": context.channel}
        reasons = _gates(turns, signals)
        if not self.knowledge._scoped(scope):
            reasons.append("scope_denied")
        if candidate is not None and (type(candidate) is not dict or candidate.get("mode") != "deterministic-demo"):
            reasons.append("model_text_requires_review")
        if reasons:
            return self._result(context, turns, None, reasons)
        query = _query(text)
        matches = []
        for record in self.knowledge._active(scope):
            # Do not reuse fuzzy keyword matching for automatic handling.
            if record["kind"] == "faq" and query in {_query(q) for q in record["questions"]}:
                if any(term in text or term in record["answer"] for term in _REVIEW_TOPICS):
                    matches.append((None, "sensitive_topic_requires_review"))
                else:
                    matches.append((self._faq_reply(record), None))
            elif record["kind"] == "product":
                field = self._exact_product_field(record, query)
                if field is not None:
                    if field not in record["facts"]:
                        matches.append((None, "static_fact_missing"))
                    else:
                        value = record["facts"][field]
                        if any(term in value for term in _REVIEW_TOPICS):
                            matches.append((None, "sensitive_topic_requires_review"))
                        else:
                            matches.append((self._product_reply(record, field), None))
        if len(matches) != 1:
            return self._result(context, turns, None,
                                ["ambiguous_static_answer" if len(matches) > 1 else "exact_static_answer_missing"])
        expected, reason = matches[0]
        if reason:
            return self._result(context, turns, None, [reason])
        # Compare the whole bounded deterministic result, not just its citations.
        # verified_customer and PULY output are intentionally not accepted inputs.
        if candidate is not None and candidate != expected:
            return self._result(context, turns, None, ["static_candidate_mismatch"])
        for source in expected["sources"]:
            if self.knowledge.resolve_source_evidence(source, scope) != source:
                return self._result(context, turns, None, ["static_source_changed"])
        return self._result(context, turns, expected, [])

    @staticmethod
    def _exact_product_field(record, query):
        # Full-string grammar: one known identifier + one static fact question.
        # Extra intents/instructions and vague recommendations are not accepted.
        identifiers = (record["sku"], record["name"], *record["aliases"])
        for identifier in identifiers:
            escaped = re.escape(_query(identifier))
            for field in _STATIC_FACTS:
                pattern = rf"(?:请问|查询)?{escaped}(?:的)?{field}(?:是多少|是什么|有哪些|什么|多少)?"
                if re.fullmatch(pattern, query):
                    return field
        return None

    def _faq_reply(self, record):
        return {"text": record["answer"], "intent": "knowledge", "sources": [self.knowledge._source(record)],
                "requires_human": False, "mode": "deterministic-demo"}

    def _product_reply(self, record, field):
        source = self.knowledge._source(record)
        return {"text": f"{record['sku']}「{record['name']}」\n{field}：{record['facts'][field]}\n知识来源：[{source['id']}]",
                "intent": "product", "sources": [source], "requires_human": False, "mode": "deterministic-demo"}

    def _result(self, context, turns, reply, reasons):
        return {"kind": "ampuly.cs.public-reply-eligibility/1", "eligible": reply is not None and not reasons,
                "eligibility": "eligible_candidate" if reply is not None and not reasons else "review_required",
                "reasons": list(dict.fromkeys(reasons)), "reply": deepcopy(reply), "context_key": context.key,
                "knowledge_config_hash": self.knowledge.config_hash,
                "history_hash": sha256(canonical_json(turns)).hexdigest(), "policy_version": "static-public/1",
                "review_required": True, "sending_authorized": False, "effects_produced": [],
                "approval_status": "host_approval_required", "semantic_quality_verified": False}
