"""Recovery-aware composition of the ORIGINAL lifecycle, not another worker.

Epoch is a private server-created native trial-context field. Original trial
claims, observations, budget, evaluation parsing, human review and publication
ledger stay in EmployeeLifecycle. Historical facts remain readable; a new
current-epoch approval can revalidate an existing immutable published version
without overwriting or re-publishing its directory.
"""
from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

from ampuly.core.restoration_gate import RestorationGate, RestorationGateError, TRIAL_EPOCH
from .ai_employee_contract import AIEmployeeContractError
from .employee_lifecycle import EmployeeLifecycle, build_employee_repositories
from .employee_lifecycle_store import EmployeeLifecycleStore


def _contract(exc):
    return AIEmployeeContractError(exc.code, "资料已恢复；旧执行保持暂停，请由当前所有者重新试跑和验收。")


class RecoveryEmployeeLifecycle(EmployeeLifecycle):
    def __init__(self, root, **kwargs):
        super().__init__(root, **kwargs)
        self.restoration_gate = RestorationGate(root)

    def _has_active_trials(self, conn):
        """Original start hook: old unfinished history is NOT a new worker."""
        status = self.restoration_gate.status()
        if not status.restored:
            return bool(conn.execute("SELECT COUNT(*) FROM employee_trials WHERE state IN ('prepared','running','outcome_unknown')").fetchone()[0])
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise AIEmployeeContractError("storage_record_invalid", "试跑冲突检查需要原生事务。")
        rows = conn.execute("SELECT context_json FROM employee_trials WHERE state IN ('prepared','running','outcome_unknown') LIMIT 201").fetchall()
        if len(rows) > 200:
            raise AIEmployeeContractError("storage_busy", "旧活动记录数量超出恢复核对范围。")
        for row in rows:
            raw = row[0]
            if type(raw) is not bytes or len(raw) > 65536:
                raise AIEmployeeContractError("storage_record_invalid", "原试跑上下文不能核实。")
            try:
                context = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise AIEmployeeContractError("storage_record_invalid", "原试跑上下文不能核实。") from None
            if type(context) is not dict:
                raise AIEmployeeContractError("storage_record_invalid", "原试跑上下文不能核实。")
            if context.get(TRIAL_EPOCH) == status.epoch:
                return True
        self.restoration_gate.status()
        return False

    def require_current_trial_dispatch(self, trial_id):
        try:
            return self.restoration_gate.require_trial(self.state, trial_id)
        except RestorationGateError as exc:
            raise _contract(exc) from None

    def prepare(self, **kwargs):
        result = super().prepare(**kwargs)
        try:
            return {**result, "context": self.restoration_gate.trial_context(result["context"])}
        except RestorationGateError as exc:
            raise _contract(exc) from None

    def claim_trial(self, trial_id):
        try:
            self.restoration_gate.require_trial(self.state, trial_id)
        except RestorationGateError as exc:
            raise _contract(exc) from None
        return super().claim_trial(trial_id)

    def _evidence(self, package_id, fingerprint, *, project_id, actor):
        evidence = super()._evidence(package_id, fingerprint, project_id=project_id, actor=actor)
        try:
            self.restoration_gate.require_trial(self.state, evidence[0]["trial_id"])
        except RestorationGateError as exc:
            raise _contract(exc) from None
        return evidence

    def trial_detail(self, *, project_id, actor, trial_id):
        result = super().trial_detail(project_id=project_id, actor=actor, trial_id=trial_id)
        status = self.restoration_gate.status()
        if not status.restored:
            return result
        trial = self.state.trial(trial_id, project_id=project_id)
        historical = trial["context"].get(TRIAL_EPOCH) != status.epoch
        result = {**result, "recovery_revalidation_required": historical, "historical": historical}
        if historical:
            result["allowed_actions"] = []
        elif result["state"] == "approved" and self.restoration_gate.package_revalidated(
                self.state, trial["package_id"], trial["fingerprint"]):
            # The original immutable catalogue verifies the old publication
            # hash chain. Current environment evidence is the NEW approval,
            # not a rewritten published_at or second native promotion.
            try:
                catalog, _, _ = build_employee_repositories(self.root)
                existing = catalog.read(trial["package_id"], fingerprint=trial["fingerprint"], purpose="pinned")
            except AIEmployeeContractError:
                pass  # A new, not-yet-published draft retains normal publish.
            else:
                if existing["status"] == "published":
                    result["allowed_actions"] = ["employee_instance_create"]
                    result["recovery_revalidation_required"] = False
        return result

    def review(self, **kwargs):
        self._check(kwargs["project_id"], kwargs["actor"])
        try:
            self.restoration_gate.require_trial(self.state, kwargs["trial_id"])
        except RestorationGateError as exc:
            raise _contract(exc) from None
        return super().review(**kwargs)

    def publish(self, **kwargs):
        # Original publish checks old request receipts before _evidence. Do
        # not let a historical committed receipt look like a new grant.
        self._check(kwargs["project_id"], kwargs["actor"])
        if self.restoration_gate.status().restored:
            self._evidence(kwargs["package_id"], kwargs["expected_fingerprint"],
                project_id=kwargs["project_id"], actor=kwargs["actor"])
        return super().publish(**kwargs)

    def lookup_publication(self, **kwargs):
        result = super().lookup_publication(**kwargs)
        if self.restoration_gate.status().restored:
            historical = not self.restoration_gate.package_revalidated(
                self.state, kwargs["package_id"], kwargs["expected_fingerprint"])
            result = {**result, "historical": historical, "recovery_revalidation_required": historical}
            if historical:
                result["allowed_actions"] = []
        return result


def build_recovery_employee_repositories(ampuly_home, *, context_provider=None):
    """Preserve pinned/history reads, gate only NEW-adoption policy flags."""
    catalog, instances, bindings = build_employee_repositories(ampuly_home, context_provider=context_provider)
    gate, state = RestorationGate(ampuly_home), EmployeeLifecycleStore(ampuly_home)
    original_loader = instances.package_loader

    def package_loader(package_id, version, fingerprint):
        record = deepcopy(original_loader(package_id, version, fingerprint))
        if not gate.package_revalidated(state, package_id, fingerprint):
            record["new_adoption_allowed"] = False
            record["is_current_verified"] = False
        return record

    instances.package_loader = package_loader
    return catalog, instances, bindings
