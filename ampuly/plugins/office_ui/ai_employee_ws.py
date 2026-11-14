"""Owner-only AE asset host bridge, with no runtime/delegate construction.

The normal WS router has already opened its account store. This bridge never
calls its constructing getter, ConsoleAPI, a project engine or lifecycle store
factory. Missing host dependencies fail closed. Project membership beyond the
existing owner contract is deliberately not guessed from customer visibility.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ampuly.project_id import is_valid_project_id
from .ai_employee_actions import AuthenticatedActor, AuthorizedProject, EmployeeActionDispatcher
from .ai_employee_contract import AIEmployeeContractError
from .ai_employee_safe_files import AIEmployeeSafeFiles
from .employee_instances import EmployeeBindingRepository, EmployeeInstanceRepository
from .employee_packages import EmployeePackageRepository
from .employee_saved_workflow import SavedWorkflowBindingLoader
from .model_catalogue import observe_employee_model_choices


_WORKFLOW_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class _Lazy:
    """Construct file repositories only after dispatcher authentication/scope."""
    def __init__(self, factory: Callable, methods: tuple[str, ...]) -> None:
        self.factory = factory
        self.methods = methods
        self.value = None

    def __getattr__(self, name: str) -> Callable:
        if name not in self.methods:
            raise AttributeError(name)

        def call(*args, **kwargs):
            return getattr(self._get(), name)(*args, **kwargs)

        return call

    def _get(self) -> Any:
        if self.value is None:
            self.value = self.factory()
        return self.value

    @property
    def model_configuration_available(self) -> bool:
        # Only queried after an authorized instance list/detail already opened
        # this repository. A missing source never advertises new selection.
        return getattr(self._get(), "model_configuration_available", False) is True


class _BindingResolutionPreview:
    """Current read previews and explicit guarded map selection, never launch.

    source='single_candidate' can also occur in an old persisted map. Its
    presence is checked directly, not inferred from the source display value.
    """
    def __init__(self, repository: EmployeeBindingRepository) -> None:
        self.repository = repository

    def candidates(self, *args, **kwargs) -> dict:
        return self.repository.candidates(*args, **kwargs)

    def resolve_set(self, *args, **kwargs) -> dict:
        return self.repository.resolve_set(*args, **kwargs)

    def preview(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                binding_slot: str = "executor") -> dict:
        repository = self.repository
        table, before = repository._load(project_id)
        try:
            result = repository.preview(project_id, binding, workflow_id=workflow_id,
                                        node_id=node_id, binding_slot=binding_slot)
            current = repository.instances._load(project_id, result["instance_id"])[0]
            if (current["instance_revision"] != result["instance_revision"]
                    or current["revision_fingerprint"] != result["revision_fingerprint"]
                    or result["binding_revision"] != table["binding_revision"]):
                raise AIEmployeeContractError("storage_record_invalid", "岗位读取期间发生变化。")
            matched = [row for row in table["bindings"] if
                (row["workflow_id"], row["node_id"], row["binding_slot"]) == (workflow_id, node_id, binding_slot)]
            if matched and (len(matched) != 1 or matched[0]["instance_id"] != result["instance_id"]):
                raise AIEmployeeContractError("storage_record_invalid", "岗位映射读取期间发生变化。")
            return {**result, "resolution_basis": "stored_mapping" if matched else "single_candidate"}
        finally:
            if repository._load(project_id)[1] != before:
                raise AIEmployeeContractError("storage_record_invalid", "岗位映射读取期间发生变化。")


def dispatch_owner_employee_action(handler: Any, socket: Any, message: Any,
                                   *, actor_source: Callable, server_loop=None) -> dict:
    """One bounded sync call, invoked in the host's worker thread.

    actor_source is the server's existing socket resolver, never browser data.
    Replays pass through auth/project checks again. Asset reads construct no
    running engine or database. Only an explicit lifecycle action uses the
    already installed host and captured server loop for a finite test job.
    """
    accounts = getattr(handler, "_account_store", None)
    context = getattr(handler, "services_context", None)

    def root() -> Path:
        value = getattr(context, "ampuly_home", None)
        if not isinstance(value, (Path, str)) or not str(value) or not Path(value).is_absolute():
            raise AIEmployeeContractError("storage_path_invalid", "岗位资产根尚未核实。")
        return Path(os.path.abspath(value))

    def authenticate(session: Any) -> AuthenticatedActor | None:
        if session is not socket or accounts is None:
            return None
        home = root()
        business = getattr(accounts, "business", None)
        database_path = getattr(business, "path", None)
        if (Path(getattr(accounts, "home", "")) != home
                or not isinstance(database_path, (str, Path))
                or Path(database_path) != home / "business.db" or not Path(database_path).is_file()):
            return None  # Do not query a foreign/missing pre-existing authority.
        mapped = (getattr(handler, "_client_accounts", None) or {}).get(socket)
        if mapped is not None:
            # The socket account is a login hint, not a current privilege grant.
            # AccountStore.get reads its existing durable source and constructs
            # no store; all asset actions/replays share this one fresh query.
            login = getattr(mapped, "login", None)
            lookup = getattr(accounts, "get", None)
            if type(login) is not str or not login or not callable(lookup):
                return None
            current = lookup(login)
            current_login = getattr(current, "login", None)
            customer_scope = getattr(current, "customer_ids", None)
            if (type(current_login) is not str or current_login.lower() != login.lower()
                    or getattr(current, "role", None) != "owner"
                    or getattr(current, "disabled", None) is not False
                    or type(customer_scope) not in {tuple, list} or customer_scope):
                return None
            return AuthenticatedActor(current_login)
        actor = actor_source(handler, socket)
        required = getattr(accounts, "auth_required", None)
        if (type(actor) is not dict or actor.get("kind") != "local_console_owner"
                or actor.get("role") != "owner" or not callable(required) or required() is not False):
            return None
        login = actor.get("login")
        return AuthenticatedActor(login) if type(login) is str else None

    def resolve_project(actor: AuthenticatedActor, project_id: str) -> AuthorizedProject | None:
        if not is_valid_project_id(project_id) or len(project_id) > 128:
            return None
        home = root()
        # The pre-existing account store is the lifecycle source. No call to
        # OfficeServiceContext._lifecycle_store, whose constructor migrates.
        if Path(getattr(accounts, "home", "")) != home:
            return None
        business = getattr(accounts, "business", None)
        lookup = getattr(business, "project_lifecycle", None)
        database_path = getattr(business, "path", None)
        if (not callable(lookup) or not isinstance(database_path, (str, Path))
                or Path(database_path) != home / "business.db" or not Path(database_path).is_file()):
            return None
        state = lookup(project_id)
        status = getattr(state, "status", None)
        if getattr(state, "project_id", None) != project_id or status not in {"active", "archived"}:
            return None
        # A missing non-default project is not implicitly created on a read or
        # saved into existence by draft creation. Enumeration is bounded/native.
        if project_id != "default":
            rows = AIEmployeeSafeFiles(home).list_entries("projects", limit=2001)
            if not any(row == {"name": project_id, "kind": "directory"} for row in rows):
                return None
        return AuthorizedProject(project_id, True, allow_instance_revise=status == "active",
                                 allow_binding_selection=status == "active")

    def catalog_factory():
        from .employee_package_catalog import PublishedEmployeeCatalog
        from .employee_skill_tree_reader import EmployeeSkillTreeReader
        from .employee_lifecycle_store import EmployeeLifecycleStore
        home = root()
        # One native provider per authenticated request: aggregate read limits
        # span all skill references/catalog rechecks, never multiple RPCs.
        reader = EmployeeSkillTreeReader(home)
        state = EmployeeLifecycleStore(home)
        return PublishedEmployeeCatalog(home, read_skill_tree_fingerprint=reader,
            current_index_tail=state.current_tail)

    catalog = _Lazy(catalog_factory, ("list", "detail", "read"))

    def package_loader(package_id: str, version: str, fingerprint: str) -> dict:
        view = catalog.read(package_id, version=version, fingerprint=fingerprint, purpose="pinned")
        return {"manifest": view["package"], "fingerprint": view["fingerprint"],
                "status": view["status"], "new_adoption_allowed": view["new_adoption_allowed"],
                "is_current_verified": view["is_current_verified"], "revision": view["revision"]}

    def runtime_host():
        # Existing engines only: no engine_for_project/constructing getters on
        # a read, no active-project substitution for a different project.
        target_project = message.get("project_id") if type(message) is dict else None
        for engine in (getattr(context, "active_engine", None), getattr(context, "root_engine", None)):
            if (engine is None or getattr(engine, "project_id", None) != target_project
                    or Path(getattr(engine, "ampuly_home", "")) != root()):
                continue
            host = getattr(engine, "employee_runtime_host", None)
            if host is not None and getattr(host, "engine", None) is engine:
                return host
        return None

    def instances_factory():
        host = runtime_host()
        if host is not None:
            return host.instances
        return EmployeeInstanceRepository(root(), package_loader=package_loader,
            model_catalogue_source=observe_employee_model_choices)

    instances = _Lazy(instances_factory,
        ("list", "detail", "revise", "revise_limits", "adoption_preview", "model_candidates", "revise_model_config", "create", "adopt"))
    bindings = _Lazy(lambda: _BindingResolutionPreview(EmployeeBindingRepository(instances._get())),
                     ("preview", "candidates", "resolve_set"))

    def binding_loader(scope: AuthorizedProject, workflow_id: str, node_id: str, expected_workflow_revision: str):
        # Both roots originate in installed server code/context. Browser facts,
        # active-project fallbacks and constructing workflow APIs are not used.
        loader = SavedWorkflowBindingLoader(repository_root=_WORKFLOW_REPOSITORY_ROOT, ampuly_home=root())
        return loader.load(scope.project_id, workflow_id, node_id,
                           expected_workflow_revision=expected_workflow_revision)

    def lifecycle_factory():
        from .employee_lifecycle import EmployeeLifecycle
        from .employee_lifecycle_actions import LIFECYCLE_WRITE
        original = authenticate(socket)
        if original is None:
            raise AIEmployeeContractError("owner_required", "当前所有者身份不可用。")
        original_project = message.get("project_id") if type(message) is dict else None
        writing = message.get("type") in LIFECYCLE_WRITE

        def authorize(project_id, actor):
            current = authenticate(socket)
            if (current is None or current.actor != original.actor or actor != current.actor
                    or project_id != original_project):
                raise AIEmployeeContractError("owner_required", "所有者身份已变化。")
            scope = resolve_project(current, project_id)
            if (scope is None or scope.is_owner is not True
                    or writing and scope.allow_instance_revise is not True):
                raise AIEmployeeContractError("not_allowed_in_status", "当前项目不能进行此操作。")

        runner = None
        # Detail/receipt/review/publication needs no model provider or engine.
        # A test must use the already installed per-project host.
        if message.get("type") in {"employee_package_test_prepare", "employee_package_test_start"}:
            host = runtime_host()
            if host is not None and server_loop is not None:
                from .employee_trial_runner import EmployeeTrialRunner
                from ampuly.components.customer_service.employee_executor import resolve_trial_execution
                runner = EmployeeTrialRunner(engine=host.engine, host=host, loop=server_loop,
                    resolve_executor=resolve_trial_execution)
        return EmployeeLifecycle(root(), authorize=authorize, runner=runner)

    lifecycle = _Lazy(lifecycle_factory,
        ("prepare", "start", "trial_detail", "lookup_trial", "review", "publish", "lookup_publication", "history", "abandon_trial"))

    def knowledge_factory():
        from .employee_instance_knowledge import EmployeeInstanceKnowledge
        host = runtime_host()
        source = getattr(host, "knowledge_source", None) if host is not None else None
        if source is None or getattr(source, "engine", None) is not host.engine:
            raise AIEmployeeContractError("knowledge_source_unavailable", "Existing project knowledge source is required")
        return EmployeeInstanceKnowledge(repository=instances._get(), source=source)

    knowledge = _Lazy(knowledge_factory, ("candidates", "select"))

    dispatcher = EmployeeActionDispatcher(
        authenticate=authenticate,
        resolve_project=resolve_project,
        draft_repository=_Lazy(lambda: EmployeePackageRepository(root()), ("list", "detail", "create", "save", "create_from_template")),
        published_catalog=catalog,
        instance_repository=instances,
        binding_repository=bindings,
        binding_loader=binding_loader,
        lifecycle_service=lifecycle,
        knowledge_service=knowledge,
        # This is saved-node/current-instance resolution only, never a frozen
        # historical execution view, new adoption or permission to run.
    )
    return dispatcher.dispatch(message, session=socket)


