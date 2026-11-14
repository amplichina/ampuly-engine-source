"""``ampuly distribution``: the distribution layer as a contract, not a discipline.

Borrowed from what QM does with ``qm init`` / ``deploy/layers``: everything
organisation-specific lives outside the kernel and a tool checks it before
it is committed or shipped.

* ``check`` - validates the distribution layer: no secret-shaped values in
  committed config, every workflow package valid, the component catalog
  within its schema, no upstream brand in
  user-facing copy, NOTICE present, runtime state (``.ampuly/``) not staged;
* ``init <customer>`` - scaffolds ``deploy/customers/<customer>/`` for an
  FDE delivery: which org and packages, model routing, an env example and a
  one-page runbook. Secrets never enter the scaffold.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# Only keys that *name a credential* count (api_key, app_secret, access_token ...);
# max_tokens, external_secret_missing, user_token_read_only do not.
SECRET_KEY_RE = re.compile(r"(api[_-]?key|app[_-]?secret|client[_-]?secret|secret[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|password|passwd|private[_-]?key|credentials?)$", re.IGNORECASE)
SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"^[0-9a-f]{40,}$"),
)
ENV_REFERENCE_RE = re.compile(r"^\$?\{?[A-Z][A-Z0-9_]{2,}\}?$")
# Content hashes are evidence, not credentials: release approval, publication
# and rollout ledgers carry sha256 digests and candidate fingerprints under
# keys like these.  A 64-hex value there is exactly what it claims to be.
HASH_KEY_RE = re.compile(r"(sha256|sha1|sha512|md5|fingerprint|digest|_hash|^hash)$", re.IGNORECASE)
BRAND_RE = re.compile(r"OpenOPC|HKUDS", re.IGNORECASE)
# Places where the upstream name is legitimately present: attribution and the
# upstream theme's own label.
BRAND_ALLOWLIST = ("NOTICE.md", "README.SuperOnes.md", "theme.ampuly", "ampuly_locale", "upstream/", "docs/", "ENGINE-PLAN.md", "CLAUDE.md")


@dataclass
class CheckReport:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _walk(root: Path, suffixes: tuple[str, ...] | None) -> list[Path]:
    """Files under ``root`` with one of ``suffixes``; ``None`` means every file, build folders included."""
    out: list[Path] = []
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if suffixes is None or (path.suffix in suffixes and "node_modules" not in path.parts and "frontend_dist" not in path.parts):
            out.append(path)
    return out


def _secret_shaped(key: str, value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    if HASH_KEY_RE.search(str(key)) and re.fullmatch(r"[0-9a-fA-F]{32,128}", text):
        return False
    if SECRET_KEY_RE.search(str(key)):
        # A reference to an environment variable is the *allowed* shape.
        return not ENV_REFERENCE_RE.match(text) and text.lower() not in {"none", "null", "~", "changeme", "<set-in-env>"}
    return any(r.search(text) for r in SECRET_VALUE_RES)


def _scan_mapping(node: Any, path: str, hits: list[str], *, under_hash: bool = False) -> None:
    """Walk a YAML document; ``under_hash`` marks a mapping keyed by a hash name
    (``evidence_skill_sha256: {skill-id: <digest>}``) whose leaves are digests."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_text = str(key)
            if isinstance(value, (dict, list)):
                _scan_mapping(value, f"{path}.{key}", hits, under_hash=under_hash or bool(HASH_KEY_RE.search(key_text)))
            elif under_hash and re.fullmatch(r"[0-9a-fA-F]{32,128}", str(value or "").strip()):
                continue
            elif _secret_shaped(key_text, value):
                hits.append(f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan_mapping(item, f"{path}[{index}]", hits, under_hash=under_hash)


def check_secrets(
    repo: Path, roots: tuple[str, ...] = ("distribution", "config", "deploy", "upstream"), *, every_file: bool = False,
) -> list[str]:
    """Secret-shaped text under ``roots``. ``every_file`` reads every UTF-8 file whatever its name -- for files
    someone else wrote (a downloaded skill's ``.env``, ``run.py`` or ``README.MD``), where the usual config suffixes
    say nothing about where a key might sit."""
    problems: list[str] = []
    for root in roots:
        for path in _walk(repo / root, None if every_file else (".yaml", ".yml", ".json", ".jsonc", ".md", ".env", ".example")):
            rel = path.relative_to(repo).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if path.suffix.lower() in {".yaml", ".yml"}:
                try:
                    data = yaml.safe_load(text)
                except yaml.YAMLError:
                    data = None
                hits: list[str] = []
                _scan_mapping(data, "", hits)
                problems.extend(f"{rel}: secret-shaped value at {h.lstrip('.')}" for h in hits)
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if any(r.search(stripped) for r in SECRET_VALUE_RES[:4]):
                    problems.append(f"{rel}:{line_no}: looks like a credential")
    return problems


def check_packages(repo: Path) -> tuple[list[str], list[str]]:
    from ampuly.plugins.office_ui.workflow_packages import discover_packages

    problems: list[str] = []
    notes: list[str] = []
    packages = discover_packages(repo, None)
    for package in packages:
        for problem in package.problems:
            problems.append(f"workflow {package.id}: {problem}")
        notes.append(f"workflow {package.id}: {package.status}, {len(package.tests)} run record(s)")
    if not packages:
        notes.append("no workflow packages found")
    return problems, notes


def check_component_catalog(repo: Path) -> tuple[list[str], list[str]]:
    """The 组件库 catalog ships with the distribution, so it is held to its schema too."""
    from ampuly.plugins.office_ui.component_catalog import load_catalog, load_internal_categories

    catalog = load_catalog(repo)
    categories, filed, category_problems = load_internal_categories(repo)
    problems = [f"component catalog: {problem}" for problem in [*catalog.problems, *category_problems]]
    notes = []
    if catalog.connectors or catalog.skills:
        notes.append(f"component catalog: {len(catalog.connectors)} connector(s), {len(catalog.skills)} skill(s), none enabled by the catalog")
    if categories:
        notes.append(f"internal library: {len(filed)} skill(s) filed under {len(categories)} market categories")
    return problems, notes


def check_brand(repo: Path) -> list[str]:
    problems: list[str] = []
    targets = [repo / "ampuly" / "plugins" / "office_ui" / "frontend_src" / "i18n" / "index.tsx", *(_walk(repo / "distribution", (".md", ".yaml")))]
    for path in targets:
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        if any(rel.startswith(a) for a in BRAND_ALLOWLIST):
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("//", "/*", "*", "#")):
                continue  # source comments are not user-facing copy
            if BRAND_RE.search(line) and not any(a in line for a in BRAND_ALLOWLIST):
                problems.append(f"{rel}:{line_no}: upstream brand in user-facing copy")
    return problems


def check_runtime_not_staged(repo: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True, check=False).stdout
    except OSError:
        return []
    staged = [
        line.strip() for line in out.splitlines()
        if line.strip().startswith(".ampuly/") or line.strip().startswith(".opc/")
    ]
    return [f"runtime state staged for commit: {p}" for p in staged]


def check_notice(repo: Path) -> list[str]:
    """The repository's NOTICE, or the copy the wheel ships inside the package."""
    packaged = Path(__file__).resolve().parents[1] / "NOTICE.md"
    if (repo / "NOTICE.md").is_file() or packaged.is_file():
        return []
    return ["NOTICE.md is missing (attribution to the upstream project is mandatory)"]


def check_release_packages(repo: Path) -> tuple[list[str], list[str]]:
    """Archived customer packages must still match their own manifests."""
    from ampuly.cli.release_package import packaged_customers, verify_customer_release

    problems: list[str] = []
    notes: list[str] = []
    for customer in packaged_customers(repo):
        found = verify_customer_release(repo, customer)
        problems.extend(found)
        if not found:
            notes.append(f"release package {customer}: archive and SBOM match the manifest")
    return problems, notes


def run_check(repo: Path) -> CheckReport:
    report = CheckReport()
    report.problems.extend(check_secrets(repo))
    package_problems, package_notes = check_packages(repo)
    report.problems.extend(package_problems)
    report.notes.extend(package_notes)
    catalog_problems, catalog_notes = check_component_catalog(repo)
    report.problems.extend(catalog_problems)
    report.notes.extend(catalog_notes)
    report.problems.extend(check_brand(repo))
    report.problems.extend(check_runtime_not_staged(repo))
    report.problems.extend(check_notice(repo))
    release_problems, release_notes = check_release_packages(repo)
    report.problems.extend(release_problems)
    report.notes.extend(release_notes)
    return report


# ── customer scaffold ──────────────────────────────────────────────────────

_CUSTOMER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


def init_customer(repo: Path, customer: str, *, packages: list[str], org_id: str, model: str = "deepseek/deepseek-chat") -> Path:
    if not _CUSTOMER_ID_RE.match(customer):
        raise ValueError("customer id must be lowercase letters, digits, '-' or '_' (2-41 chars)")
    root = repo / "deploy" / "customers" / customer
    if root.exists():
        raise FileExistsError(f"{root} already exists")
    root.mkdir(parents=True)
    (root / "customer.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": 1,
                "customer": customer,
                "created": date.today().isoformat(),
                "org_id": org_id,
                "workflow_packages": list(packages),
                "model": {"default": model, "routing": {}},
                "gates": {"posture": "strict"},
                "notes": "配置里只放非密钥值；密钥只走 .env（不入库）或系统环境变量。",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".env.example").write_text(
        "# 复制为 .env 后填写；.env 已被 .gitignore 挡住，永远不入库。\n"
        "DEEPSEEK_API_KEY=\n"
        "DASHSCOPE_API_KEY=\n"
        "AMPULY_NOTIFY_FEISHU_APP_ID=\n"
        "AMPULY_NOTIFY_FEISHU_APP_SECRET=\n"
        "AMPULY_NOTIFY_FEISHU_TO=\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"# 客户部署：{customer}\n\n"
        f"- 组织：`{org_id}`\n"
        f"- 工作流包：{', '.join(packages) or '（待选）'}\n"
        f"- 模型：`{model}`\n\n"
        "## 上线步骤\n\n"
        "1. 在客户机器上装好 Python 与本仓库，`.venv/Scripts/ampuly trust add <项目路径>`。\n"
        "2. 复制 `.env.example` 为 `.env`，填密钥；或写进系统环境变量。\n"
        "3. `ampuly distribution check` 全绿后 `ampuly ui` 启动；设置页创建老板账号。\n"
        "4. 名录里加人、开子账号；搭建台里试跑一单，写实测记录。\n\n"
        "## 回滚\n\n"
        "停 `ampuly ui`，`ampuly maintenance` 压缩运行库，恢复 `.backups/` 里最近一份压缩备份。\n",
        encoding="utf-8",
    )
    return root
