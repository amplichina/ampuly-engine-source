"""Retired CRM execution path. Execution belongs to existing workflow nodes.

Kept only to refuse stale/internal callers explicitly, never create Task or Run.
"""
from .contracts import fail


class CRMService:
    def __init__(self, *args, **kwargs):
        pass

    async def suggest(self, **kwargs):
        fail("workflow_path_retired")

    def review(self, **kwargs):
        fail("workflow_path_retired")

    async def handoff(self, **kwargs):
        fail("workflow_path_retired")
