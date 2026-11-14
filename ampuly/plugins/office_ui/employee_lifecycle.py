"""Owner-authorized employee trial, review and immutable publication service.

No constructor creates a ledger, starts a worker or reads a live model. Model
execution and current owner checks come from the installed server composition,
never request JSON. Trial observations are written by the original worker only;
human judgments cannot manufacture a missing model execution.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets

from .ai_employee_contract import AIEmployeeContractError, canonical_json, validate_package_manifest, _declarations
from .employee_lifecycle_store import EmployeeLifecycleStore
from .employee_packages import EmployeePackageRepository
from .employee_package_versions import PublicationAnchors, publication_readiness
from .employee_publication_authority import (
    PublicationAuthorityLedger, RequestIdentity, FrozenPublication, ExpectedFile,
    AuthorizationLease, GenesisReceipt, AssetObservation, ObservedFile,
)
from .ai_employee_safe_files import AIEmployeeSafeFiles
from .ai_employee_safe_write import atomic_write

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ZERO = "0" * 64
_CHECKERS = {"output_schema": "jsonschema-2020-12-v1", "owner_quality_review": "employee-review-v1"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical_json(value)).hexdigest()


def _fail(code, message="岗位生命周期条件不满足；没有跳过验证、发布或采用。"):
    raise AIEmployeeContractError(code, message)


def _text(value, maximum=4000):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        _fail("request_invalid")
    return value


class EmployeeLifecycle:
    def __init__(self, root: Path, *, authorize, runner=None, capabilities=None):
        self.root = Path(root)
        self.authorize = authorize
        self.runner = runner
        self.capabilities = dict(capabilities or {})
        self.drafts = EmployeePackageRepository(self.root, available_capabilities=self.capabilities)
        self.state = EmployeeLifecycleStore(self.root)
        self._effect_receipts = {}

    def _check(self, project_id, actor):
        # The callback must raise on denial and return None. A truthy DTO from a
        # caller or an unresolved coroutine is not a current authorization.
        if not callable(self.authorize) or self.authorize(project_id, actor) is not None:
            _fail("owner_required")

    def history(self, *, project_id, actor, package_id):
        from .employee_lifecycle_history import read_lifecycle_history
        return read_lifecycle_history(self, project_id=project_id, actor=actor, package_id=package_id)

    def candidate(self, package_id, revision, fingerprint):
        from .employee_publication_candidate import collect_publication_candidate
        if type(revision) is not str or not _SHA.fullmatch(revision) or type(fingerprint) is not str or not _SHA.fullmatch(fingerprint):
            _fail("request_invalid")
        manifest, raw = self.drafts._load(package_id)
        if _sha(raw) != revision or manifest.get("package_fingerprint") != fingerprint:
            _fail("revision_conflict")
        problems = validate_package_manifest(manifest, available_capabilities=self.capabilities, require_complete=True)
        if any(row["severity"] == "error" for row in problems):
            _fail("validation_failed")
        files, trees = collect_publication_candidate(self.root, manifest, revision)
        return manifest, files, trees

    def _trial_source(self, *, project_id, package_id, revision, fingerprint, instance_selection=None):
        if instance_selection is None:
            manifest, files, trees = self.candidate(package_id, revision, fingerprint)
            return manifest, files, trees, {"params": {}, "knowledge_refs": [], "knowledge": {},
                                           "qualification_scope": {"kind": "package"}}
        if self.runner is None or not callable(getattr(self.runner, "instance_snapshot", None)):
            _fail("evaluation_runner_unavailable")
        snapshot = self.runner.instance_snapshot(project_id=project_id, selection=instance_selection)
        if (snapshot["manifest"]["package_id"] != package_id
                or snapshot["manifest"]["package_fingerprint"] != fingerprint
                or snapshot["revision"] != revision):
            _fail("revision_conflict")
        return snapshot["manifest"], snapshot["files"], snapshot["trees"], snapshot

    def prepare(self, *, project_id, actor, package_id, expected_revision, expected_fingerprint,
                instance_selection=None):
        self._check(project_id, actor)
        manifest, files, trees, snapshot = self._trial_source(project_id=project_id, package_id=package_id,
            revision=expected_revision, fingerprint=expected_fingerprint, instance_selection=instance_selection)
        taskset = files.get("taskset.json")
        if taskset is None or _sha(taskset) != manifest["evaluation_profile"]["taskset_fingerprint"]:
            _fail("evaluation_taskset_unavailable")
        value = json.loads(taskset)
        cases = value.get("cases") if type(value) is dict else None
        if (type(cases) is not list or not 1 <= len(cases) <= 64
                or len(cases) < manifest["evaluation_profile"]["min_samples"]
                or any(type(case) is not dict or type(case.get("id")) is not str for case in cases)
                or len({case["id"] for case in cases}) != len(cases)):
            _fail("evaluation_taskset_invalid")
        canonical_json(cases)
        if self.runner is None:
            _fail("evaluation_runner_unavailable")
        context = snapshot.get("context") or self.runner.context(manifest)
        if (type(context) is not dict or set(context) != {"model_config", "capability_impls", "runtime_profile_fingerprint"}
                or not _SHA.fullmatch(str(context["runtime_profile_fingerprint"]))):
            _fail("evaluation_context_unavailable")
        from .employee_qualification import seal_context
        context = seal_context({**context, "checker_versions": _CHECKERS, "skill_tree_fingerprints": trees},
            params=snapshot["params"], knowledge_refs=snapshot["knowledge_refs"],
            scope=snapshot["qualification_scope"])
        self._check(project_id, actor)
        return {"package_id": package_id, "revision": expected_revision, "fingerprint": expected_fingerprint,
                "sample_count": len(cases), "context": deepcopy(context),
                "data_scope": "instance_taskset" if instance_selection is not None else "package_taskset",
                "human_quality_review_required": True,
                "effects_produced": False, "allowed_actions": ["employee_package_test_start"]}

    def start(self, *, project_id, actor, request_id, package_id, expected_revision,
              expected_fingerprint, reason, data_scope, budget, instance_selection=None):
        self._check(project_id, actor)
        _text(request_id, 128); _text(reason)
        expected_scope = "instance_taskset" if instance_selection is not None else "package_taskset"
        if data_scope != expected_scope:
            _fail("request_invalid", "测试数据范围必须与明确选定的岗位包或实例相符。")
        payload = dict(package_id=package_id, expected_revision=expected_revision,
                       expected_fingerprint=expected_fingerprint, reason=reason, data_scope=data_scope, budget=budget)
        if instance_selection is not None:
            payload["instance_selection"] = deepcopy(instance_selection)
        payload_hash = _sha(payload)
        with self.state.assets._lock():
            self.state.initialize_new()
            previous = self.state.by_request(project_id, request_id)
            if previous is not None:
                if previous["actor"] != actor or previous["payload_sha256"] != payload_hash:
                    _fail("request_id_reused")
                return self.trial_detail(project_id=project_id, actor=actor, trial_id=previous["trial_id"])
            prepared = self.prepare(project_id=project_id, actor=actor, package_id=package_id,
                                    expected_revision=expected_revision, expected_fingerprint=expected_fingerprint,
                                    instance_selection=instance_selection)
            manifest, files, _, snapshot = self._trial_source(project_id=project_id, package_id=package_id,
                revision=expected_revision, fingerprint=expected_fingerprint, instance_selection=instance_selection)
            if instance_selection is not None:
                from .employee_qualification import seal_context
                observed = seal_context({**snapshot["context"], "checker_versions": _CHECKERS,
                    "skill_tree_fingerprints": snapshot["trees"]}, params=snapshot["params"],
                    knowledge_refs=snapshot["knowledge_refs"], scope=snapshot["qualification_scope"])
                if observed != prepared["context"]:
                    _fail("evaluation_context_changed")
            cases = json.loads(files["taskset.json"])["cases"]
            if instance_selection is None:
                self.runner.validate_budget(manifest, budget, len(cases))
            else:
                self.runner.validate_budget(manifest, budget, len(cases), context=prepared["context"])
            trial_id = "et-" + secrets.token_hex(12)
            frozen = {"manifest": manifest, "revision": expected_revision, "budget": deepcopy(budget),
                      "data_scope": data_scope,
                      "params": deepcopy(snapshot["params"]),
                      "knowledge_refs": deepcopy(snapshot["knowledge_refs"]),
                      "knowledge": deepcopy(snapshot["knowledge"]),
                      "instance_selection": deepcopy(instance_selection),
                      "file_order": list(files),
                      "files": {path: base64.b64encode(raw).decode("ascii") for path, raw in files.items()}}
            if sum(len(raw) for raw in files.values()) > 1024 * 1024:
                _fail("candidate_read_limit")
            self._check(project_id, actor)
            with self.state.transaction() as conn:
                if self._has_active_trials(conn):
                    _fail("storage_busy", "已有岗位测试未结束或结果未知；先核对原测试。")
                if conn.execute("SELECT COUNT(*) FROM employee_trials").fetchone()[0] >= 200:
                    _fail("candidate_read_limit", "测试记录已达本地保留上限；未清理原记录或启动新测试。")
                conn.execute("INSERT INTO employee_trials VALUES(?,?,?,?,?,?,?,'prepared',?,?,?,?,NULL,?,NULL)",
                    (trial_id, project_id, package_id, expected_fingerprint, actor, request_id, payload_hash,
                     canonical_json(frozen), canonical_json(prepared["context"]), canonical_json(cases), b"[]", _now()))
            # Persist the exact attempt before asking the host to schedule it.
            # A lost scheduling receipt is not permission to start another job.
        # Release employee_packages/.write.lock before schedule. Office-UI runs
        # start off the server loop; runner.start posts _run onto that loop,
        # and claim_trial takes this same nonblocking lock. Holding it across
        # the post lets the worker overlap, fail storage_busy, and never run.
        # Same-loop starts hide the race because _run waits until start returns.
        try:
            self.runner.start(self, trial_id, deepcopy(budget))
        except BaseException:
            with self.state.assets._lock():
                with self.state.transaction() as conn:
                    conn.execute(
                        "UPDATE employee_trials SET state='outcome_unknown' WHERE trial_id=? AND state='prepared'",
                        (trial_id,),
                    )
            raise
        # The worker's claim_trial may hold authority.db briefly right after
        # schedule. The trial row is already committed, so a short wait to read
        # it back is not a second start and does not change its outcome.
        import sqlite3, time
        for attempt in range(30):
            try:
                return self.trial_detail(project_id=project_id, actor=actor, trial_id=trial_id)
            except (sqlite3.OperationalError, AIEmployeeContractError) as exc:
                busy = (isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)) or \
                    getattr(exc, "code", None) == "storage_busy"
                if not busy or attempt == 29:
                    raise
                time.sleep(0.1)

    def trial_detail(self, *, project_id, actor, trial_id):
        self._check(project_id, actor)
        trial = self.state.trial(trial_id, project_id=project_id)
        if trial is None:
            _fail("evaluation_trial_not_found")
        review_receipt = self.state.trial_review(trial_id)
        allowed = ["employee_package_review"] if trial["state"] == "awaiting_review" else []
        if trial["state"] == "approved" and trial["candidate"].get("instance_selection") is None:
            # A past pass is not authority over a changed draft. Re-read the
            # actual publication proof before offering the next UI action.
            try:
                self._evidence(trial["package_id"], trial["fingerprint"], project_id=project_id, actor=actor)
                allowed = ["employee_package_publish"]
            except AIEmployeeContractError:
                allowed = []
        return {"trial_id": trial_id, "package_id": trial["package_id"], "fingerprint": trial["fingerprint"],
                "candidate_revision": trial["candidate"]["revision"], "test_request_id": trial["request_id"],
                "review_request_id": review_receipt["review_request_id"] if review_receipt else None,
                "decision": review_receipt["decision"] if review_receipt else None,
                "state": trial["state"], "context": trial["context"],
                "revision": _sha({"trial_id": trial_id, "state": trial["state"], "observations": trial["observations"],
                                  "record": trial["record"]}),
                "sample_count": len(trial["samples"]), "observations": trial["observations"],
                "cases": deepcopy(trial["samples"]),
                "effects_produced": False, "test_passed": trial["state"] == "approved",
                "allowed_actions": allowed,
                **({"abandon": deepcopy(trial["record"])} if trial["state"] == "abandoned" else {})}

    # Owner closes a trial whose outcome could not be established. The row is
    # never deleted or reset: its budget id and observations stay as recorded
    # (spend is not pretended away). The audit record lives in record_json, the
    # state becomes the terminal 'abandoned' (not reviewable, not publishable),
    # and it no longer holds the one-active-trial exclusivity.
    _ABANDONABLE = frozenset({"outcome_unknown"})

    def abandon_trial(self, *, project_id, actor, request_id, trial_id, expected_state, reason):
        self._check(project_id, actor)
        _text(request_id, 128); _text(reason)
        if expected_state not in self._ABANDONABLE:
            _fail("not_allowed_in_status")
        with self.state.assets._lock():
            trial = self.state.trial(trial_id, project_id=project_id)
            if trial is None:
                _fail("evaluation_trial_not_found")
            record = trial["record"]
            if trial["state"] == "abandoned":
                if not (type(record) is dict and record.get("kind") == "employee_trial_abandon"
                        and record.get("request_id") == request_id and record.get("actor") == actor
                        and record.get("reason") == reason):
                    _fail("not_allowed_in_status")
            elif trial["state"] != expected_state or record is not None:
                _fail("not_allowed_in_status")
            else:
                audit = {"kind": "employee_trial_abandon", "trial_id": trial_id, "package_id": trial["package_id"],
                         "fingerprint": trial["fingerprint"], "prior_state": trial["state"], "actor": actor,
                         "request_id": request_id, "reason": reason, "at": _now(), "budget_id": trial["budget_id"],
                         "observations_sha256": _sha(trial["observations"]), "spend_reset": False,
                         "reviewable": False, "publishable": False}
                with self.state.transaction() as conn:
                    if conn.execute("UPDATE employee_trials SET state='abandoned',record_json=? "
                                    "WHERE trial_id=? AND project_id=? AND state=? AND record_json IS NULL",
                                    (canonical_json(audit), trial_id, project_id, expected_state)).rowcount != 1:
                        _fail("storage_busy")
        return self.trial_detail(project_id=project_id, actor=actor, trial_id=trial_id)

    def _has_active_trials(self, conn):
        return bool(conn.execute("SELECT COUNT(*) FROM employee_trials WHERE state IN ('prepared','running','outcome_unknown')").fetchone()[0])

    def claim_trial(self, trial_id):
        """One original worker claims the persisted attempt before any spend."""
        with self.state.assets._lock():
            trial = self.state.trial(trial_id)
            if trial is None or trial["state"] != "prepared":
                _fail("evaluation_trial_stale")
            self._check(trial["project_id"], trial["actor"])
            with self.state.transaction() as conn:
                if conn.execute("UPDATE employee_trials SET state='running',observed_at=? WHERE trial_id=? AND state='prepared'",
                                (_now(), trial_id)).rowcount != 1:
                    _fail("evaluation_trial_stale")
            return {**trial, "state": "running"}

    def bind_trial_budget(self, trial_id, budget_id):
        _text(budget_id, 128)
        with self.state.assets._lock():
            with self.state.transaction() as conn:
                if conn.execute("UPDATE employee_trials SET budget_id=? WHERE trial_id=? AND state='running' AND budget_id IS NULL",
                                (budget_id, trial_id)).rowcount != 1:
                    _fail("evaluation_trial_stale")

    def finish_trial(self, trial_id, *, state):
        if state not in {"failed", "outcome_unknown", "cancelled"}:
            _fail("request_invalid")
        with self.state.assets._lock():
            with self.state.transaction() as conn:
                conn.execute("UPDATE employee_trials SET state=?,observed_at=? WHERE trial_id=? AND state IN ('prepared','running')",
                             (state, _now(), trial_id))

    def lookup_trial(self, *, project_id, actor, original_request_id):
        self._check(project_id, actor)
        _text(original_request_id, 128)
        original = self.state.by_request(project_id, original_request_id)
        if original is None or original["actor"] != actor:
            _fail("evaluation_trial_not_found")
        return self.trial_detail(project_id=project_id, actor=actor, trial_id=original["trial_id"])

    def record_observation(self, trial_id, observation, *, final=False):
        """Host-worker only; deliberately absent from every client dispatcher."""
        with self.state.assets._lock():
            trial = self.state.trial(trial_id)
            if trial is None or trial["state"] != "running":
                _fail("evaluation_trial_stale")
            self._check(trial["project_id"], trial["actor"])
            if type(observation) is not dict or set(observation) != {"case_id", "completed", "hard_fail_count", "output", "model"}:
                _fail("evaluation_observation_invalid")
            index = len(trial["observations"])
            if (index >= len(trial["samples"]) or observation["case_id"] != trial["samples"][index]["id"]
                    or type(observation["completed"]) is not bool
                    or type(observation["hard_fail_count"]) is not int or not 0 <= observation["hard_fail_count"] <= 1000
                    or type(observation["output"]) is not str or len(observation["output"].encode("utf-8")) > 64 * 1024
                    or observation["model"] != trial["context"]["model_config"]["model"]
                    or type(final) is not bool or final != (index + 1 == len(trial["samples"]))):
                _fail("evaluation_observation_invalid")
            observations = trial["observations"] + [deepcopy(observation)]
            with self.state.transaction() as conn:
                conn.execute("UPDATE employee_trials SET observations_json=?,state=?,observed_at=? WHERE trial_id=? AND state='running'",
                    (canonical_json(observations), "awaiting_review" if final else "running", _now(), trial_id))

    def review(self, *, project_id, actor, request_id, trial_id, expected_fingerprint, decision, judgments, note):
        self._check(project_id, actor)
        _text(request_id, 128)
        if decision not in {"approve", "reject"} or type(note) is not str or len(note) > 4000:
            _fail("request_invalid")
        payload_hash = _sha(dict(trial_id=trial_id, expected_fingerprint=expected_fingerprint,
                                 decision=decision, judgments=judgments, note=note))
        with self.state.assets._lock():
            trial = self.state.trial(trial_id, project_id=project_id)
            if trial is None or trial["fingerprint"] != expected_fingerprint:
                _fail("evaluation_trial_not_found")
            old = self.state.review_request(request_id, actor)
            if old is not None:
                if old["trial_id"] != trial_id or old["payload_sha256"] != payload_hash:
                    _fail("request_id_reused")
                return self.trial_detail(project_id=project_id, actor=actor, trial_id=trial_id)
            if trial["state"] != "awaiting_review":
                _fail("not_allowed_in_status")
            if trial["candidate"].get("instance_selection") is None:
                manifest, _, _ = self.candidate(trial["package_id"], trial["candidate"]["revision"], expected_fingerprint)
            else:
                # A reviewed instance configuration is private evidence. It
                # must never rewrite/re-publish the shared package candidate.
                from .employee_trial_runner import EmployeeTrialRunner
                manifest, _, _ = EmployeeTrialRunner._frozen(trial)
            observations = trial["observations"]
            if (len(observations) != len(trial["samples"]) or type(judgments) is not list
                    or len(judgments) != len(observations)):
                _fail("request_invalid", "请逐条查看真实测试输出并给出质量判断，不能只提交通过标记。")
            repeats = []
            for observation, judgment in zip(observations, judgments):
                if (type(judgment) is not dict or set(judgment) != {"case_id", "non_inferior_met", "absolute_threshold_met"}
                        or judgment["case_id"] != observation["case_id"]
                        or type(judgment["non_inferior_met"]) is not bool or type(judgment["absolute_threshold_met"]) is not bool):
                    _fail("request_invalid")
                repeats.append(dict(repeat_id=observation["case_id"], completed=observation["completed"],
                    hard_fail_count=observation["hard_fail_count"], non_inferior_met=judgment["non_inferior_met"],
                    absolute_threshold_met=judgment["absolute_threshold_met"]))
            passing = all(r["completed"] and r["hard_fail_count"] == 0 and r["non_inferior_met"] and r["absolute_threshold_met"] for r in repeats)
            if decision == "approve" and not passing:
                _fail("publication_test_not_passed")
            stamp = _now()
            record = dict(schema_version=1, record_kind="employee_package.evaluation", record_id="evaluation-" + trial_id,
                trial_id=trial_id, package_id=trial["package_id"], package_fingerprint=expected_fingerprint,
                taskset_fingerprint=manifest["evaluation_profile"]["taskset_fingerprint"],
                model_config=trial["context"]["model_config"], capability_impls=trial["context"]["capability_impls"],
                checker_versions=trial["context"]["checker_versions"], repeats=repeats, observed_at=stamp, previous_sha256=_ZERO)
            record["record_sha256"] = _sha(record)
            approval = dict(schema_version=1, record_kind="employee_package.release_approval", package_id=trial["package_id"],
                approval=dict(approval_id="approval-" + trial_id, status="approved" if decision == "approve" else "rejected",
                    requested_by=trial["actor"], requested_at=trial["observed_at"], candidate_fingerprint=expected_fingerprint,
                    decided_by=actor, decided_at=stamp, decision_note=note))
            self._check(project_id, actor)
            with self.state.transaction() as conn:
                conn.execute("UPDATE employee_trials SET record_json=?,state=? WHERE trial_id=? AND state='awaiting_review'",
                             (canonical_json(record), "approved" if decision == "approve" else "rejected", trial_id))
                conn.execute("INSERT INTO employee_approvals VALUES(?,?,?,?,?,?,?)",
                    (trial["package_id"], expected_fingerprint, trial_id, actor, request_id, payload_hash, canonical_json(approval)))
        return self.trial_detail(project_id=project_id, actor=actor, trial_id=trial_id)

    def _evidence(self, package_id, fingerprint, *, project_id, actor):
        approval = self.state.approval(package_id, fingerprint)
        if approval is None or approval["actor"] != actor:
            _fail("publication_approval_unavailable")
        trial = self.state.trial(approval["trial_id"], project_id=project_id)
        if trial is None or trial["state"] != "approved" or trial["record"] is None:
            _fail("publication_evaluation_unavailable")
        manifest, files, trees = self.candidate(package_id, trial["candidate"]["revision"], fingerprint)
        if trees != trial["context"]["skill_tree_fingerprints"]:
            _fail("fingerprint_mismatch")
        record = trial["record"]
        anchors = PublicationAnchors(evaluation_tail_sha256=record["record_sha256"],
            approval_sha256=_sha(approval["record_raw"]), authorized_trial_ids=frozenset({trial["trial_id"]}),
            authorized_reviewer_ids=frozenset({actor}))
        publication_context = {key: trial["context"][key] for key in
            ("model_config", "capability_impls", "checker_versions", "skill_tree_fingerprints")}
        resources = {path: files[path] for path in _declarations(manifest)}
        readiness = publication_readiness(manifest, resources, expected_fingerprint=fingerprint,
            expected_context=publication_context, anchors=anchors, evaluations_raw=canonical_json(record) + b"\n",
            approval_raw=approval["record_raw"], available_capabilities=self.capabilities)
        if readiness["ready"] is not True:
            _fail("publication_test_not_passed")
        return trial, manifest, files, approval

    def _authority(self, project_id, actor):
        return PublicationAuthorityLedger(read_connection=self.state.connection,
            write_connection=lambda: self.state.connection(write=True),
            authorization=_Authorization(self, project_id, actor),
            source_root=self.root, source_bound_writer=self._write_version, observer=self._observe_version)

    def lookup_publication(self, *, project_id, actor, original_request_id, package_id, expected_fingerprint):
        """Read an original receipt without preparing or dispatching anything."""
        self._check(project_id, actor)
        _text(original_request_id, 128)
        identity = RequestIdentity("employee_package_publish", project_id, original_request_id, actor,
                                   _sha(dict(package_id=package_id, expected_fingerprint=expected_fingerprint)))
        old = self._authority(project_id, actor).lookup(identity) if self.state.exists() else None
        self._check(project_id, actor)
        if old is None:
            # Absence is not proof of no past side effects. Never authorize a
            # replacement request from this read-only lookup response.
            return {"package_id": package_id, "fingerprint": expected_fingerprint,
                    "original_request_id": original_request_id, "lookup_found": False,
                    "revision": None, "result": "outcome_unknown", "effects_produced": None,
                    "allowed_actions": []}
        result = self._published_reply(old, package_id, expected_fingerprint, original_request_id)
        result.pop("request_id", None)
        return {**result, "original_request_id": original_request_id, "lookup_found": True}

    def publish(self, *, project_id, actor, request_id, package_id, expected_fingerprint, composition_gate=None):
        self._check(project_id, actor)
        _text(request_id, 128)
        identity = RequestIdentity("employee_package_publish", project_id, request_id, actor,
                                   _sha(dict(package_id=package_id, expected_fingerprint=expected_fingerprint)))
        with self.state.assets._lock():
            authority = self._authority(project_id, actor)
            old = authority.lookup(identity)
            if old is not None:
                return self._published_reply(old, package_id, expected_fingerprint, request_id)
            trial, manifest, files, approval = self._evidence(package_id, expected_fingerprint, project_id=project_id, actor=actor)
            from .employee_verifiable_template import apply_formal_publish_gate
            apply_formal_publish_gate(composition_gate)
            if not self.state.has_head(package_id):
                authority.establish_genesis(package_id)
            head = authority.current(package_id)
            stamp = _now()
            published = {**manifest, "status": "published", "published_at": stamp, "published_by": actor}
            plan = FrozenPublication(identity, package_id, manifest["version"], expected_fingerprint, stamp,
                "effect-" + secrets.token_hex(12), head["epoch"], head["tail"], canonical_json(published),
                tuple(ExpectedFile(path, len(raw), _sha(raw)) for path, raw in files.items()),
                "employee_packages/" + package_id, _sha(trial["context"]), trial["trial_id"], trial["candidate"]["revision"])
            prepared = authority.prepare(plan)
            result = authority.dispatch_source_bound(prepared.reference, identity)
            return self._published_reply(result, package_id, expected_fingerprint, request_id)

    @staticmethod
    def _published_reply(status, package_id, fingerprint, request_id):
        committed = status.state == "committed"
        return {"package_id": package_id, "fingerprint": fingerprint, "request_id": request_id,
                "revision": status.after_tail, "result": "success" if committed else "outcome_unknown",
                "effects_produced": True if committed else None,
                "allowed_actions": ["employee_instance_create"] if committed else []}

    def _write_version(self, reference, plan, after_index, snapshot):
        from .employee_native_promotion_windows import create_exclusive_stage, populate_stage, promote_stage
        guard = self.state.assets._root_guard
        if guard is None:
            _fail("employee_lifecycle_lock_required")
        # Native lock creation prepares only the two exact parent chains; no
        # candidate or existing immutable tree is adopted or recursively copied.
        from .ai_employee_safe_write import open_locked
        parent = f"employee_packages/{plan.package_id}"
        for relative in (parent + "/.publication/" + reference.request_key + "/.parent.lock",
                         parent + "/.versions/.parent.lock"):
            with open_locked(self.root, relative):
                pass
        with AIEmployeeSafeFiles(self.root).workspace() as workspace:
            with create_exclusive_stage(workspace, package_id=plan.package_id,
                    request_key=reference.request_key, for_population=True) as stage:
                populate_stage(stage, plan=plan, snapshot=snapshot)
                receipt = promote_stage(stage, fingerprint=plan.fingerprint)
                self._effect_receipts[reference.request_key] = (reference.plan_sha256, plan.effect_id, receipt)
        atomic_write(self.root, parent + "/versions.jsonl", after_index, root_guard=guard)

    def _observe_version(self, reference, plan):
        import os
        original = self._effect_receipts.pop(reference.request_key, None)
        if original is None or original[:2] != (reference.plan_sha256, plan.effect_id):
            _fail("publication_effect_unverified")
        receipt = original[2]
        if (receipt.package_id, receipt.request_key, receipt.fingerprint) != (plan.package_id, reference.request_key, plan.fingerprint):
            _fail("publication_effect_unverified")
        files = AIEmployeeSafeFiles(self.root)
        prefix = f"employee_packages/{plan.package_id}/.versions/{plan.fingerprint}"
        # Keep the native directory pinned across the independent byte reads;
        # matching contents alone cannot impersonate the original promotion.
        with files.workspace() as workspace:
            target = files.target(workspace, prefix)
            handle = workspace._open_directory_handle(target) if os.name == "nt" else workspace.open_directory(target)
            try:
                identity = workspace._identity(handle) if os.name == "nt" else (os.fstat(handle).st_dev, os.fstat(handle).st_ino)
                if identity != receipt._identity:
                    _fail("publication_effect_unverified")
                manifest = files.read(prefix + "/manifest.yaml", 2 * 1024 * 1024)
                observed = tuple(ObservedFile(row.path, files.read(prefix + "/" + row.path, max(1, row.size))) for row in plan.files)
                index = files.read(f"employee_packages/{plan.package_id}/versions.jsonl", 2 * 1024 * 1024)
            finally:
                workspace._close_handle(handle) if os.name == "nt" else os.close(handle)
        return AssetObservation(reference.request_key, reference.plan_sha256, plan.package_id,
                                plan.effect_id, plan.target_scope_ref, index, manifest, observed)


class _Authorization:
    def __init__(self, service, project_id, actor):
        self.service, self.project_id, self.actor = service, project_id, actor

    def genesis(self, package_id):
        self.service._check(self.project_id, self.actor)
        raw = self.service.state.assets.files.read(f"employee_packages/{package_id}/versions.jsonl", 2 * 1024 * 1024)
        if raw is not None:
            _fail("publication_index_unverified")
        return GenesisReceipt(package_id, "employee-lifecycle-empty-v1", 0, None)

    def verify_genesis(self, receipt):
        if receipt != self.genesis(receipt.package_id):
            _fail("publication_index_unverified")
        return receipt

    def freeze(self, plan, plan_sha256):
        self.service._check(self.project_id, self.actor)
        trial, _, _, approval = self.service._evidence(plan.package_id, plan.fingerprint, project_id=self.project_id, actor=self.actor)
        if (plan.request.project_id != self.project_id or plan.request.actor != self.actor
                or plan.evidence_ref != trial["trial_id"] or plan.context_digest != _sha(trial["context"])):
            _fail("publication_authorization_invalid")
        return AuthorizationLease(plan.request, plan.package_id, plan_sha256, plan.context_digest,
                                  plan.evidence_ref, approval["review_epoch"], "review:" + trial["trial_id"])

    def verify(self, plan, lease, phase):
        self.service._check(self.project_id, self.actor)
        # Receipt lookup rechecks current ownership, not a new draft that may
        # legitimately have changed after the immutable publication completed.
        if phase == "lookup":
            return lease
        if self.freeze(plan, lease.plan_sha256) != lease:
            _fail("publication_authorization_invalid")
        return lease


def build_employee_repositories(ampuly_home, *, context_provider=None):
    from .employee_package_catalog import PublishedEmployeeCatalog
    from .employee_skill_tree_reader import EmployeeSkillTreeReader
    from .employee_instances import EmployeeInstanceRepository, EmployeeBindingRepository
    from .model_catalogue import observe_employee_model_choices
    root = Path(ampuly_home)
    state = EmployeeLifecycleStore(root)
    catalog = PublishedEmployeeCatalog(root, current_index_tail=state.current_tail,
                                       read_skill_tree_fingerprint=EmployeeSkillTreeReader(root))
    instances = EmployeeInstanceRepository(root, package_loader=catalog.package_loader,
        context_provider=context_provider, model_catalogue_source=observe_employee_model_choices)
    return catalog, instances, EmployeeBindingRepository(instances)
