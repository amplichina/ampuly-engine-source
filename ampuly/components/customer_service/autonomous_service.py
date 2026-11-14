"""Admitted service orchestration, using the existing Engine model and outbox.

This is a host-only composition, not a public RPC or an enabled channel. The
authenticated channel supplies scoped history; Engine supplies its provider,
model-budget scope and live Stop/permission check. No model/customer payload can
choose the account, grant, sender or model. Normal admitted replies do not call
human approve. Exception handoff and uncertain delivery are distinct outcomes.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import inspect

from .core.autonomous_runtime import AutonomousRuntime
from .core.contracts import Context, canonical_json
from .core.conversation import _turns
from .core.knowledge import KnowledgeBase
from .model_adapter import CustomerServiceModelError
from .service import CustomerServiceDraftSession


class CustomerServiceAutonomousSession:
    """One retained session per trusted conversation; no automatic retry loop.

    A durable generation claim also prevents a second session/process restart
    from reissuing an uncertain paid model call. Recovery of such a claim is a
    separate explicit host operation, not achieved by constructing this class.
    The caller must enforce its channel transport deadline. The awaited outbox
    does not invent delivery proof or a second execution engine. Real channels
    provide asynchronous transport methods and recheck the final async fence.
    """

    def __init__(self, *, runtime: AutonomousRuntime, admission_id: str,
                 context: Context, knowledge: KnowledgeBase, provider,
                 before_dispatch, system_prompt=None, max_output_tokens=3500):
        if (type(runtime) is not AutonomousRuntime or type(context) is not Context
                or not isinstance(knowledge, KnowledgeBase)
                or not callable(before_dispatch)):
            raise ValueError("autonomous_host_binding_required")
        self._runtime = runtime
        self._admission_id = admission_id
        self._context = context
        self._knowledge = knowledge
        self._host_check = before_dispatch
        self._active = None
        self._running = False
        self._draft = CustomerServiceDraftSession(
            context=context, knowledge=knowledge, provider=provider,
            before_dispatch=self._before_model, system_prompt=system_prompt,
            max_output_tokens=max_output_tokens, structured_understanding=True,
        )

    @property
    def model_observation(self):
        return self._draft.model_observation

    def _check_live(self):
        admission = self._runtime.preflight(self._admission_id, self._context)
        # The provider's evidence must be this very frozen knowledge snapshot,
        # not a grant whose hash was copied onto an unrelated KnowledgeBase.
        actual_hash = sha256(canonical_json(self._knowledge.export())).hexdigest()
        if (self._knowledge.config_hash != actual_hash
                or actual_hash != admission.knowledge_sha256):
            raise ValueError("autonomous_knowledge_snapshot_changed")
        if self._active is not None:
            self._runtime.assert_generation_current(self._context, *self._active)
        return admission

    async def _before_model(self):
        self._check_live()
        checked = self._host_check()
        if inspect.isawaitable(checked):
            checked = await checked
        if checked is not None:
            raise CustomerServiceModelError("autonomous_host_check_denied")
        # Recheck after an awaited host decision, before the original transport.
        self._check_live()

    async def handle(self, turns, *, adapter, signals=None, timeout_seconds=30):
        """Handle one authenticated inbound event under a bound Engine budget.

        ``turns`` is selected by the channel's trusted host, not arbitrary client
        history. Returned draft/handoff data is private host data, never sent as
        a channel message; only the immutable command.text reaches the adapter.
        A receipt with unknown outcome is reconciled, never blindly retried.
        """
        if self._running:
            raise CustomerServiceModelError("conversation_model_busy")
        from ampuly.llm.budget import budget_is_bound, ModelBudgetStop
        if not budget_is_bound():
            raise ModelBudgetStop("customer_service_model_budget_unbound")
        self._running = True
        try:
            history = _turns(deepcopy(turns))
            last = history[-1]
            history_hash = sha256(canonical_json(history)).hexdigest()
            self._check_live()
            accepted = self._runtime.receive(self._context, last["id"], last["text"])
            revision = accepted["revision"]
            if not self._runtime.claim_generation(self._context, last["id"], revision, history_hash):
                return {"status": "duplicate", "action": "none", "command": None,
                        "reason_codes": ["generation_already_claimed"]}
            self._active = (last["id"], revision, history_hash)
            try:
                await self._before_model()
            except BaseException:
                # No model method has been called at this point. Release only
                # the generation-in-flight latch, never erase its dedupe key.
                self._runtime.mark_generation_finished(self._context, *self._active)
                raise
            candidate = await self._draft.prepare(history, signals=signals, timeout_seconds=timeout_seconds)
            if (candidate["model_calls"] == 0 or self.model_observation is not None
                    or not candidate["validation_errors"]):
                self._runtime.mark_generation_finished(self._context, *self._active)
            self._check_live()
            command = self._runtime.propose(self._context, last["id"], candidate["draft"]["reply"], revision)
            decision = self._runtime.authorize_candidate(
                command["command_id"], admission_id=self._admission_id,
                candidate=candidate, expected_history_hash=history_hash,
            )
            if decision.action == "handoff":
                # Persist ownership change so subsequent messages do not keep
                # triggering model calls while the exception awaits a person.
                self._runtime.takeover(self._context)
                command = self._runtime.get_command(command["command_id"])
                status = "waiting_exception_handler"
            elif decision.action == "hold":
                status = "held"
            else:
                command = await self._runtime.dispatch_async(command["command_id"], adapter)
                status = command["status"]
            return {"status": status, "action": decision.action, "command": command,
                    "reason_codes": list(decision.reason_codes), "candidate": candidate,
                    "model_observation": self.model_observation}
        finally:
            self._active = None
            self._running = False
