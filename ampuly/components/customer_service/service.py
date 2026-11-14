"""Host-only draft session. No registration, database open, approval or send.

The Engine supplies a scoped knowledge snapshot, channel Context, its selected
provider, and a live pre-dispatch policy/Stop check. These constructor inputs
are not permissions and MUST NOT be copied from a model/customer wire payload.
Keep one session per conversation; a timed-out provider retains its slot until
it has actually stopped. A new session is not a way to retry an uncertain call.
"""
from __future__ import annotations

from copy import deepcopy

from .core.contracts import Context
from .core.conversation import ConversationService
from .core.knowledge import KnowledgeBase
from .model_adapter import CustomerServiceModelError, EngineModelClient


class CustomerServiceDraftSession:
    def __init__(self, *, context: Context, knowledge: KnowledgeBase, provider,
                 before_dispatch, task_type: str = "customer_service", system_prompt: str | None = None,
                 max_output_tokens: int = 3500, revision_note: str = "",
                 structured_understanding: bool = False):
        if type(context) is not Context or not isinstance(knowledge, KnowledgeBase):
            raise ValueError("Trusted Context and scoped KnowledgeBase required")
        if not callable(before_dispatch):
            raise ValueError("Host policy/Stop check required")
        self._context = context
        self._knowledge = knowledge
        self._provider = provider
        self._before_dispatch = before_dispatch
        self._task_type = task_type
        # Orders are deliberately not accepted here: the lab's fixtures and
        # verified_customer flag are not a real Engine order authorization.
        self._conversation = ConversationService(knowledge, None, system_prompt=system_prompt,
                                                structured_understanding=structured_understanding)
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 3500:
            raise ValueError("Invalid host output cap")
        self._max_output_tokens = max_output_tokens
        if type(revision_note) is not str or len(revision_note) > 4000:
            raise ValueError("Invalid original review feedback")
        self._revision_note = revision_note
        self._adapter = None
        self._running = False

    @property
    def model_observation(self):
        """Detached host telemetry, not an authorization or settlement receipt."""
        return None if self._adapter is None else self._adapter.observation

    async def prepare(self, turns, *, signals=None, timeout_seconds=30) -> dict:
        """Draft once under the caller's existing Engine model-budget context.

        No arbitrary model, identity, customer-verification flag, order, budget,
        tool, connector or recipient can be supplied to this method. The host
        scopes history and signals, persists artifacts, and owns human review.
        Structural acceptance remains review_required=true, never auto-send.
        """
        pending = self._conversation._pending_model_task
        if self._running or pending is not None and not pending.done():
            raise CustomerServiceModelError("conversation_model_busy")
        self._running = True
        try:
            self._adapter = EngineModelClient(
                self._provider, task_type=self._task_type,
                before_dispatch=self._before_dispatch,
                max_output_tokens=self._max_output_tokens,
                revision_note=self._revision_note,
            )
            self._conversation.client = self._adapter
            result = await self._conversation.prepare_async(
                self._context, turns, signals=signals, timeout_seconds=timeout_seconds,
                verified_customer=False, active_order_id=None,
            )
            # Core deliberately catches provider failures to build a safe
            # fallback. Host policy/Stop/budget denials must still end the run.
            self._adapter.raise_governance_failure()
            return deepcopy(result)
        finally:
            self._running = False
