"""Explicit owner-adopted, immutable knowledge snapshot for service acceptance.

This reader is not a grant. The console authenticates the owner and scopes the
snapshot; the full exported bytes are bound into both AI and human acceptance.
Source references remain labels, never fetched URLs or executable instructions.
Legacy demo files continue to use KnowledgeBase unchanged.
"""
from .core.knowledge import KnowledgeBase


class ServiceKnowledge(KnowledgeBase):
    _allowed_modes = frozenset({"service_snapshot"})

    def summary(self):
        result = super().summary()
        result["mode"] = "service_snapshot"
        return result

    def _source(self, record):
        source = super()._source(record)
        source["evidence_scope"] = {
            "access": "owner_supplied_snapshot", "coverage": "record", "version_status": "pinned",
            "limitations": ["source_reference_is_not_fetched",
                            "record_review_hash_is_not_service_admission",
                            "source_truth_requires_owner_acceptance"],
        }
        return source
