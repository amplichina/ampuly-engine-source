"""Service acceptance over the existing Engine Provider, budget and identity.

No migration, alternative model runtime, automatic channel activation or wire
passed flags. Knowledge and account-specific admission are separate from the
existing employee package's generic acceptance.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import stat
import time
from types import SimpleNamespace
from uuid import uuid4

from ampuly.components.customer_service.core.admission import AdmissionRequest, verify_evidence
from ampuly.components.customer_service.core.autonomy import CurrentBasis
from ampuly.components.customer_service.core.contracts import Context, canonical_json
from ampuly.components.customer_service.host_knowledge import ServiceKnowledge


def digest(value):
    return sha256(canonical_json(value)).hexdigest()


def policy_snapshot(instructions):
    if type(instructions) is not str or not 1 <= len(instructions) <= 4000:
        raise ValueError("customer_service_rules_invalid")
    import ampuly.components.customer_service as package
    root = Path(package.__file__).parent
    names = ("core/autonomy.py", "core/understanding.py", "core/conversation.py", "core/admission.py",
             "core/acceptance.py", "core/knowledge.py", "core/autonomous_runtime.py",
             "core/api_acceptance.py", "core/delivery.py", "core/runtime.py",
             "autonomous_service.py", "model_adapter.py", "host_knowledge.py", "service.py")
    hashes = {}
    for name in names:
        path = root / name
        if path.is_symlink() or path.stat().st_size > 512 * 1024:
            raise ValueError("customer_service_implementation_unavailable")
        hashes[name] = sha256(path.read_bytes()).hexdigest()
    hashes["console/host.py"] = sha256(Path(__file__).read_bytes()).hexdigest()
    for name in ("wecom.py", "wecom_adapter.py", "bindings.py"):
        hashes["console/" + name] = sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
    relay = root.parents[1] / "plugins" / "wecom_service" / "wecom_relay_client.py"
    hashes["wecom/relay.py"] = sha256(relay.read_bytes()).hexdigest()
    return {"version": "cs-autonomy-v1", "instructions": instructions, "implementation_sha256": digest(hashes)}


class ServiceHost:
    def __init__(self, console, ws, project_id):
        self.console, self.ws, self.project_id = console, ws, project_id
        self.engine, self.business, self.actor, self.home = console.customer_service_context(ws, project_id)
        self.home = Path(self.home)
        self._authority_identity = self._identities()

    def _identities(self):
        from ampuly.plugins.office_ui.extension_host import _customer_service_path_identity
        paths = [(self.home, True), (self.home / "business.db", False)]
        if self.project_id != "default":
            paths.append((self.home / "projects" / self.project_id, True))
        return tuple(_customer_service_path_identity(path, directory=directory) for path, directory in paths)

    def check(self):
        engine, business, actor, home = self.console.customer_service_context(self.ws, self.project_id)
        if (engine is not self.engine or business is not self.business or actor != self.actor
                or Path(home) != self.home or self._identities() != self._authority_identity):
            raise ValueError("customer_service_authority_changed")

    def outbox_path(self):
        path = self.storage() / "outbox-v3.sqlite"
        for suffix in ("", ".owner", "-journal", "-wal", "-shm"):
            entry = Path(str(path) + suffix)
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError("customer_service_linked_storage")
            if suffix in {"-journal", "-wal", "-shm"} or info.st_size > 32 * 1024 * 1024:
                raise ValueError("customer_service_store_busy_or_oversized")
        return path

    def storage(self, *, create=False):
        self.check()
        current = self.home
        for ancestor in (current, *current.parents):
            if ancestor.is_symlink() or ancestor.is_junction():
                raise ValueError("customer_service_linked_storage")
        for name in ("customer_service", self.project_id):
            current = current / name
            if not current.exists() and create:
                current.mkdir()  # One attempt; no migration, retry or recursion.
            if current.is_symlink() or current.is_junction() or not current.is_dir():
                raise ValueError("customer_service_storage_unavailable")
        return current

    def ledger(self, *, initialize=False):
        from ampuly.components.customer_service.core.acceptance import AcceptanceLedger
        path = self.storage(create=initialize) / "acceptance.sqlite"
        return AcceptanceLedger(path, authorize=self.check, initialize=initialize and not path.exists())

    def scope(self, channel, account_id, *, sending=False):
        self.check()
        if channel == "console":
            if account_id != "console":
                raise ValueError("customer_service_account_invalid")
        elif channel == "wecom_cs":
            from ampuly.plugins.wecom_service.wecom_kf_store import WecomKfStore
            row = WecomKfStore(self.business).account(account_id)
            if not row or row["project_id"] != self.project_id or sending and row.get("enabled") is not True:
                raise ValueError("customer_service_account_unavailable")
        else:
            raise ValueError("customer_service_channel_not_integrated")
        return {"tenant_id": "install-" + sha256(str(self.home).casefold().encode()).hexdigest()[:24],
                "shop_id": self.project_id, "channel": channel, "account_id": account_id}

    def package(self, reference):
        self.check()
        if type(reference) is not dict or set(reference) != {"package_id", "version", "fingerprint"}:
            raise ValueError("customer_service_package_invalid")
        from ampuly.plugins.office_ui.employee_package_catalog import PublishedEmployeeCatalog
        from ampuly.plugins.office_ui.employee_skill_tree_reader import EmployeeSkillTreeReader
        from ampuly.plugins.office_ui.employee_lifecycle_store import EmployeeLifecycleStore
        from ampuly.components.customer_service.employee_executor import _snapshot
        state = EmployeeLifecycleStore(self.home)
        catalog = PublishedEmployeeCatalog(self.home, current_index_tail=state.current_tail,
            read_skill_tree_fingerprint=EmployeeSkillTreeReader(self.home))
        view, files = catalog.read_with_files(reference["package_id"], version=reference["version"],
            fingerprint=reference["fingerprint"], purpose="pinned")
        if view.get("is_current_verified") is not True or view.get("new_adoption_allowed") is not True:
            raise ValueError("customer_service_package_not_available")
        manifest = view["package"]
        profile, prompt = _snapshot(manifest, files)
        if profile["version"] != "customer-service-conversation-draft-v2":
            raise ValueError("customer_service_structured_package_required")
        runtime_host = getattr(self.engine, "employee_runtime_host", None)
        if runtime_host is None or runtime_host.engine is not self.engine:
            raise ValueError("customer_service_runtime_unavailable")
        model_profile = runtime_host.context_for_manifest(manifest)
        model = model_profile["model_config"]["model"]
        if not model.partition("/")[2].lower().startswith("deepseek"):
            raise ValueError("customer_service_configure_engine_deepseek")
        provider = self.engine.llm.for_employee_model(model, customer_id="", environment_id="", params=None)
        return manifest, prompt, model_profile, provider

    def current_basis(self, suite):
        self.check()
        # These original acceptance actors must still be this freshly checked
        # owner. A new owner cannot revive a former owner's retained evidence.
        if (type(suite) is not dict or suite.get("actor") != self.actor
                or type(suite.get("reviews")) is not dict
                or any(type(review) is not dict or review.get("actor") != self.actor
                       for review in suite["reviews"].values())):
            raise ValueError("customer_service_acceptance_actor_changed")
        request = AdmissionRequest.from_dict(suite["request"])
        if self.scope(request.channel, request.account_id) != suite["request"]["scope"]:
            raise ValueError("customer_service_scope_changed")
        artifacts = suite["artifacts"]
        _, _, model, _ = self.package(artifacts["package"])
        knowledge = ServiceKnowledge(artifacts["knowledge"])
        policy = policy_snapshot(artifacts["policy_rules"]["instructions"])
        basis = CurrentBasis(artifacts["package"]["fingerprint"], knowledge.config_hash, digest(model), digest(policy))
        if any(getattr(basis, key) != value for key, value in request.as_dict()["basis"].items()):
            raise ValueError("customer_service_accepted_basis_changed")
        return basis

    def prepare(self, data):
        from ampuly.components.customer_service.core.acceptance import AcceptanceCase
        allowed = {"package", "knowledge", "instructions", "channel", "account_id", "valid_until", "allowed_intents",
                   "max_clarification_turns", "authorized_reply_actions", "max_auto_messages", "cases"}
        if type(data) is not dict or set(data) != allowed or len(canonical_json(data)) > 2 * 1024 * 1024:
            raise ValueError("customer_service_prepare_invalid")
        scope = self.scope(data["channel"], data["account_id"])
        manifest, _, model, _ = self.package(data["package"])
        raw = deepcopy(data["knowledge"])
        if type(raw) is not dict:
            raise ValueError("customer_service_knowledge_invalid")
        raw.update(mode="service_snapshot", tenant_id=scope["tenant_id"], shop_id=scope["shop_id"])
        knowledge, policy, now = ServiceKnowledge(raw), policy_snapshot(data["instructions"]), int(time.time())
        if type(data["valid_until"]) is not int or not now < data["valid_until"] <= now + 30 * 86400:
            raise ValueError("customer_service_admission_period_invalid")
        request = AdmissionRequest(admission_id="cs-" + uuid4().hex, **scope,
            package_sha256=manifest["package_fingerprint"], knowledge_sha256=knowledge.config_hash,
            model_sha256=digest(model), policy_sha256=digest(policy), valid_from=now, valid_until=data["valid_until"],
            allowed_intents=data["allowed_intents"], max_clarification_turns=data["max_clarification_turns"],
            authorized_reply_actions=data["authorized_reply_actions"], max_auto_messages=data["max_auto_messages"])
        if type(data["cases"]) is not list or not 3 <= len(data["cases"]) <= 32:
            raise ValueError("customer_service_cases_invalid")
        cases = []
        for index, row in enumerate(data["cases"]):
            if type(row) is not dict or set(row) - {"case_id", "turns", "expected_action", "expected_intents",
                    "required_reason_codes", "required_source_ids", "clarification_turns"}:
                raise ValueError("customer_service_case_invalid")
            ctx = Context(**scope, seller_id=scope["account_id"], buyer_id=f"acceptance-{index}",
                          conversation_id=f"acceptance-{index}")
            cases.append(AcceptanceCase(context=ctx, **row))
        ledger = self.ledger(initialize=True)
        try:
            return ledger.begin_suite(request, cases, self.actor, artifacts={"knowledge": knowledge.export(),
                "package": deepcopy(data["package"]), "policy_rules": policy, "model_profile": model},
                label="prelaunch-model-test")
        finally:
            ledger.close()

    async def run_tests(self, ledger, suite_id, budget):
        from ampuly.components.customer_service.core.acceptance import AcceptanceCase
        from ampuly.components.customer_service.service import CustomerServiceDraftSession
        from ampuly.plugins.office_ui.employee_trial_runner import EmployeeTrialRunner
        from ampuly.llm.budget import model_budget_scope
        suite = ledger.get_suite(suite_id)
        self.current_basis(suite)
        manifest, prompt, _, provider = self.package(suite["artifacts"]["package"])
        prompt += "\n\n本服务的话术原则（不能扩大权限或虚构客户事实）：\n" + suite["artifacts"]["policy_rules"]["instructions"]
        if len(prompt.encode("utf-8")) > 32000:
            raise ValueError("customer_service_prompt_too_large")
        cases = [AcceptanceCase.from_dict(case) for case in suite["cases"]]
        validator = EmployeeTrialRunner(engine=self.engine, host=self.engine.employee_runtime_host,
            loop=asyncio.get_running_loop(), resolve_executor=lambda *args: None)
        limits = validator.validate_budget(manifest, budget, len(cases))
        if suite["revoked"] or any(value != "pending" for value in suite["case_states"].values()):
            raise ValueError("customer_service_test_already_started")
        opened = self.business.create_run_budget(project_id=self.project_id, customer_id="", environment_id="",
            order_id="", limits=limits, actor=self.actor)
        budget_id = opened.get("budget_id")
        if type(budget_id) is not str or not budget_id:
            raise ValueError("customer_service_budget_unavailable")
        deadline, active = time.monotonic() + budget["max_wall_clock_s"], None
        try:
            for case in cases:
                self.current_basis(suite)
                ledger.mark_started(suite_id, case.case_id)
                active = case.case_id
                remaining = min(30, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("customer_service_test_deadline")
                def check():
                    self.current_basis(suite)
                    current = ledger.get_suite(suite_id)
                    if current.get("revoked") or time.monotonic() >= deadline:
                        raise ValueError("customer_service_test_stopped")
                task = SimpleNamespace(project_id=self.project_id, metadata={"budget_id": budget_id,
                    "ampuly_home": str(self.home), "customer_service_suite_id": suite_id})
                draft = CustomerServiceDraftSession(context=case.context,
                    knowledge=ServiceKnowledge(suite["artifacts"]["knowledge"]), provider=provider,
                    before_dispatch=check, system_prompt=prompt, max_output_tokens=validator._output_cap(provider),
                    structured_understanding=True)
                with model_budget_scope(task):
                    candidate = await draft.prepare(case.turns, timeout_seconds=remaining)
                check()
                ledger.record_case(suite_id, case.case_id, candidate, draft.model_observation)
                active = None
        except BaseException:
            if active is not None:
                try:
                    ledger.record_failure(suite_id, active, status="unknown", reason_code="model_run_interrupted")
                except Exception:
                    pass  # Pending is also permanently non-admissible.
            raise
        finally:
            self.business.close_run_budget(budget_id, actor=self.actor,
                reason="customer_service_acceptance_finished", release_held=False)

    def admit(self, ledger, suite_id):
        from ampuly.components.customer_service.core.autonomous_runtime import AutonomousRuntime
        suite = ledger.get_suite(suite_id)
        self.current_basis(suite)
        request, refs = AdmissionRequest.from_dict(suite["request"]), ledger.references(suite_id)
        if not request.valid_from <= int(time.time()) < request.valid_until:
            raise ValueError("customer_service_admission_expired")
        verify_evidence(request, **refs, evidence_reader=ledger.evidence_reader)
        def current(context):
            latest = self.current_basis(ledger.get_suite(suite_id))
            if {key: getattr(context, key) for key in suite["request"]["scope"]} != suite["request"]["scope"]:
                raise ValueError("customer_service_scope_changed")
            return latest
        runtime = AutonomousRuntime(self.outbox_path(), evidence_reader=ledger.evidence_reader,
            basis_reader=current, before_send=lambda command: self.check(), allowed_versions=("cs-host-1",))
        try:
            runtime.db.execute("PRAGMA max_page_count=8192")
            runtime.issue_admission(request, **refs)
        finally:
            runtime.close()
        return {"admission_id": request.admission_id, "request_sha256": request.digest,
                "status": "admitted", "channel_enabled": False,
                "message": "验收准入已记录；没有启用真实账号或发送消息。"}
