"""A21 enterprise freeze: fingerprint from published employee catalog, never draft digest.

Published freeze identity lives under::

    employee_packages/<package_id>/versions.jsonl
    employee_packages/<package_id>/.versions/<fingerprint>/

Draft packages under ``employee_packages/<id>/manifest.yaml`` (no versions index
row) and workflow draft tree digests are explicitly refused here. Owner
publication still goes through employee_package_test_start → review →
employee_package_publish; this module only *reads* an already-published pin.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .ai_employee_contract import AIEmployeeContractError
from .employee_package_catalog import PublishedEmployeeCatalog


def _published_catalog(root: Path) -> PublishedEmployeeCatalog:
    """Build the catalog exactly as the product WS does (ai_employee_ws.catalog_factory).

    Without the native skill-tree reader every published package that carries a
    skill is refused with ``immutable_skill_tree_unverified``; with it, the
    pinned skill tree is re-hashed and a tampered tree is refused. The index
    tail anchor comes from the lifecycle authority store, so an unsettled
    publication never reads as current.
    """
    from .employee_lifecycle_store import EmployeeLifecycleStore
    from .employee_skill_tree_reader import EmployeeSkillTreeReader

    reader = EmployeeSkillTreeReader(root)
    state = EmployeeLifecycleStore(root)
    return PublishedEmployeeCatalog(root, read_skill_tree_fingerprint=reader,
                                    current_index_tail=state.current_tail)


def published_employee_freeze(
    ampuly_home: Path | str,
    package_id: str,
    *,
    version: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Return the freeze record for one published employee package pin.

    The fingerprint is the catalog ``package_fingerprint`` of the published
    version directory, not a mutable draft digest. Raises
    ``AIEmployeeContractError`` when the package is missing, draft-only, or the
    requested pin is not in the published index.
    """
    root = Path(ampuly_home)
    catalog = _published_catalog(root)
    detail = catalog.detail(package_id, version=version)
    package = dict(detail.get("package") or {})
    publication = dict(detail.get("publication") or {})
    status = str(detail.get("status") or package.get("status") or "").strip().lower()
    if status != "published":
        raise AIEmployeeContractError(
            "publication_not_published",
            "冻结只接受企业已发布岗位包，不接受草稿或其它状态。",
        )
    pin = str(detail.get("fingerprint") or package.get("package_fingerprint") or "").strip()
    if not pin or len(pin) != 64:
        raise AIEmployeeContractError("fingerprint_invalid", "已发布岗位包缺少有效指纹。")
    if fingerprint is not None and str(fingerprint).strip() != pin:
        raise AIEmployeeContractError(
            "fingerprint_mismatch",
            "请求的冻结指纹与企业发布目录中的岗位包不一致。",
        )
    version_dir = root / "employee_packages" / package_id / ".versions" / pin
    index_path = root / "employee_packages" / package_id / "versions.jsonl"
    if not version_dir.is_dir() or not index_path.is_file():
        raise AIEmployeeContractError(
            "publication_source_unavailable",
            "企业发布冻结目录不完整（缺 versions.jsonl 或 .versions 针脚）。",
        )
    return {
        "freeze_source": "employee_packages_published",
        "package_id": package_id,
        "version": str(package.get("version") or publication.get("version") or ""),
        "package_fingerprint": pin,
        "status": status,
        "index_relative": f"employee_packages/{package_id}/versions.jsonl",
        "version_relative": f"employee_packages/{package_id}/.versions/{pin}",
        "published_at": str(package.get("published_at") or publication.get("at") or ""),
        "published_by": str(package.get("published_by") or publication.get("actor") or ""),
        "catalog_revision": str(detail.get("revision") or ""),
        # Explicitly not a workflow draft tree digest:
        "draft_digest": None,
    }


def assert_not_draft_digest(freeze: dict[str, Any]) -> None:
    """Fail closed if a caller tries to pass a draft-style freeze record."""
    if not isinstance(freeze, dict):
        raise AIEmployeeContractError("request_invalid", "冻结记录无效。")
    if freeze.get("freeze_source") != "employee_packages_published":
        raise AIEmployeeContractError(
            "freeze_not_enterprise_published",
            "冻结必须来自企业发布员工目录，不能是草稿 digest。",
        )
    if freeze.get("draft_digest"):
        raise AIEmployeeContractError(
            "freeze_not_enterprise_published",
            "冻结记录仍带草稿 digest，拒绝作为企业发布冻结。",
        )
    pin = str(freeze.get("package_fingerprint") or "")
    if len(pin) != 64:
        raise AIEmployeeContractError("fingerprint_invalid", "企业发布冻结指纹无效。")
