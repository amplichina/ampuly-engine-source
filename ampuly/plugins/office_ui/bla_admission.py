"""G1 BLA (bla-agents) readonly admission — default-off.

Locks MIT notice, reviewed commit, deps, and npm package integrity for the
GitHub BLA anti-REA hardening track. Does **not** install, run setup MCP, or
run harden. Drift reporting and install-class veto remain read-only.
Public synthetic planning fixtures live under examples/isolation/.
"""
from __future__ import annotations

from typing import Any, Mapping

RULES_VERSION = "g1-bla-admission-v1"
G2_RULES_VERSION = "g2-bla-drift-gate-v1"
G3_RULES_VERSION = "g3-bla-boundary-v1"
G4_RULES_VERSION = "g4-bla-gray-release-v1"

# Frozen admission fields for the reviewed upstream revision.
# Updating these values requires a new upstream review.
REVIEWED_COMMIT = "71cf950485bb267bb13a7c0405ef05ddab65b559"
LICENSE_SPDX = "MIT"
LICENSE_SHA256 = "54f971eb44dc3be0be67b6c0518c62b60f0d70dab19bbd0cf3cb2456f0fb8321"

# Frozen upstream package metadata; this does not authorize installation.
_LOCK: dict[str, Any] = {
    "schema_version": 1,
    "rules_version": RULES_VERSION,
    "source_repo": "https://github.com/qybaihe/bla",
    "reviewed_commit": REVIEWED_COMMIT,
    "reviewed_commit_short": "71cf950",
    "license": {
        "spdx": "MIT",
        "notice_required": True,
        "copyright": "Copyright (c) 2026 BLA Contributors",
        "license_sha256": (
            "54f971eb44dc3be0be67b6c0518c62b60f0d70dab19bbd0cf3cb2456f0fb8321"
        ),
    },
    "source_package_json": {
        "name": "bla-agents",
        "version_at_commit": "1.5.2",
        "dependencies": {
            "@babel/generator": "^7.26.0",
            "@babel/parser": "^7.26.0",
            "@babel/traverse": "^7.26.0",
            "@babel/types": "^7.26.0",
            "zod": "^3.24.0",
        },
    },
    "npm_published": {
        "dist_tag_latest": "1.1.0",
        "version": "1.1.0",
        "integrity": (
            "sha512-UgRhP5Yd2En/DyBab3Yh6kSrNWtm9xYI8OoTN6Hg0Nl8q6ubn3j7NaVstBlI/"
            "TvMhby1/9C/1aqY5FHf6No76A=="
        ),
        "shasum": "699a7d752621bfab213e4d8bdceffaabfc561a68",
        "tarball": "https://registry.npmjs.org/bla-agents/-/bla-agents-1.1.0.tgz",
        "version_drift_vs_source": True,
    },
    "install_side_effects": {
        "setup_registers_mcp": True,
        "setup_writes_agent_config": True,
        "harden_mutates_artifacts": True,
        "auto_setup_forbidden_in_g1": True,
        "auto_harden_forbidden_in_g1": True,
    },
    "admission": {
        "rights_ok": True,
        "mit_notice_locked": True,
        "commit_locked": True,
        "deps_reviewed": True,
        "package_integrity_locked": True,
        "auto_mcp_setup": False,
        "auto_full_harden": False,
    },
}


def apply_bla_admission_gate(*, enabled: bool = False) -> bool:
    """Safety gate: G1 admission APIs stay inert unless explicitly enabled."""
    return bool(enabled)


def get_bla_lock_snapshot() -> dict[str, Any]:
    """Return a shallow-copied frozen lock snapshot (no I/O)."""
    import copy

    return copy.deepcopy(_LOCK)


def bla_admission_read_api(*, enabled: bool = False) -> dict[str, Any]:
    """Readonly admission payload. Default-off: empty admission when disabled."""
    if not apply_bla_admission_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": RULES_VERSION,
            "reason": "bla_admission_gate_off",
            "admission": None,
            "actions_forbidden": {
                "auto_mcp_setup": True,
                "auto_full_harden": True,
                "install": True,
            },
        }
    lock = get_bla_lock_snapshot()
    adm = lock["admission"]
    drift = bool(lock["npm_published"].get("version_drift_vs_source"))
    gaps: list[str] = []
    if drift:
        gaps.append("npm_version_drift_source_1_5_2_vs_published_1_1_0")
    return {
        "ok": True,
        "enabled": True,
        "rules_version": RULES_VERSION,
        "reason": "admitted_readonly",
        "reviewed_commit": lock["reviewed_commit"],
        "license_spdx": lock["license"]["spdx"],
        "license_notice_required": lock["license"]["notice_required"],
        "npm_integrity": lock["npm_published"]["integrity"],
        "npm_version": lock["npm_published"]["version"],
        "source_version_at_commit": lock["source_package_json"]["version_at_commit"],
        "admission": dict(adm),
        "gaps": gaps,
        "actions_forbidden": {
            "auto_mcp_setup": True,
            "auto_full_harden": True,
            "install": True,
        },
    }


def assert_no_auto_side_effects(payload: Mapping[str, Any] | None = None) -> bool:
    """Contract: G1 never authorizes MCP setup or full harden."""
    p = payload if payload is not None else bla_admission_read_api(enabled=True)
    forbidden = p.get("actions_forbidden") or {}
    return (
        forbidden.get("auto_mcp_setup") is True
        and forbidden.get("auto_full_harden") is True
        and forbidden.get("install") is True
        and (p.get("admission") or {}).get("auto_mcp_setup") is False
        and (p.get("admission") or {}).get("auto_full_harden") is False
    )




# --- G2: drift report + install-class veto + isolation allowlist (readonly) ---

INSTALL_CLASS_ACTIONS = frozenset({
    "install",
    "npm_install",
    "bla_setup",
    "setup_mcp",
    "auto_mcp_setup",
    "bla_harden",
    "auto_full_harden",
    "defend",
})

ISOLATION_ALLOWLIST = (
    "examples/isolation/README.md",
    "examples/isolation/sample-clean.txt",
    "examples/isolation/CLEAN-PARAMS-POINTER.md",
    "examples/isolation/clean-params-template.json",
)

FORMAT_PROMOTION = {
    "js": {
        "empirically_verified_upstream": True,
        "promoted": False,
        "reason": "candidate_only_until_g2_sample_run",
    },
    "asar": {
        "empirically_verified_upstream": True,
        "promoted": False,
        "reason": "candidate_only_until_g2_sample_run",
    },
    "python": {
        "empirically_verified_upstream": False,
        "promoted": False,
        "reason": "designed_for_not_empirically_verified",
    },
    "pyc": {
        "empirically_verified_upstream": False,
        "promoted": False,
        "reason": "designed_for_not_empirically_verified",
    },
    "wheel": {
        "empirically_verified_upstream": False,
        "promoted": False,
        "reason": "ampuly_wheel_not_claimed",
    },
    "react": {
        "empirically_verified_upstream": False,
        "promoted": False,
        "reason": "react_not_claimed",
    },
    "ampuly": {
        "empirically_verified_upstream": False,
        "promoted": False,
        "reason": "business_ampuly_forbidden",
    },
}


def apply_bla_g2_gate(*, enabled: bool = False) -> bool:
    """G2 drift/isolation APIs stay inert unless explicitly enabled."""
    return bool(enabled)


def build_drift_report(*, enabled: bool = False) -> dict[str, Any]:
    """Verifiable admission + drift report from the frozen G1 lock (no network)."""
    if not apply_bla_g2_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": G2_RULES_VERSION,
            "reason": "bla_g2_gate_off",
            "report": None,
        }
    lock = get_bla_lock_snapshot()
    npm_v = lock["npm_published"]["version"]
    src_v = lock["source_package_json"]["version_at_commit"]
    drift = bool(lock["npm_published"].get("version_drift_vs_source")) or (npm_v != src_v)
    gaps: list[str] = []
    if drift:
        gaps.append("npm_version_drift_source_1_5_2_vs_published_1_1_0")
    aligned = (not drift) and (not gaps)
    return {
        "ok": True,
        "enabled": True,
        "rules_version": G2_RULES_VERSION,
        "reason": "drift_report",
        "report": {
            "g1_rules_version": RULES_VERSION,
            "reviewed_commit": lock["reviewed_commit"],
            "license_spdx": lock["license"]["spdx"],
            "license_sha256": lock["license"]["license_sha256"],
            "npm_version": npm_v,
            "npm_integrity": lock["npm_published"]["integrity"],
            "npm_shasum": lock["npm_published"]["shasum"],
            "source_version_at_commit": src_v,
            "version_aligned": aligned,
            "gaps": gaps,
            # G2 never auto-allows install even if aligned.
            "install_class_allowed": False,
            "alignment_blocks_install": not aligned,
            "alignment_status": ("aligned" if aligned else "blocked_drift"),
            "note_zh": (
                "对齐前禁止放行安装类动作；对齐本身不在本模块执行（不发版、不执行包管理安装）。"
                if not aligned
                else "版本已对齐声明，但仍禁止自动安装类/setup/加固（需显式后续阶段）。"
            ),
        },
    }


def versions_aligned(*, enabled: bool = False) -> bool:
    """True only when G2 gate on and npm version equals source tip with no gaps."""
    rep = build_drift_report(enabled=enabled)
    if not rep.get("ok"):
        return False
    r = rep["report"] or {}
    return bool(r.get("version_aligned")) and not (r.get("gaps") or [])


def evaluate_install_class_action(
    action: str,
    *,
    enabled: bool = False,
) -> dict[str, Any]:
    """Gate install-class actions. Default-off and drift both deny; never auto-allows."""
    action_key = (action or "").strip().lower()
    if not apply_bla_g2_gate(enabled=enabled):
        return {
            "ok": False,
            "allowed": False,
            "action": action_key,
            "reason": "bla_g2_gate_off",
            "rules_version": G2_RULES_VERSION,
        }
    if action_key not in INSTALL_CLASS_ACTIONS:
        return {
            "ok": True,
            "allowed": False,
            "action": action_key,
            "reason": "unknown_or_non_install_action",
            "rules_version": G2_RULES_VERSION,
            "note": "G2 only classifies install-class actions; unknowns are denied.",
        }
    rep = build_drift_report(enabled=True)
    report = rep["report"] or {}
    if report.get("alignment_blocks_install") or not report.get("version_aligned"):
        return {
            "ok": True,
            "allowed": False,
            "action": action_key,
            "reason": "version_drift_blocks_install",
            "gaps": list(report.get("gaps") or []),
            "npm_version": report.get("npm_version"),
            "source_version_at_commit": report.get("source_version_at_commit"),
            "rules_version": G2_RULES_VERSION,
        }
    return {
        "ok": True,
        "allowed": False,
        "action": action_key,
        "reason": "g2_forbids_auto_install_class",
        "gaps": [],
        "rules_version": G2_RULES_VERSION,
    }


def evaluate_isolation_sample(
    relative_path: str,
    fmt: str,
    *,
    enabled: bool = False,
) -> dict[str, Any]:
    """Readonly eligibility for an isolation sample. Does not run bla or mutate artifacts."""
    if not apply_bla_g2_gate(enabled=enabled):
        return {
            "ok": False,
            "eligible": False,
            "reason": "bla_g2_gate_off",
            "rules_version": G2_RULES_VERSION,
        }
    path = (relative_path or "").replace(chr(92), "/").lstrip("./")
    fmt_key = (fmt or "").strip().lower()
    promo = FORMAT_PROMOTION.get(fmt_key)
    if promo is None:
        return {
            "ok": True,
            "eligible": False,
            "path": path,
            "format": fmt_key,
            "reason": "unknown_format",
            "rules_version": G2_RULES_VERSION,
        }
    if fmt_key == "ampuly":
        return {
            "ok": True,
            "eligible": False,
            "path": path,
            "format": fmt_key,
            "reason": "business_ampuly_forbidden",
            "promotion": dict(promo),
            "rules_version": G2_RULES_VERSION,
        }
    if path not in ISOLATION_ALLOWLIST:
        return {
            "ok": True,
            "eligible": False,
            "path": path,
            "format": fmt_key,
            "reason": "path_not_in_allowlist",
            "allowlist": list(ISOLATION_ALLOWLIST),
            "promotion": dict(promo),
            "rules_version": G2_RULES_VERSION,
        }
    planning_ok = bool(promo.get("empirically_verified_upstream")) and not promo.get("promoted")
    return {
        "ok": True,
        "eligible": planning_ok,
        "path": path,
        "format": fmt_key,
        "reason": "planning_candidate" if planning_ok else str(promo.get("reason") or "not_promoted"),
        "promotion": dict(promo),
        "execution_forbidden": True,
        "rules_version": G2_RULES_VERSION,
    }




# --- G2b: clean params template + original-artifact recovery checklist (readonly) ---

CLEAN_PARAMS_TEMPLATE_PATH = (
    "examples/isolation/clean-params-template.json"
)

# Structural template (no I/O); mirrored by examples/isolation/clean-params-template.json.
_CLEAN_PARAMS_TEMPLATE: dict[str, Any] = {
    "schema_version": 1,
    "rules_version": G2_RULES_VERSION,
    "template_id": "g2-clean-params-v1",
    "purpose_zh": (
        "隔离样本规划用的干净参数模板；记录原产物身份与恢复所需字段。"
        "本模板不执行加固或包管理安装。"
    ),
    "execution_forbidden": True,
    "install_forbidden": True,
    "business_ampuly_forbidden": True,
    "clean_artifact": {
        "relative_path": "examples/isolation/sample-clean.txt",
        "format": "txt",
        "content_sha256": None,
        "recorded_at": None,
    },
    "parameters": {
        "tool": "bla-agents",
        "reviewed_commit": REVIEWED_COMMIT,
        "npm_version_locked": "1.1.0",
        "source_version_at_commit": "1.5.2",
        "alignment_required_before_install": True,
        "command_class": "none",
        "harden_flags": [],
        "setup_mcp": False,
        "output_dir": None,
    },
    "report_fields": {
        "clean_hash_before": None,
        "protected_hash_after": None,
        "restore_verified": False,
        "format_promoted": False,
        "gaps": ["npm_version_drift_source_1_5_2_vs_published_1_1_0"],
    },
    "restore": {
        "strategy": "replace_protected_with_clean_copy",
        "requires_clean_copy_retained": True,
        "requires_index_or_signature_pair": True,
        "auto_restore": False,
    },
}

# Six-axis item: 原产物可恢复 — readonly checklist fields only (no restore execution).
ORIGINAL_ARTIFACT_RECOVERY_CHECKLIST: dict[str, Any] = {
    "axis": "original_artifact_recoverable",
    "axis_zh": "原产物可恢复",
    "rules_version": G2_RULES_VERSION,
    "status": "checklist_only",
    "execution_forbidden": True,
    "fields": {
        "clean_artifact_path_recorded": {
            "required": True,
            "satisfied": True,
            "value": "examples/isolation/sample-clean.txt",
        },
        "clean_params_template_present": {
            "required": True,
            "satisfied": True,
            "value": CLEAN_PARAMS_TEMPLATE_PATH,
        },
        "clean_content_hash_recorded": {
            "required": True,
            "satisfied": False,
            "value": None,
            "note_zh": "真实样本跑前再填 sha256；G2 合同层不伪造哈希。",
        },
        "protected_artifact_path_recorded": {
            "required": True,
            "satisfied": False,
            "value": None,
            "note_zh": "尚未产生受保护副本（未执行加固）。",
        },
        "restore_strategy_documented": {
            "required": True,
            "satisfied": True,
            "value": "replace_protected_with_clean_copy",
        },
        "index_or_signature_pair_fields_reserved": {
            "required": True,
            "satisfied": True,
            "value": ["requires_index_or_signature_pair"],
        },
        "business_ampuly_excluded": {
            "required": True,
            "satisfied": True,
            "value": True,
        },
        "evidence_pack_kept_clean": {
            "required": True,
            "satisfied": True,
            "value": True,
        },
        "auto_restore_disabled": {
            "required": True,
            "satisfied": True,
            "value": True,
        },
        "restore_executed_in_this_phase": {
            "required": True,
            "satisfied": True,
            "value": False,
            "note_zh": "本阶段必须为 False。",
        },
    },
    "blocking_gaps": [
        "clean_content_hash_not_yet_recorded",
        "protected_artifact_not_yet_produced",
        "npm_version_drift_source_1_5_2_vs_published_1_1_0",
    ],
    "axis_verdict": "not_satisfied_pending_sample",
    "note_zh": (
        "清单字段已固化；轴判定未通过，因尚未保留带哈希的干净样本跑与可恢复对照。"
        "不得以清单存在冒充恢复已验证。"
    ),
}


def get_clean_params_template(*, enabled: bool = False) -> dict[str, Any]:
    """Return frozen clean-params template. Default-off."""
    import copy

    if not apply_bla_g2_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": G2_RULES_VERSION,
            "reason": "bla_g2_gate_off",
            "template": None,
        }
    return {
        "ok": True,
        "enabled": True,
        "rules_version": G2_RULES_VERSION,
        "reason": "clean_params_template",
        "template_path": CLEAN_PARAMS_TEMPLATE_PATH,
        "template": copy.deepcopy(_CLEAN_PARAMS_TEMPLATE),
    }


def original_artifact_recovery_read(*, enabled: bool = False) -> dict[str, Any]:
    """Readonly six-axis checklist for original-artifact recovery. Default-off."""
    import copy

    if not apply_bla_g2_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": G2_RULES_VERSION,
            "reason": "bla_g2_gate_off",
            "checklist": None,
        }
    return {
        "ok": True,
        "enabled": True,
        "rules_version": G2_RULES_VERSION,
        "reason": "recovery_checklist",
        "checklist": copy.deepcopy(ORIGINAL_ARTIFACT_RECOVERY_CHECKLIST),
    }


# --- G3: Ampuly product boundary (readonly declaration) ---

AMPULY_BLA_OUT_OF_SCOPE = (
    "ampuly_business_data",
    "authorization_store",
    "runtime_knowledge",
    "model_materials",
    "third_party_code",
    "employee_knowledge_injection",
)


def apply_bla_g3_gate(*, enabled: bool = False) -> bool:
    return bool(enabled)


def bla_boundary_read_api(*, enabled: bool = False) -> dict[str, Any]:
    """Readonly Ampuly vs BLA boundary. Default-off. Never authorizes harden."""
    if not apply_bla_g3_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": G3_RULES_VERSION,
            "reason": "bla_g3_gate_off",
            "boundary": None,
        }
    return {
        "ok": True,
        "enabled": True,
        "rules_version": G3_RULES_VERSION,
        "reason": "boundary_declared",
        "boundary": {
            "bla_role": "optional_release_hardening_layer_anti_rea",
            "not_drm": True,
            "not_authorization_boundary": True,
            "out_of_scope": list(AMPULY_BLA_OUT_OF_SCOPE),
            "clean_required": [
                "source_tree",
                "dev_debug_env",
                "evidence_packs",
            ],
            "harden_auto": False,
            "install_auto": False,
            "note_zh": (
                "BLA 不代替授权执行边界；不对业务 .ampuly / 授权库 / 模型资料注入；"
                "干净树与证据包保持未加固。"
            ),
        },
        "actions_forbidden": {
            "auto_mcp_setup": True,
            "auto_full_harden": True,
            "install": True,
            "inject_business_ampuly": True,
        },
    }




# --- G4: gray-release switch (default off; never auto harden/install) ---

def apply_bla_g4_gate(*, enabled: bool = False) -> bool:
    """Gray-release master switch. Default off."""
    return bool(enabled)


def bla_gray_release_read(*, enabled: bool = False) -> dict[str, Any]:
    """Readonly gray-release contract. Enabling only exposes status; never authorizes harden/install."""
    if not apply_bla_g4_gate(enabled=enabled):
        return {
            "ok": False,
            "enabled": False,
            "rules_version": G4_RULES_VERSION,
            "reason": "bla_g4_gate_off",
            "gray": None,
            "actions_forbidden": {
                "auto_mcp_setup": True,
                "auto_full_harden": True,
                "install": True,
                "auto_publish": True,
                "rollback_auto": True,
            },
        }
    # Even when gate on: gray channel is declared but actions stay forbidden until explicit later phase.
    drift = build_drift_report(enabled=True)
    aligned = bool((drift.get("report") or {}).get("version_aligned"))
    return {
        "ok": True,
        "enabled": True,
        "rules_version": G4_RULES_VERSION,
        "reason": "gray_declared_readonly",
        "gray": {
            "channel": "bla_optional_hardening",
            "rolled_out": False,
            "canary_percent": 0,
            "auto_harden": False,
            "auto_install": False,
            "auto_publish": False,
            "rollback_armed": False,
            "requires_version_aligned": True,
            "version_aligned": aligned,
            "note_zh": (
                "灰发开关仅声明；默认未放量。禁止自动加固/安装/发布；"
                "回滚需显式后续阶段，本 API 不执行回滚。"
            ),
        },
        "actions_forbidden": {
            "auto_mcp_setup": True,
            "auto_full_harden": True,
            "install": True,
            "auto_publish": True,
            "rollback_auto": True,
        },
    }


__all__ = [
    "RULES_VERSION",
    "G2_RULES_VERSION",
    "G3_RULES_VERSION",
    "REVIEWED_COMMIT",
    "LICENSE_SHA256",
    "INSTALL_CLASS_ACTIONS",
    "ISOLATION_ALLOWLIST",
    "FORMAT_PROMOTION",
    "AMPULY_BLA_OUT_OF_SCOPE",
    "CLEAN_PARAMS_TEMPLATE_PATH",
    "ORIGINAL_ARTIFACT_RECOVERY_CHECKLIST",
    "apply_bla_admission_gate",
    "get_bla_lock_snapshot",
    "bla_admission_read_api",
    "assert_no_auto_side_effects",
    "apply_bla_g2_gate",
    "build_drift_report",
    "versions_aligned",
    "evaluate_install_class_action",
    "evaluate_isolation_sample",
    "get_clean_params_template",
    "original_artifact_recovery_read",
    "apply_bla_g3_gate",
    "bla_boundary_read_api",
    "G4_RULES_VERSION",
    "apply_bla_g4_gate",
    "bla_gray_release_read",
]
