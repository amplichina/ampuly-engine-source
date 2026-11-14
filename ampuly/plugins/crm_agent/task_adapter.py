"""Retired draft adapter: description text is not a workflow binding."""
from .contracts import fail


class PendingTaskAdapter:
    def __init__(self, *args, **kwargs):
        pass

    async def create(self, *args, **kwargs):
        fail("workflow_path_retired")

    async def lookup(self, *args, **kwargs):
        fail("workflow_path_retired")
