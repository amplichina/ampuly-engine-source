"""Bounded shipped-template reader; never reads a customer's knowledge or config."""
from pathlib import Path

from .package_builder import ASSET_NAMES, MAX_ASSET_BYTES, build_customer_service_package


def _read_resource(path: Path) -> bytes:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError("Shipped customer-service resource is missing or linked")
    with path.open("rb") as handle:
        value = handle.read(MAX_ASSET_BYTES + 1)
    if not value or len(value) > MAX_ASSET_BYTES:
        raise ValueError("Shipped customer-service resource exceeds its bound")
    return value


def build_shipped_customer_service_package(*, actor: str, created_at: str, max_cost_cny: str,
                                          structured_understanding: bool = True):
    """Create an in-memory AE00 draft, never save, adopt, publish or execute it.

    Actor/time/budget are supplied by the trusted host. No path or private
    enterprise resource can be passed in. Model verification is still absent.
    """
    root = Path(__file__).parent
    resources = root / "packages/customer-service"
    skill = root / "skills/customer-service"
    for folder in (root, root / "packages", resources, root / "skills", skill):
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Shipped customer-service resource directory is missing or linked")
    return build_customer_service_package(
        assets={name: _read_resource(resources / name) for name in sorted(ASSET_NAMES)},
        skill_bytes=_read_resource(skill / "SKILL.md"), actor=actor,
        created_at=created_at, max_cost_cny=max_cost_cny,
        structured_understanding=structured_understanding,
    )
