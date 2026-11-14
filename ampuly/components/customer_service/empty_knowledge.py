"""Explicit zero-record scope for internal synthetic trials.

This is not a customer knowledge configuration and cannot be loaded through
KnowledgeBase.from_path. All inherited lookup methods see an empty record set;
no source, product, private rule or authenticated buyer is manufactured.
"""
from hashlib import sha256

from .core.contracts import Context, canonical_json
from .core.knowledge import KnowledgeBase


class EmptyKnowledgeBase(KnowledgeBase):
    def __init__(self, context: Context):
        if type(context) is not Context or context.channel != "console":
            raise ValueError("Empty knowledge requires a host-created internal console scope")
        self._date = None
        self._config = {"schema_version": "ampuly.cs.empty-knowledge/1",
            "knowledge_base_id": "empty-" + context.key[:24],
            "name": "明确未提供知识；不允许补造企业事实",
            "tenant_id": context.tenant_id, "shop_id": context.shop_id,
            "version": "empty-v1", "mode": "no_knowledge", "records": []}
        self.config_hash = sha256(canonical_json(self._config)).hexdigest()

    def summary(self):
        return {**super().summary(), "mode": "no_knowledge",
                "source": "host_explicit_empty_scope", "record_count": 0}
