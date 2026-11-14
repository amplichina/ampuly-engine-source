#!/usr/bin/env python3
"""Generate a deterministic SPDX 2.3 SBOM for a SuperOnes checkout.

The Python project currently publishes dependency *constraints* rather than a
lock file, while the bundled UI has an npm lock file.  The SBOM therefore
labels Python entries as ``declared`` and uses the exact npm lock entries for
the UI.  This is intentionally dependency-free and suitable for CI/release
artifacts; it never executes package managers or reads credentials.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


SPDX_VERSION = "SPDX-2.3"
CREATOR = "Organization: SuperOnes"
NOASSERTION = "NOASSERTION"


def _normalise_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_name(spec: str) -> str:
    # PEP 508 names end before an optional version/extras/environment marker.
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    if not match:
        raise ValueError(f"invalid dependency declaration: {spec!r}")
    return match.group(1)


def _spdx_id(prefix: str, name: str, version: str | None = None) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-") or "unknown"
    if version:
        safe += "-" + re.sub(r"[^A-Za-z0-9.+-]+", "-", version)
    return f"SPDXRef-{prefix}-{safe}"


def _python_packages(root: Path, pyproject: dict[str, Any]) -> list[dict[str, Any]]:
    project = dict(pyproject.get("project", {}))
    dependencies = list(project.get("dependencies", []) or [])
    packages: list[dict[str, Any]] = []
    for declaration in sorted((str(item) for item in dependencies), key=str.casefold):
        name = _dependency_name(declaration)
        package_name = _normalise_pypi_name(name)
        packages.append(
            {
                "SPDXID": _spdx_id("PyPI", package_name),
                "name": package_name,
                "versionInfo": declaration[len(name) :].strip() or NOASSERTION,
                "downloadLocation": NOASSERTION,
                "licenseConcluded": NOASSERTION,
                "licenseDeclared": NOASSERTION,
                "supplier": NOASSERTION,
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{package_name}",
                    }
                ],
                "annotations": [
                    {
                        "annotationType": "OTHER",
                        "annotator": CREATOR,
                        "annotationDate": "1970-01-01T00:00:00Z",
                        "comment": "source=pyproject.toml; resolution=declared",
                    }
                ],
            }
        )
    return packages


def _npm_packages(lock_path: Path) -> list[dict[str, Any]]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict[str, Any]] = []
    for location, data in sorted((lock.get("packages") or {}).items()):
        if not location or not isinstance(data, dict) or not data.get("version"):
            continue
        # Development-only packages are not part of the browser release.
        if data.get("dev") is True:
            continue
        name = location.removeprefix("node_modules/")
        version = str(data["version"])
        package: dict[str, Any] = {
            "SPDXID": _spdx_id("NPM", name, version),
            "name": name,
            "versionInfo": version,
            "downloadLocation": data.get("resolved", NOASSERTION),
            "licenseConcluded": data.get("license", NOASSERTION),
            "licenseDeclared": data.get("license", NOASSERTION),
            "supplier": NOASSERTION,
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:npm/{name}@{version}",
                }
            ],
        }
        if data.get("integrity"):
            integrity = str(data["integrity"])
            algorithm, _, encoded = integrity.partition("-")
            try:
                checksum = base64.b64decode(encoded, validate=True).hex()
            except (ValueError, base64.binascii.Error):
                checksum = encoded
            package["checksums"] = [
                {"algorithm": algorithm.upper(), "checksumValue": checksum}
            ]
        packages.append(package)
    return packages


def _file_checksum(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_sbom(
    root: Path,
    *,
    created: str = "1970-01-01T00:00:00Z",
    artifact: Path | None = None,
) -> dict[str, Any]:
    import tomllib

    pyproject_path = root / "pyproject.toml"
    lock_path = root / "ampuly" / "plugins" / "office_ui" / "frontend_src" / "package-lock.json"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    packages = _python_packages(root, pyproject) + _npm_packages(lock_path)
    packages.insert(
        0,
        {
            "SPDXID": "SPDXRef-superones-engine",
            "name": str(pyproject.get("project", {}).get("name", "superones-engine")),
            "versionInfo": str(pyproject.get("project", {}).get("version", NOASSERTION)),
            "downloadLocation": NOASSERTION,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "supplier": CREATOR,
        },
    )
    packages.sort(key=lambda item: (str(item["name"]).casefold(), str(item["versionInfo"])))
    files: list[dict[str, Any]] = []
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": "SPDXRef-superones-engine",
        }
    ]
    if artifact is not None:
        artifact_path = artifact.resolve()
        root_path = root.resolve()
        try:
            relative_artifact = artifact_path.relative_to(root_path)
        except ValueError as exc:
            raise ValueError("artifact must be inside the repository root") from exc
        if not artifact_path.is_file():
            raise ValueError(f"artifact is not a file: {artifact_path}")
        relative_name = relative_artifact.as_posix()
        file_id = _spdx_id("File", relative_name)
        files.append(
            {
                "SPDXID": file_id,
                "fileName": relative_name,
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": _file_checksum(artifact_path)}
                ],
                "licenseConcluded": NOASSERTION,
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-superones-engine",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": file_id,
            }
        )
    return {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "superones-engine-release",
        "documentNamespace": "https://superones.dev/sbom/superones-engine-release",
        "creationInfo": {"created": created, "creators": [CREATOR]},
        "packages": packages,
        "relationships": relationships,
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="write SPDX JSON here (stdout by default)")
    parser.add_argument(
        "--artifact",
        type=Path,
        help="optional release artifact inside --root to bind with a SHA-256 checksum",
    )
    parser.add_argument(
        "--created",
        default=os.environ.get("SOURCE_DATE_EPOCH", "1970-01-01T00:00:00Z"),
        help="stable SPDX creation timestamp; defaults to SOURCE_DATE_EPOCH or epoch",
    )
    args = parser.parse_args(argv)
    try:
        document = build_sbom(args.root.resolve(), created=args.created, artifact=args.artifact)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
