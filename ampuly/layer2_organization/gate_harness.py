"""LLM-judged gate harness for company-mode work items.

This module used to encode ~700 lines of hard-coded "if blocker_type ==
X then action = Y" rules. That approach kept misclassifying real
deliveries (e.g. reading `ready_for_final_release: false` as a hard
release blocker even when the same report explicitly said
`ready_for_downstream_execution: true`) and could not generalize across
the variety of user tasks the runtime is supposed to handle.

The current design is intentionally minimal:

1. Build a *descriptive* evidence packet for the completed work item. The
   packet only describes what happened — it makes no policy decisions.
   It contains: the work item's original requirements, the upstream context
   the agent saw, the agent's actual output (result + summary + artifact
   index + work_item_summary_for_downstream), the agent's own self-reported risks and
   blockers, and a small number of cheap objective signals (task status,
   dependency health, prior rework history).

2. Hand the packet to an LLM judge with a tightly scoped prompt. The
   judge compares "what was asked" vs "what was produced" and returns
   one of exactly three actions: `pass`, `rework_same_work_item`, or
   `escalate`. The reason text is fed back to the original agent
   session via `gate_harness_rework_feedback` so the same agent can fix
   it on the next turn.

3. Apply a single safety net: a stagnation cap. If the same blocker
   fingerprint has caused N reworks in a row without converging,
   upgrade to `escalate` so the user gets a chance to break the loop.
   This is the only deterministic decision the harness still makes.

When nobody can judge -- the judge fails, answers with nothing usable, or there is no judge at all -- evaluate()
returns `escalate` with source `judge_unavailable`: the finished work is kept and a person decides. A gate nobody
judged is never a pass (model-routing phase 0; it used to trust the agent's self-report).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ampuly.core.config import GateHarnessPolicyConfig
from ampuly.core.models import Task
from ampuly.layer2_organization.work_item_identity import projection_id_for_task
from ampuly.layer6_observability.metering import meter_for_task
from ampuly.llm.budget import ModelBudgetStop, RouteRefused
from ampuly.layer2_organization.role_prompt_json import parse_role_prompt_json_value
from ampuly.llm.provider import ProviderQuotaExhaustedError
from ampuly.llm.retry import LLMOutcomeUnknown, LLMRetryError, call_llm_json_with_retry

logger = logging.getLogger(__name__)


# Callback signature for spawning a fresh judge session.
#
# The runner is expected to:
#   1. Inspect `source_task.assigned_external_agent` (or fall back to a
#      native model) to pick the SAME agent type that produced the
#      work item being judged. The point is consistency — codex-produced
#      work is judged by codex, claude_code-produced work by
#      claude_code, native-produced work by the native LLM.
#   2. Open a NEW session for the judge (not a resume of the source
#      agent's session) so the judge starts with a clean context and
#      cannot be biased by the agent's prior reasoning trace.
#   3. Send `system_prompt` as the system message and the JSON-encoded
#      packet as the user message.
#   4. Return the raw text the judge produced.
#
# gate_harness then parses that text as JSON (tolerating the runtime's
# ``Verification:`` footer). Anything that does not parse into a known
# action escalates to a person as ``judge_unavailable``.
JudgeRunner = Callable[
    ["GateEvidencePacket", str, Task],
    Awaitable[str],
]


GATE_HARNESS_AGENT_PROMPT = """\
You are the gate-harness judge for one completed work item of an AI company runtime.

You receive an evidence packet describing what the work item was asked
to produce, what context the agent had, and what the agent actually
produced. Decide whether the output meets the work item's requirements.

Return STRICT JSON with exactly these two fields and no extras:
{
  "action": "pass" | "rework_same_work_item" | "escalate",
  "reason": "<your explanation; if action is rework_same_work_item, include the concrete actionable steps the agent should take>"
}

Action semantics:
- `pass` — the deliverables meet the stated requirements.
- `rework_same_work_item` — the same agent can fix the gap; your `reason`
  must spell out what is wrong and what to do about it.
- `escalate` — the gap needs a human decision, OR the same problem
  has already caused multiple unsuccessful reworks.

Return JSON only — no markdown fences, no commentary.
"""


@dataclass
class GateEvidencePacket:
    """Minimal snapshot fed to the LLM judge: 5 fields only.

    - requirements: what the work item was asked to produce (task.title +
      task.description — the "task brief" that was given to the agent)
    - output: what the agent actually produced (task.result raw stdout)
    - prior_rework_count / prior_rework_feedback: history of previous
      gate judge rework cycles for this work item
    """

    projection_id: str
    requirements: str
    output: str
    prior_rework_count: int = 0
    prior_rework_feedback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "requirements": self.requirements,
            "output": self.output,
            "prior_rework_count": self.prior_rework_count,
            "prior_rework_feedback": self.prior_rework_feedback,
        }


@dataclass
class GateHarnessDecision:
    """Projection-only decision returned to company_mode."""

    action: str
    summary: str
    target_projection_id: str = ""
    target_projection_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    blocker_types: list[str] = field(default_factory=list)
    residual_risks: list[str] = field(default_factory=list)
    source: str = "llm_judge"
    # Which judge produced this. "provider_session" means a fresh session
    # of the same provider that produced the work item looked at it;
    # "native_fallback" means nobody wired that dispatch and the native
    # model judged from the evidence packet instead. The two are not the
    # same claim about a delivery, and they used to be the same record.
    judged_by: str = ""
    blocker_fingerprint: str = ""

    def __post_init__(self) -> None:
        projection_id = str(self.target_projection_id or "").strip()
        projection_ids = [
            str(item).strip()
            for item in list(self.target_projection_ids or [])
            if str(item).strip()
        ]
        if not projection_ids and projection_id:
            projection_ids = [projection_id]
        self.target_projection_id = projection_id or (projection_ids[0] if projection_ids else "")
        self.target_projection_ids = projection_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "summary": self.summary,
            "target_projection_id": self.target_projection_id,
            "target_projection_ids": list(self.target_projection_ids),
            "notes": list(self.notes),
            "constraints": list(self.constraints),
            "blockers": list(self.blockers),
            "blocker_types": list(self.blocker_types),
            "residual_risks": list(self.residual_risks),
            "source": self.source,
            "judged_by": self.judged_by,
            "blocker_fingerprint": self.blocker_fingerprint,
        }


class GateHarness:
    """Evaluates one completed work item using an LLM judge."""

    ALLOWED_ACTIONS = ("pass", "rework_same_work_item", "escalate")

    def __init__(
        self,
        *,
        policy: GateHarnessPolicyConfig | dict[str, Any] | None = None,
        llm: Any | None = None,
        org_engine: Any | None = None,
        judge_runner: JudgeRunner | None = None,
        cost_tracker: Any | None = None,
    ) -> None:
        # Judging a work item is a paid call made on that work item's behalf.
        # Optional: a harness built without one judges unmetered, as before.
        self.cost_tracker = cost_tracker
        if isinstance(policy, GateHarnessPolicyConfig):
            self.policy = policy
        else:
            self.policy = GateHarnessPolicyConfig.model_validate(dict(policy or {}))
        self.llm = llm
        self.org_engine = org_engine
        # Preferred path: company_mode wires a runner that spawns a
        # fresh session of the SAME external agent type that produced
        # the work item being judged (codex → codex, claude_code →
        # claude_code, native → native llm). When `judge_runner` is
        # set, `_invoke_judge` uses it. Otherwise we fall back to
        # `self.llm.simple_chat` so the harness still works in tests
        # and in setups that have not yet wired the per-agent dispatch.
        self.judge_runner = judge_runner

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> tuple[GateEvidencePacket, GateHarnessDecision]:
        packet = self.build_evidence_packet(task, task_by_projection_id)
        decision = await self._invoke_judge(packet, source_task=task)
        decision = self._apply_stagnation_cap(task, decision)
        return packet, decision

    # ------------------------------------------------------------------
    # Evidence packet construction (Class B: descriptive only)
    # ------------------------------------------------------------------

    def build_evidence_packet(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> GateEvidencePacket:
        metadata = dict(task.metadata or {})
        projection_id = projection_id_for_task(task)

        # Requirements = the task brief the agent received.
        requirements = f"{task.title or ''}\n\n{task.description or ''}".strip()

        # Output = agent's raw stdout result.
        if isinstance(task.result, dict):
            output = str(task.result.get("content", "") or "")
        elif task.result is not None:
            output = str(task.result)
        else:
            output = ""

        return GateEvidencePacket(
            projection_id=projection_id,
            requirements=requirements,
            output=output,
            prior_rework_count=int(metadata.get("gate_harness_rework_count", 0) or 0),
            prior_rework_feedback=str(metadata.get("gate_harness_rework_feedback", "") or "").strip(),
        )

    # ------------------------------------------------------------------
    # LLM judge
    # ------------------------------------------------------------------

    async def _invoke_judge(
        self,
        packet: GateEvidencePacket,
        *,
        source_task: Task,
    ) -> GateHarnessDecision:
        # Preferred: company_mode-supplied runner that spawns a fresh
        # session of the SAME external agent type that produced this
        # work item (codex → codex, etc.). Fallback: native simple_chat.
        # Strategy: the judge_runner path spawns a fresh agent session to
        # produce the JSON. Per system design, errors inside a running
        # agent are handled by the agent itself, so we keep that path
        # single-shot. Only the simple_chat fallback is wrapped in the
        # retry helper, because there is no agent to self-correct.
        #
        # Whatever keeps the judge from answering -- an error, nothing usable, no judge at all -- escalates to a
        # person (model-routing phase 0). Catch order on both paths: a stop (the allowance, or refused before
        # sending), a provider quota error (never re-raised: its park would re-run the paid work turn), then the rest.
        if self.judge_runner is not None:
            try:
                raw = await self.judge_runner(packet, GATE_HARNESS_AGENT_PROMPT, source_task)
            except ModelBudgetStop as exc:
                return self._budget_stop_decision(packet, exc)
            except ProviderQuotaExhaustedError as exc:
                return self._judge_unavailable(packet, "quota", str(exc))
            except Exception as exc:
                return self._judge_unavailable(packet, "runner_error", f"{type(exc).__name__}: {exc}")
            if not str(raw or "").strip():
                return self._judge_unavailable(packet, "empty", "")
            ok, data = parse_role_prompt_json_value(raw)
            if not ok:
                return self._judge_unavailable(packet, "non_json", str(raw)[:200])
            if not isinstance(data, dict):
                return self._judge_unavailable(packet, "non_object", str(raw)[:200])
            action = str(data.get("action", "") or "").strip()
            if action not in self.ALLOWED_ACTIONS:
                return self._judge_unavailable(packet, "unknown_action", action[:80])
        elif self.llm is not None:
            allowed_actions = self.ALLOWED_ACTIONS

            def _validate_judge_response(parsed: Any) -> str | None:
                if not isinstance(parsed, dict):
                    return "Top-level response must be a JSON object."
                act = str(parsed.get("action", "") or "").strip()
                if act not in allowed_actions:
                    return (
                        f"Unknown action `{act}`. Choose one of: "
                        f"{', '.join(sorted(allowed_actions))}."
                    )
                return None

            from ampuly.core.order_model_plan import gate_model_for, plan_scope_for

            try:
                with plan_scope_for(source_task):
                    # 「找不到合格模型就交给人，绝不放行」: the floor is never below 标准 and never below what the
                    # work itself ran on, so a strong step is not judged by the flash model.
                    allowed = gate_model_for(source_task, live_config=getattr(self.llm, "config", None))
                    if allowed.get("refused"):
                        return self._judge_unavailable(packet, "gate_tier_unavailable",
                                                       "no model in the table is strong enough to judge this")
                    data = await call_llm_json_with_retry(
                        self.llm,
                        system=GATE_HARNESS_AGENT_PROMPT,
                        payload=packet.to_dict(),
                        task_type="gate_judge",
                        validator=_validate_judge_response,
                        label=f"gate_harness:{packet.projection_id}",
                        meter=meter_for_task(
                            getattr(self, "cost_tracker", None), source_task
                        ),
                    )
            except ModelBudgetStop as exc:
                return self._budget_stop_decision(packet, exc)
            except ProviderQuotaExhaustedError as exc:
                return self._judge_unavailable(packet, "quota", str(exc))
            except LLMRetryError as exc:
                kind = exc.kind if isinstance(exc, LLMOutcomeUnknown) else "no_valid_verdict"
                return self._judge_unavailable(packet, kind, str(exc.last_error))
            except Exception as exc:
                return self._judge_unavailable(packet, "runner_error", f"{type(exc).__name__}: {exc}")
            action = str(data.get("action", "") or "").strip()
        else:
            return self._judge_unavailable(packet, "no_judge", "")

        reason = str(data.get("reason", "") or "").strip() or "(no reason provided)"

        # `reason` is the canonical text fed back to the agent's session
        # via `gate_harness_rework_feedback` on the next turn. The judge
        # is instructed to put any "what to fix and how" content directly
        # in `reason` when the action is rework_same_work_item.
        return GateHarnessDecision(
            action=action,
            summary=reason,
            target_projection_id=packet.projection_id if action == "rework_same_work_item" else "",
            target_projection_ids=[packet.projection_id] if action == "rework_same_work_item" else [],
            source="llm_judge",
            judged_by=self._judge_identity(),
            blocker_fingerprint=self._fingerprint(action, reason),
        )

    def _budget_stop_decision(self, packet: GateEvidencePacket, exc: ModelBudgetStop) -> GateHarnessDecision:
        """The judge was stopped before it could answer -- by the order's allowance, or refused before sending.

        Not a transport error to pass over: a gate that nobody judged goes to a person, the finished work is kept, and
        the pause names the reason (``budget_stop_wait.judge_stop_kind`` reads the kind from the notes).
        """
        refused = isinstance(exc, RouteRefused)
        kind = "route_refused" if refused else str(getattr(exc, "kind", "") or "refused")
        logger.warning(
            "[gate_harness] Judge for work item `%s` was stopped (%s); escalating to a person.",
            packet.projection_id, kind,
        )
        if refused:
            reason = (
                f"The gate judge's model call was refused before it was sent ({exc.code}): nothing was sent or spent, and "
                "nobody judged this work item; a person decides. Approve accepts the work as it is, unjudged; deny fails "
                "this work item -- it does not re-run the judge. Fix the judge's key or model first if later steps need it."
            )
        else:
            reason = (
                f"The order's allowance stopped the gate judge ({kind}): {exc}. Nobody judged this work item; a person decides. "
                "Approve accepts the work as it is, unjudged; deny fails this work item (the run is then settled as failed) -- "
                "it does not re-run the judge, so grant more allowance before approving if later steps need it."
            )
        return GateHarnessDecision(
            action="escalate",
            summary=reason,
            notes=[f"budget_stop:{kind}", str(exc)],
            source="budget_stop",
            judged_by=self._judge_identity(),
            blocker_fingerprint=self._fingerprint("escalate", f"budget_stop:{kind}"),
        )

    def _judge_unavailable(self, packet: GateEvidencePacket, kind: str, detail: str) -> GateHarnessDecision:
        """Nobody judged this work item: a person decides. Never a pass (model-routing phase 0)."""
        logger.warning(
            "[gate_harness] Nobody judged work item `%s` (%s); escalating to a person.", packet.projection_id, kind,
        )
        return GateHarnessDecision(
            action="escalate",
            summary=(
                f"Nobody judged this work item ({kind}). Approve accepts the work as it is, unjudged; deny fails this "
                "work item -- it does not re-run the judge."
            ),
            notes=[f"judge_unavailable:{kind}", str(detail or "")[:300]],
            source="judge_unavailable",
            judged_by=self._judge_identity(),
            blocker_fingerprint=self._fingerprint("escalate", f"judge_unavailable:{kind}"),
        )

    def _judge_identity(self) -> str:
        """Who is judging: the work's own provider, or the native stand-in."""
        return "provider_session" if self.judge_runner is not None else "native_fallback"

    # ------------------------------------------------------------------
    # Stagnation cap (Class C: the only deterministic decision left)
    # ------------------------------------------------------------------

    def _apply_stagnation_cap(
        self,
        task: Task,
        decision: GateHarnessDecision,
    ) -> GateHarnessDecision:
        """Escalate only when the SAME problem keeps repeating.

        Total rework count is irrelevant — a work item that gets reworked
        10 times for 10 different reasons is healthy iteration (e.g.
        user keeps asking for changes). What indicates stagnation is
        N consecutive reworks whose fingerprints match: the judge
        keeps saying the same thing, meaning the agent is unable to
        fix the problem.

        We count consecutive trailing rework entries in history that
        share the current decision's fingerprint. If that streak
        reaches the threshold, we escalate.
        """
        if decision.action != "rework_same_work_item":
            return decision
        threshold = max(2, int(getattr(self.policy, "stagnation_threshold", 3) or 3))
        current_fp = decision.blocker_fingerprint
        if not current_fp:
            return decision

        history = [
            dict(item)
            for item in list(task.metadata.get("gate_harness_history", []) or [])
            if isinstance(item, dict)
        ]

        # Count consecutive trailing reworks with the same fingerprint.
        consecutive = 0
        for item in reversed(history):
            if (
                str(item.get("action", "") or "") == "rework_same_work_item"
                and str(item.get("blocker_fingerprint", "") or "") == current_fp
            ):
                consecutive += 1
            else:
                break

        if consecutive + 1 < threshold:
            return decision

        upgraded_summary = (
            f"{decision.summary}\n\n"
            f"[stagnation] The same problem has been flagged {consecutive + 1} "
            f"consecutive times without progress. Escalating to user."
        )
        return GateHarnessDecision(
            action="escalate",
            summary=upgraded_summary,
            target_projection_id=decision.target_projection_id,
            target_projection_ids=list(decision.target_projection_ids),
            source=f"{decision.source}+stagnation_cap",
            blocker_fingerprint=decision.blocker_fingerprint,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(action: str, reason: str) -> str:
        if not action and not reason:
            return ""
        joined = f"{action}||{reason.strip()[:240]}"
        return hashlib.sha1(joined.encode("utf-8", errors="replace")).hexdigest()[:16]

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        value = str(text or "").strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[1] if "\n" in value else value[3:]
            if value.endswith("```"):
                value = value[:-3]
        return value.strip()
