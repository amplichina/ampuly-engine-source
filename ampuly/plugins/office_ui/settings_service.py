"""API provider settings for the office UI (模型与密钥设置).

Key handling contract (distribution iron rule #3: keys live in env vars only):
- Keys entered in the UI are written to the Windows user-level environment
  (HKCU\\Environment — the same location ``setx`` writes and the same place
  ``LLMProvider._env_from_user_registry`` reads) plus the current process env.
- Keys never touch the application database, config files, or logs; responses
  only ever carry a masked tail.
- On non-Windows (Docker) hosts the page is read-only for keys: they come from
  the container environment (deploy/.env), and this module just reports status.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from loguru import logger

# ── Provider catalog ─────────────────────────────────────────────────────────
# LLM providers are live (litellm-native prefixes; key in env is enough).
# Generation tools below the live ones are roadmap entries and nothing
# more: no adapter, no key field, no call path. They are listed because
# the roadmap is real, and marked not_built because the integration is
# not -- "coming soon" told a reader somebody was working on it.

from ampuly.llm.task_kinds import KINDS as _TASK_KINDS
from ampuly.plugins.office_ui.model_catalogue import ENTRIES_BY_PROVIDER as _CATALOGUE
from ampuly.plugins.office_ui.model_catalogue import models_of as _catalogue_models

# Each provider's ``models`` is derived from the model catalogue (model_catalogue.py), in order, so the two cannot drift;
# ``entries`` carries what each model is and whether it may be chosen.
LLM_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": _catalogue_models("deepseek"),
        "note_zh": "默认引擎；国产已备案，合规友好",
        "note_en": "Default engine",
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "api_key_env": "DASHSCOPE_API_KEY",
        "models": _catalogue_models("qwen"),
        "note_zh": "阿里云百炼（DashScope）",
        "note_en": "Alibaba DashScope",
    },
    {
        "id": "glm",
        "name": "智谱 GLM",
        "api_key_env": "ZHIPUAI_API_KEY",
        "models": _catalogue_models("glm"),
        "note_zh": "智谱开放平台",
        "note_en": "Zhipu AI",
    },
    {
        "id": "kimi",
        "name": "Kimi",
        "api_key_env": "MOONSHOT_API_KEY",
        "models": _catalogue_models("kimi"),
        "note_zh": "月之暗面",
        "note_en": "Moonshot AI",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "api_key_env": "OPENAI_API_KEY",
        "models": _catalogue_models("openai"),
        "note_zh": "境外模型：默认不可用；启用前先确认该厂商的条款允许本公司使用",
        "note_en": "Requires access to api.openai.com",
    },
    {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "api_key_env": "ANTHROPIC_API_KEY",
        "models": _catalogue_models("anthropic"),
        "note_zh": "境外模型：默认不可用；启用前先确认该厂商的条款允许本公司使用",
        "note_en": "Requires access to api.anthropic.com",
    },
]

# Live generation-tool credentials (the image_generate tool reads these).
GEN_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "jimeng_ak",
        "name": "即梦 AccessKey",
        "api_key_env": "VOLC_ACCESSKEY",
        "models": [],
        "note_zh": "火山引擎 AK（与 SecretKey 成对）",
        "note_en": "Volcano Engine AccessKey",
    },
    {
        "id": "jimeng_sk",
        "name": "即梦 SecretKey",
        "api_key_env": "VOLC_SECRETKEY",
        "models": [],
        "note_zh": "火山引擎 SK（与 AccessKey 成对）",
        "note_en": "Volcano Engine SecretKey",
    },
    {
        "id": "qwen_image",
        "name": "千问生图",
        "api_key_env": "DASHSCOPE_API_KEY",
        "models": [],
        "note_zh": "通义万相；与通义千问共用 DashScope Key",
        "note_en": "Shares the DashScope key with Qwen LLM",
    },
]

# Judgment models (4.7 Q04-C): typed answers -- yes/no, one of a list, a score -- with no text written. Not chat models,
# so never offered as a default or as a step's model; the test button asks one small question (puly.check_connection).
JUDGE_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "puly",
        "name": "PULY（判断模型 · JEV）",
        "api_key_env": "JEV_API_KEY",
        "models": ["puly/jev-latest"],
        "note_zh": "识别、分类、调配这类「是否 / 选一个 / 打分」的判断，由 TypeSafe 的 JEV 模型回答；按调用计费，判断内容会发往 TypeSafe 云端",
        "note_en": "Yes/no, pick-one and score judgments, answered by TypeSafe's JEV model; billed per call, the content goes to TypeSafe's cloud",
    },
]

GEN_TOOLS_NOT_BUILT: list[dict[str, Any]] = [
    {
        "id": "jianying",
        "name_zh": "剪映（视频剪辑）",
        "name_en": "CapCut (video)",
        "state": "not_built",
        "missing_zh": "没有适配器、没有密钥字段、没有调用路径",
        "missing_en": "no adapter, no key field, no call path",
    },
    {
        "id": "minimax_av",
        "name_zh": "MiniMax（音视频生成）",
        "name_en": "MiniMax (audio/video)",
        "state": "not_built",
        "missing_zh": "没有适配器、没有密钥字段、没有调用路径",
        "missing_en": "no adapter, no key field, no call path",
    },
]

# Routing slots the UI exposes in v1. ``default`` maps to llm.default_model;
# the rest map to llm.routing[task_type].
ROUTING_SLOTS: list[dict[str, str]] = [
    {"id": "default", "task_type": ""},
    {"id": "quick_tasks", "task_type": "quick_tasks"},
]

# What the settings page *reports on*, which is not the same list as what it lets you *write*. The six kinds are
# set through the tier table and its own audit profile, never by pinning a model to a slot -- so they are added
# here and deliberately not to ROUTING_SLOTS, whose other two readers decide the payload's shape and the set of
# ids `settings_set_routing` will accept.
HEALTH_SLOTS: list[dict[str, str]] = ROUTING_SLOTS + [
    {"id": kind, "task_type": kind} for kind in _TASK_KINDS
]

_ALLOWED_KEY_ENVS = {p["api_key_env"] for p in LLM_PROVIDERS} | {
    p["api_key_env"] for p in GEN_PROVIDERS
} | {p["api_key_env"] for p in JUDGE_PROVIDERS}
# The duty officer's pager: credentials and targets share the key storage.
from ampuly.plugins.office_ui.notifier import NOTIFY_ENV_NAMES as _NOTIFY_ENV_NAMES, notify_status as _notify_status  # noqa: E402

_ALLOWED_KEY_ENVS |= set(_NOTIFY_ENV_NAMES)


def _allowed_key_env(env_name: str) -> bool:
    """A variable this page may write and ``configured_key`` read: the console's own (``_ALLOWED_KEY_ENVS``) or one a
    registered console extension declares (``extensions.secret_env_names``)."""
    if env_name in _ALLOWED_KEY_ENVS:
        return True
    from ampuly.plugins.office_ui import extensions

    return env_name in extensions.secret_env_names()

# Runtime switches the operator can flip from the UI. These are booleans, not
# credentials, but they share the storage mechanism (user-level environment)
# so a setting survives a restart. Anything that weakens a safety boundary
# must carry `caution` so the UI can warn before it is turned on.
RUNTIME_FLAGS: list[dict[str, Any]] = [
    {
        "id": "lexical_workspace",
        "env": "AMPULY_ALLOW_LEXICAL_WORKSPACE",
        "name_zh": "Windows 文件落盘",
        "name_en": "Windows file writes",
        "note_zh": (
            "允许公司模式角色在 Windows 上真正写文件（否则只能把交付物全文塞进站内信）。"
            "边界规则仍然生效（词法围栏、拒符号链接与硬链接），仅放弃"
            "「检查与生效为同一操作」的原子性。"
        ),
        "note_en": (
            "Let company roles write real files on Windows. Boundary rules still "
            "apply; only the atomic check/use guarantee is given up."
        ),
        "caution_zh": "仅限单租户主机（本机/独立客户容器），多租户部署请用 Linux。",
        "caution_en": "Single-tenant hosts only; use Linux for multi-tenant deployments.",
    },
    {
        "id": "master_single_shot",
        "env": "AMPULY_MASTER_SINGLE_SHOT",
        "name_zh": "总控只做单次问答（关闭多步执行）",
        "name_en": "Master control single-call turn (multi-step off)",
        "note_zh": (
            "默认情况下总控会在一条消息里先查后决定：自己调用查询工具（订单、阻塞、能力、流程草稿、候选包文件），"
            "看到结果再回答、提出流程改动（由你在搭建台应用并保存）或起草工单（在业务工单里等你放行），每轮仍然最多起草一张工单、一次控制动作，走同一套审批与执行入口。"
            "勾选这一项即回到旧做法——一次提问只问一次模型，最多执行一个动作。"
        ),
        "note_en": (
            "By default master control looks things up and then decides inside one message: it calls the "
            "read tools itself (orders, blockers, capabilities, process drafts, candidate files) and "
            "answers, proposes a workflow change for you to apply and save on the builder canvas, or drafts a work "
            "order for you to release in Orders from what came back — still at most one order draft and "
            "one control verb per turn, through the same handlers. Tick this to restore the old behaviour: "
            "one model call per message, at most one action."
        ),
        "caution_zh": "多步执行时每一轮都是一次付费模型调用，一条消息最多 24 轮；勾选后即刻恢复单次问答。",
        "caution_en": "In multi-step mode every iteration is a paid model call, up to 24 per message; ticking this restores the single-call turn immediately.",
    },
    {
        "id": "master_vision",
        "env": "AMPULY_MASTER_VISION",
        "name_zh": "总控用通义千问视觉识别图片和扫描件",
        "name_en": "Master control reads pictures and scans with Qwen vision",
        "note_zh": (
            "本机没有 OCR：不开时，发给总控的图片和扫描件只保存、不读取。打开后，每个图片或扫描件（取前 3 页）"
            "在总控回答前交给通义千问视觉模型（qwen3-vl-plus）识别一次，每条消息最多 3 个文件；识别结果标明「由视觉模型识别，可能有误」，"
            "图片里的文字只当内容、不当指令。结果未知时不重发。用的是 DASHSCOPE_API_KEY（与通义千问共用）。"
        ),
        "note_en": (
            "There is no OCR here: with this off, pictures and scans sent to master control are stored, not read. On, each "
            "picture or scan (its first 3 pages) is read once by Qwen's vision model (qwen3-vl-plus) before the answer, at "
            "most 3 files a message; what it reads is labelled as read by a vision model, and text in a picture is content, "
            "never instructions. An unknown outcome is not sent again. Uses DASHSCOPE_API_KEY (shared with Qwen)."
        ),
        "caution_zh": "每个文件一次付费调用，文件会发往阿里云百炼；单价以百炼控制台为准，这里只记 token、不估金额。",
        "caution_en": "One paid call per file, and the files go to Alibaba Cloud (Bailian); the price is Bailian's, only tokens are recorded here.",
    },
]

_FLAG_BY_ENV = {flag["env"]: flag for flag in RUNTIME_FLAGS}
_TRUTHY = {"1", "true", "yes", "on"}


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "•••"
    return f"•••{value[-4:]}"


def _read_user_registry_env(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg_key:
            return str(winreg.QueryValueEx(reg_key, name)[0]).strip()
    except OSError:
        return ""


def _key_value(env_name: str) -> str:
    return (os.environ.get(env_name) or "").strip() or _read_user_registry_env(env_name)


def configured_key(env_name: str) -> str:
    """A key this page manages, as set on this machine ('' when not set) -- for a caller that sends it itself.

    Only the variables this page writes can be read this way; the value is the caller's to put in one header and
    nowhere else.
    """
    if not _allowed_key_env(env_name):
        raise ValueError(f"unknown provider key env: {env_name}")
    return _key_value(env_name)


def write_user_key(env_name: str, value: str) -> None:
    """Persist a key to HKCU\\Environment + the current process env.

    The value is never logged. Raises on non-Windows hosts (keys there come
    from the deployment environment) and on unknown env names.
    """
    if not _allowed_key_env(env_name):
        raise ValueError(f"unknown provider key env: {env_name}")
    value = value.strip()
    if not value:
        raise ValueError("empty key")
    if os.name != "nt":
        raise RuntimeError("key_write_unsupported_on_this_host")
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as reg_key:
        winreg.SetValueEx(reg_key, env_name, 0, winreg.REG_SZ, value)
    os.environ[env_name] = value
    logger.info(f"settings: stored API key for {env_name} (user-level env)")


def delete_user_key(env_name: str) -> None:
    if not _allowed_key_env(env_name):
        raise ValueError(f"unknown provider key env: {env_name}")
    os.environ.pop(env_name, None)
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
            ) as reg_key:
                winreg.DeleteValue(reg_key, env_name)
        except OSError:
            pass
    logger.info(f"settings: removed API key for {env_name}")


def set_runtime_flag(env_name: str, enabled: bool) -> None:
    """Flip a runtime switch for this process and persist it for the next start.

    The process environment is updated first so the change takes effect
    immediately (the consumers read the variable per call); the user-level
    environment carries it across restarts.
    """
    if env_name not in _FLAG_BY_ENV:
        raise ValueError(f"unknown runtime flag: {env_name}")
    if enabled:
        os.environ[env_name] = "1"
    else:
        os.environ.pop(env_name, None)
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
            ) as reg_key:
                if enabled:
                    winreg.SetValueEx(reg_key, env_name, 0, winreg.REG_SZ, "1")
                else:
                    try:
                        winreg.DeleteValue(reg_key, env_name)
                    except OSError:
                        pass
        except OSError:
            logger.warning(f"settings: could not persist runtime flag {env_name}")
    logger.info(f"settings: runtime flag {env_name} -> {'on' if enabled else 'off'}")


def _flag_enabled(env_name: str) -> bool:
    return str(os.environ.get(env_name, "") or _read_user_registry_env(env_name)).strip().lower() in _TRUTHY


def runtime_flag_enabled(env_name: str) -> bool:
    """Whether a declared runtime switch is on right now.

    The one reader of a switch, so "off by default" is decided in exactly one
    place: absent means off, and a name that is not declared in
    :data:`RUNTIME_FLAGS` raises rather than reading as off -- a consumer
    watching a variable no settings page can write is a switch the owner
    cannot turn back off.
    """
    if env_name not in _FLAG_BY_ENV:
        raise ValueError(f"unknown runtime flag: {env_name}")
    return _flag_enabled(env_name)


# 总控施工模式 (policy, B1): two values, not switches, kept where the switches are -- the user-level
# environment -- so they survive a restart and are read per build. An unset or unusable value is the default.
BUILD_SETTINGS: list[dict[str, Any]] = [
    {
        "id": "master_build_model",
        "env": "AMPULY_MASTER_BUILD_MODEL",
        "kind": "model",
        "default": "",
        "name_zh": "施工用模型",
        "name_en": "Model for build mode",
        "note_zh": (
            "总控施工模式用哪个模型。空着就用引擎的默认模型；填了的话，这个模型要在模型目录里、它的密钥已经设好，"
            "不然施工开工前就会停下说明，不会花钱。"
        ),
        "note_en": (
            "Which model master control's build mode uses. Empty means the engine's default model; a model named here "
            "has to be in the model catalogue with its key set, or a build stops before it starts and spends nothing."
        ),
    },
    {
        "id": "master_build_model_calls",
        "env": "AMPULY_MASTER_BUILD_MODEL_CALLS",
        "kind": "count",
        "default": 300,
        "min": 1,
        "max": 5000,
        "name_zh": "施工单个任务最多模型调用次数",
        "name_en": "Model calls per build job, at most",
        "note_zh": (
            "每个施工任务最多调用模型多少次（它派的子助手和离线试跑也算在里面）。另外它每干一步，还会多一次整理要点的调用，"
            "也算在里面，所以实际能干的步数大约是这个数的一半。到了就停下告诉你，不会自己重试；"
            "你让它接着干，就再给它一份同样多的次数。"
        ),
        "note_en": (
            "How many model calls one build job may make, its sub-helpers and offline trials included. Each step also "
            "makes one more call that keeps the running summary, counted too, so the steps it can take are about half "
            "this number. At the cap it stops and says so, never retrying; telling it to go on grants it the same "
            "number again."
        ),
    },
]
_BUILD_SETTING_BY_ID = {setting["id"]: setting for setting in BUILD_SETTINGS}


def _build_setting_value(setting: dict[str, Any], raw: str) -> Any:
    """``raw`` as this setting's value, or the default when it is unset or not a value the setting takes."""
    clean = str(raw or "").strip()
    if setting["kind"] == "count":
        try:
            value = int(clean)
        except ValueError:
            return setting["default"]
        return value if setting["min"] <= value <= setting["max"] else setting["default"]
    return clean[:200]


def build_setting(setting_id: str) -> Any:
    """The current value of one build-mode setting (``BUILD_SETTINGS``); a name that is not declared raises."""
    setting = _BUILD_SETTING_BY_ID.get(str(setting_id or ""))
    if setting is None:
        raise ValueError(f"unknown build setting: {setting_id}")
    env_name = setting["env"]
    return _build_setting_value(setting, str(os.environ.get(env_name, "") or _read_user_registry_env(env_name)))


def set_build_setting(setting_id: str, value: Any) -> Any:
    """Set one build-mode setting for this process and persist it for the next start; empty clears it.

    Checked before anything is written: a count outside its range, or a model the catalogue does not list, is refused.
    """
    setting = _BUILD_SETTING_BY_ID.get(str(setting_id or ""))
    if setting is None:
        raise ValueError(f"unknown build setting: {setting_id}")
    clean = str(value if value is not None else "").strip()
    if clean and setting["kind"] == "count":
        try:
            number = int(clean)
        except ValueError as exc:
            raise ValueError(f"{setting_id} must be a whole number") from exc
        if not setting["min"] <= number <= setting["max"]:
            raise ValueError(f"{setting_id} must be between {setting['min']} and {setting['max']}")
        clean = str(number)
    if clean and setting["kind"] == "model" and not _model_known(clean):
        raise ValueError(f"{clean!r} is not in the model catalogue")
    env_name = setting["env"]
    if clean:
        os.environ[env_name] = clean
    else:
        os.environ.pop(env_name, None)
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as reg_key:
                if clean:
                    winreg.SetValueEx(reg_key, env_name, 0, winreg.REG_SZ, clean)
                else:
                    try:
                        winreg.DeleteValue(reg_key, env_name)
                    except OSError:
                        pass
        except OSError:
            logger.warning(f"settings: could not persist build setting {env_name}")
    logger.info(f"settings: build setting {env_name} -> {clean or '(default)'}")
    return build_setting(setting_id)


def build_settings_payload(engine: Any) -> dict[str, Any]:
    llm_config = getattr(getattr(engine, "config", None), "llm", None)
    default_model = str(getattr(llm_config, "default_model", "") or "")
    routing = dict(getattr(llm_config, "routing", {}) or {})

    def _provider_row(provider: dict[str, Any]) -> dict[str, Any]:
        value = _key_value(provider["api_key_env"])
        row = {
            "id": provider["id"],
            "name": provider["name"],
            "api_key_env": provider["api_key_env"],
            "models": list(provider["models"]),
            "note_zh": provider["note_zh"],
            "note_en": provider["note_en"],
            "configured": bool(value),
            "key_masked": _mask(value),
        }
        catalogue = _CATALOGUE.get(str(provider["id"]))
        if catalogue is not None:
            import copy

            row["entries"] = copy.deepcopy(catalogue)
        return row

    flags = [
        {
            "id": flag["id"],
            "env": flag["env"],
            "name_zh": flag["name_zh"],
            "name_en": flag["name_en"],
            "note_zh": flag["note_zh"],
            "note_en": flag["note_en"],
            "caution_zh": flag.get("caution_zh", ""),
            "caution_en": flag.get("caution_en", ""),
            "enabled": _flag_enabled(flag["env"]),
        }
        for flag in RUNTIME_FLAGS
    ]

    return {
        "providers": [_provider_row(p) for p in LLM_PROVIDERS],
        "notify": _notify_status(),
        "gen_providers": [_provider_row(p) for p in GEN_PROVIDERS],
        "judge_providers": [_provider_row(p) for p in JUDGE_PROVIDERS],
        "runtime_flags": flags,
        "flags_writable": os.name == "nt",
        # 总控施工模式 (B1): what the build's two settings are now, in plain Chinese; the page entry that writes them
        # is B2's (set_build_setting).
        "build_settings": [
            {
                **{key: setting[key] for key in ("id", "env", "kind", "default", "name_zh", "name_en", "note_zh", "note_en")},
                **({"min": setting["min"], "max": setting["max"]} if setting["kind"] == "count" else {}),
                "value": build_setting(setting["id"]),
            }
            for setting in BUILD_SETTINGS
        ],
        "gen_tools_not_built": [dict(item) for item in GEN_TOOLS_NOT_BUILT],
        "default_model": default_model,
        "routing": {slot["id"]: routing.get(slot["task_type"], "") for slot in ROUTING_SLOTS if slot["task_type"]},
        "route_health": route_health(engine),
        # 模型分配 (routing phase 1). Read-only here; written through settings_set_model_tiers, which has its
        # own audit profile -- a tier is not a routing slot and must not be settable as one.
        "tiers": dict(getattr(llm_config, "tiers", {}) or {}),
        "tier_params": {str(tier): dict(params or {})
                        for tier, params in dict(getattr(llm_config, "tier_params", {}) or {}).items()},
        "task_tiers": dict(getattr(llm_config, "task_tiers", {}) or {}),
        "residency": str(getattr(llm_config, "residency", "") or "cn_only"),
        "no_training_optout": dict(getattr(llm_config, "no_training_optout", {}) or {}),
        "work_window_cap": int(getattr(llm_config, "work_window_cap", 0) or 0),
        "keys_writable": os.name == "nt",
    }


TIER_CHANGE_SCOPE = (
    "Takes effect on orders placed after this change. An order already placed keeps the plan it was launched "
    "with."
)


def plan_model_tiers(engine: Any, *, tiers: dict[str, str] | None = None,
                     tier_params: dict[str, dict[str, Any]] | None = None,
                     task_tiers: dict[str, str] | None = None,
                     residency: str | None = None,
                     no_training_optout: dict[str, str] | None = None,
                     work_window_cap: int | None = None) -> list[dict[str, Any]]:
    """Every change this request would make, all of it validated first; nothing is changed here.

    The same shape as ``plan_routing``: refuse before anything moves, so a request that is wrong in its third
    field does not leave the first two applied. Every refusal is a ``ValueError`` whose text begins with the code.
    """
    from ampuly.plugins.office_ui.model_catalogue import entry as _entry, refusal
    from ampuly.plugins.office_ui.model_residency import RESIDENCY, model_refusal
    from ampuly.plugins.office_ui.model_tiers import TIERS, normalize_tier

    llm_config = engine.config.llm
    changes: list[dict[str, Any]] = []

    wanted_residency = str(residency if residency is not None else getattr(llm_config, "residency", "") or "")
    if residency is not None and wanted_residency not in RESIDENCY:
        raise ValueError(f"residency_unknown: {residency}")
    wanted_optout = (dict(no_training_optout) if no_training_optout is not None
                     else dict(getattr(llm_config, "no_training_optout", {}) or {}))

    wanted_params = (dict(tier_params) if tier_params is not None
                     else dict(getattr(llm_config, "tier_params", {}) or {}))
    for tier, params in wanted_params.items():
        if not normalize_tier(tier):
            raise ValueError(f"tier_unknown: {tier}")
        cap = dict(params or {}).get("window_cap", 0)
        try:
            if int(cap or 0) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"window_cap_invalid: {tier}") from None
        if dict(params or {}).get("thinking") and not str(dict(params or {}).get("tool_loop_trial", "") or "").strip():
            # Turning it on would assert, for the owner, that a thing nobody has run works.
            raise ValueError(f"thinking_without_trial: {tier}")

    if tiers is not None:
        current = dict(getattr(llm_config, "tiers", {}) or {})
        for tier, model in dict(tiers).items():
            if not normalize_tier(tier):
                raise ValueError(f"tier_unknown: {tier}")
            clean = str(model or "").strip()
            if not clean or clean == str(current.get(tier, "") or ""):
                continue
            blocked = model_refusal(_entry(clean), wanted_residency, wanted_optout)
            if blocked:
                raise ValueError(f"{blocked}: {clean}")
            refused = refusal(clean, params=dict(wanted_params.get(normalize_tier(tier), {}) or {}))
            if refused is not None:
                raise ValueError(f"{refused['code']}: {clean}")
        if dict(tiers) != current:
            changes.append({"field": "tiers", "before": current, "after": dict(tiers)})

    if task_tiers is not None:
        for kind, tier in dict(task_tiers).items():
            if tier and not normalize_tier(tier):
                raise ValueError(f"tier_unknown: {tier}")
        current = dict(getattr(llm_config, "task_tiers", {}) or {})
        if dict(task_tiers) != current:
            changes.append({"field": "task_tiers", "before": current, "after": dict(task_tiers)})

    if tier_params is not None and wanted_params != dict(getattr(llm_config, "tier_params", {}) or {}):
        changes.append({"field": "tier_params",
                        "before": dict(getattr(llm_config, "tier_params", {}) or {}), "after": wanted_params})
    if residency is not None and wanted_residency != str(getattr(llm_config, "residency", "") or ""):
        changes.append({"field": "residency",
                        "before": str(getattr(llm_config, "residency", "") or ""), "after": wanted_residency})
    if no_training_optout is not None and wanted_optout != dict(getattr(llm_config, "no_training_optout", {}) or {}):
        changes.append({"field": "no_training_optout",
                        "before": dict(getattr(llm_config, "no_training_optout", {}) or {}), "after": wanted_optout})
    if work_window_cap is not None:
        try:
            cap = int(work_window_cap)
        except (TypeError, ValueError):
            raise ValueError(f"window_cap_invalid: {work_window_cap}") from None
        if cap < 0:
            raise ValueError(f"window_cap_invalid: {work_window_cap}")
        if cap != int(getattr(llm_config, "work_window_cap", 0) or 0):
            changes.append({"field": "work_window_cap",
                            "before": int(getattr(llm_config, "work_window_cap", 0) or 0), "after": cap})
    _ = TIERS
    return changes


def apply_model_tiers(engine: Any, changes: list[dict[str, Any]], *, actor: str = "owner") -> dict[str, Any]:
    """Apply a plan from :func:`plan_model_tiers`: live first, then to the file.

    Answers ``{"applied", "saved", "changes"}``. A save that fails leaves the change live and says so, which is
    the same contract ``apply_routing`` has -- telling the owner 「saved」 when it was not would be worse.
    """
    llm_config = engine.config.llm
    for change in changes:
        setattr(llm_config, str(change["field"]), change["after"])
    saved = True
    try:
        _persist_llm_config(engine, llm_config)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning("settings: model tiers applied live but not saved")
        saved = False
    return {"applied": True, "saved": saved, "changes": list(changes)}


def apply_tier_changes_live(engines: Any, changes: list[dict[str, Any]]) -> int:
    """The same tier changes, live, on other engines this process already runs; how many took them.

    The tiers file is machine-wide, but ``apply_model_tiers`` changes only the engine the request reached; each project
    engine holds its own copy of the model config (candidate 3, : every engine reads its own), so without
    this a loaded project kept the old tiers / residency for its non-order traffic until a restart. Running orders keep
    the plan they were launched with either way.
    """
    count = 0
    for other in engines or ():
        llm_config = getattr(getattr(other, "config", None), "llm", None)
        if llm_config is None:
            continue
        for change in changes:
            setattr(llm_config, str(change["field"]), change["after"])
        count += 1
    return count


def route_health(engine: Any) -> dict[str, dict[str, Any]]:
    """For each model slot, whether a call on it could be sent now, and if not why -- a dry run that sends nothing.

    ``default`` is the default model; a routing slot is judged by the provider's own route (``LLMProvider._route``),
    so it says exactly what a call would meet: a model not in the catalogue, another vendor's key that is not set,
    a customer's own key. No customer is in scope here.
    """
    from ampuly.llm.budget import RouteRefused

    llm = getattr(engine, "llm", None)
    health: dict[str, dict[str, Any]] = {}
    for slot in HEALTH_SLOTS:
        task_type = slot["task_type"]
        if llm is None:
            health[slot["id"]] = {"usable": False, "reason": "no_provider"}
            continue
        try:
            target = llm._route(task_type or None) if callable(getattr(llm, "_route", None)) else llm
        except RouteRefused as exc:
            health[slot["id"]] = {"usable": False, "reason": exc.code}
            continue
        except Exception as exc:  # noqa: BLE001 - a dry run that cannot tell is not usable
            health[slot["id"]] = {"usable": False, "reason": type(exc).__name__}
            continue
        usable = bool(getattr(target, "_api_key", None))
        health[slot["id"]] = {"usable": usable, "reason": "" if usable else "key_not_set"}
    return health


async def test_model(model: str, timeout_seconds: float = 30.0) -> dict[str, Any]:
    """Fire a minimal completion at ``model`` using env-var credentials (a judgment model: one small question)."""
    if any(model in provider["models"] for provider in JUDGE_PROVIDERS):
        from ampuly.plugins.office_ui import puly

        return await puly.check_connection(timeout=timeout_seconds)

    # Only a listed model that may be chosen here is pinged (model-routing phase 0): an unlisted, retired or
    # unroutable id, or an overseas one that is off by default, is refused before anything is sent.
    from ampuly.plugins.office_ui.model_catalogue import refusal

    refused = refusal(model)
    if refused is not None and refused["code"] != "model_needs_params":
        return {"ok": False, "error": refused["code"], "refused": refused}
    variable = _catalogue_key_env(model)
    key = str(os.environ.get(variable, "") or "").strip() if variable else ""
    if not key and variable:
        from ampuly.llm.provider import LLMProvider

        key = str(LLMProvider._env_from_user_registry(variable) or "")
    if not key:
        return {"ok": False, "error": "key_not_set"}

    import litellm

    started = time.monotonic()
    try:
        # Newer OpenAI ids reject temperature=0 / max_tokens; mainland providers still take the old pair.
        completion_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "ping — reply with 'pong' only"}],
            "api_key": key,
            "num_retries": 0,
        }
        if str(model).startswith("openai/"):
            completion_kwargs["max_completion_tokens"] = 32
        else:
            completion_kwargs["max_tokens"] = 8
            completion_kwargs["temperature"] = 0
        response = await asyncio.wait_for(
            litellm.acompletion(**completion_kwargs),
            timeout=timeout_seconds,
        )
        _ = response.choices[0].message
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"ok": True, "latency_ms": latency_ms}
    except TimeoutError:
        return {"ok": False, "error": f"timeout after {int(timeout_seconds)}s"}
    except Exception as e:
        message = str(e)
        return {"ok": False, "error": message[:300]}


def _model_known(model: str) -> bool:
    return any(model in provider["models"] for provider in LLM_PROVIDERS)


# What a routing change's audit row says about when it takes effect (model-routing phase 0).
ROUTING_CHANGE_SCOPE = (
    "Takes effect on new orders and on work that is not an order's. An order already placed keeps the model plan "
    "it was launched with; other loaded projects and new processes pick the new settings up after a restart."
)


def _prefix(model: str) -> str:
    text = str(model or "")
    return text.split("/", 1)[0] if "/" in text else ""


def _catalogue_key_env(model: str) -> str:
    return next((p["api_key_env"] for p in LLM_PROVIDERS if model in p["models"]), "")


def _key_is_set(variable: str) -> bool:
    if not variable:
        return False
    if str(os.environ.get(variable, "") or "").strip():
        return True
    from ampuly.llm.provider import LLMProvider

    return bool(LLMProvider._env_from_user_registry(variable))


def plan_routing(engine: Any, *, default_model: str | None, routing_updates: dict[str, str],
                 customer_model_keys: bool = False) -> list[dict[str, Any]]:
    """Every change a routing request would make, all of it validated first; nothing is changed here.

    Refused (``ValueError``) before anything moves: an unknown model; a default that would leave the vendor of an
    engine whose calls use a customer's own key -- the engine's own, or any customer's resolved per call
    (``customer_model_keys``) -- because that key would go to the new vendor; a slot on another vendor whose key is not
    set (every call on it would be refused).

    Only a move to another vendor changes how the default authenticates: the catalogue's key variable, no proxy base,
    no reasoning effort, no literal key of the old vendor (recorded without its value). A default moved within its
    vendor keeps its key variable and base, which may be a relay's.
    """
    llm_config = engine.config.llm
    llm = getattr(engine, "llm", None)
    customer_engine = bool(str(getattr(llm, "_customer_key_env", "") or ""))
    changes: list[dict[str, Any]] = []
    old_default = str(llm_config.default_model or "")
    new_default = old_default
    from ampuly.plugins.office_ui.model_catalogue import entry as _catalogue_entry, refusal
    from ampuly.plugins.office_ui.model_residency import model_refusal as _residency_refusal

    # A new choice must also be one the owner's residency setting allows. As with the catalogue, only a NEW
    # choice is checked: a default already in llm_config.yaml is never refused on load.
    def _residency_block(model: str) -> str:
        return _residency_refusal(_catalogue_entry(model), str(getattr(llm_config, "residency", "") or ""),
                                  dict(getattr(llm_config, "no_training_optout", {}) or {}))

    if default_model:
        if not _model_known(default_model):
            raise ValueError(f"unknown model: {default_model}")
        if default_model != old_default:
            blocked = _residency_block(default_model)
            if blocked:
                raise ValueError(f"{blocked}: {default_model}")
            refused = refusal(default_model)
            if refused is not None:
                # A new choice must be one the catalogue offers; a default already set is never refused on load.
                raise ValueError(f"{refused['code']}: {default_model}")
            vendor_change = _prefix(default_model) != _prefix(old_default)
            if vendor_change and (customer_engine or customer_model_keys):
                raise ValueError("customer_key_engine: this project's model calls use a customer's own key; its default "
                                 "cannot move to another vendor here")
            new_default = default_model
            changes.append({"field": "llm.default_model", "old": old_default, "new": new_default})
            if vendor_change:
                # Nothing of the old vendor's way in travels with the new vendor: its key variable, a proxy base, a
                # reasoning effort, a literal key (recorded as set, never with its value).
                catalogue_env = _catalogue_key_env(new_default)
                if catalogue_env != str(llm_config.api_key_env or ""):
                    changes.append({"field": "llm.api_key_env", "old": str(llm_config.api_key_env or ""), "new": catalogue_env})
                if str(llm_config.api_base or ""):
                    changes.append({"field": "llm.api_base", "old": str(llm_config.api_base), "new": ""})
                if getattr(llm_config, "reasoning_effort", None):
                    changes.append({"field": "llm.reasoning_effort", "old": llm_config.reasoning_effort, "new": None})
                if str(getattr(llm_config, "api_key", "") or ""):
                    changes.append({"field": "llm.api_key", "old": "<set>", "new": ""})

    slot_by_id = {slot["id"]: slot["task_type"] for slot in ROUTING_SLOTS if slot["task_type"]}
    current = dict(llm_config.routing or {})
    for slot_id, model in routing_updates.items():
        task_type = slot_by_id.get(slot_id)
        if not task_type:
            continue
        model = str(model or "").strip()
        if model and not _model_known(model):
            raise ValueError(f"unknown model: {model}")
        old = str(current.get(task_type, "") or "")
        if model == old:
            continue
        if model:
            blocked = _residency_block(model)
            if blocked:
                raise ValueError(f"{blocked}: {model}")
            refused = refusal(model)
            if refused is not None:
                raise ValueError(f"{refused['code']}: {model}")
        if model and _prefix(model) != _prefix(new_default):
            if customer_engine:
                raise ValueError(f"customer_key_engine: {model} is another vendor's model and this project's calls use a "
                                 "customer's own key; every call on it would be refused")
            variable = _catalogue_key_env(model)
            if not _key_is_set(variable):
                raise ValueError(f"key_not_set: {model} authenticates with {variable}, which is not set")
        changes.append({"field": f"llm.routing.{task_type}", "old": old or None, "new": model or None})
    return changes


def apply_routing(engine: Any, *, default_model: str | None, routing_updates: dict[str, str],
                  customer_model_keys: bool = False) -> dict[str, Any]:
    """Validate the whole routing request, then apply it to the live engine and persist it; return what changed.

    Returns ``{"changes": [...], "saved": bool, "save_error": str}``. A routed call reaches another vendor only
    through that vendor's own sibling (``LLMProvider._route``), so the default's key stays pinned whatever the slots
    say -- the old "unpin on mixed vendors" workaround is gone. A file that cannot be written leaves the change live
    and says so.
    """
    changes = plan_routing(engine, default_model=default_model, routing_updates=routing_updates,
                           customer_model_keys=customer_model_keys)
    if not changes:
        return {"changes": [], "saved": True, "save_error": ""}
    llm_config = engine.config.llm
    llm = getattr(engine, "llm", None)
    customer_engine = bool(str(getattr(llm, "_customer_key_env", "") or ""))
    routing = dict(llm_config.routing or {})
    for change in changes:
        field, new = change["field"], change["new"]
        if field == "llm.default_model":
            llm_config.default_model = new
        elif field == "llm.api_key_env":
            llm_config.api_key_env = new
        elif field == "llm.api_base":
            llm_config.api_base = new
        elif field == "llm.reasoning_effort":
            llm_config.reasoning_effort = new
        elif field == "llm.api_key":
            llm_config.api_key = ""
        elif field.startswith("llm.routing."):
            task_type = field[len("llm.routing."):]
            if new:
                routing[task_type] = new
            else:
                routing.pop(task_type, None)
    llm_config.routing = routing

    if llm is not None:
        llm.config = llm_config
        if not str(getattr(llm, "_customer_key_env", "") or ""):
            env_name = str(llm_config.api_key_env or "")
            value = (str(os.environ.get(env_name, "") or "").strip() if env_name else "") or (
                llm._env_from_user_registry(env_name) if env_name else None)
            llm._api_key = llm_config.api_key or value or None
        llm._api_base = llm_config.api_base or None
        refresh_tariff = getattr(llm, "refresh_server_text_tariff", None)
        if callable(refresh_tariff):
            refresh_tariff()

    try:
        _persist_llm_config(engine, llm_config, customer_engine=customer_engine)
    except Exception as exc:  # noqa: BLE001 - live now, not saved: the caller says so
        logger.warning("settings: routing applied live but not saved: {}", type(exc).__name__)
        return {"changes": changes, "saved": False, "save_error": type(exc).__name__}
    return {"changes": changes, "saved": True, "save_error": ""}


def _persist_llm_config(engine: Any, llm_config: Any, *, customer_engine: bool = False) -> None:
    """Write default_model / api_key_env / api_base / reasoning_effort / routing back to .ampuly llm_config.yaml.

    The key variable written is the live one -- the planner already put the catalogue's there when the default moved
    to another vendor, and kept a relay's when it did not. A customer's engine holds the customer's variable in its
    live config, and that name must never become the machine-wide default, so there the file's own is kept. A literal
    key the planner removed is removed from the file too.
    """
    import yaml

    ampuly_home = getattr(engine, "ampuly_home", None)
    if not ampuly_home:
        logger.warning("settings: no ampuly_home; routing change not persisted")
        return
    path = os.path.join(str(ampuly_home), "config", "llm_config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        data = {}
    section = data.setdefault("llm", {})
    section["default_model"] = llm_config.default_model
    if not customer_engine:
        section["api_key_env"] = str(llm_config.api_key_env or "")
    if not str(getattr(llm_config, "api_key", "") or ""):
        section.pop("api_key", None)
    section["api_base"] = str(llm_config.api_base or "")
    section["reasoning_effort"] = getattr(llm_config, "reasoning_effort", None)
    section["routing"] = dict(llm_config.routing or {})
    # Model routing phase 1. Written beside routing because they are the same kind of setting -- 「这一步用哪个
    # 模型」 -- and a save that wrote one but not the other would leave the file disagreeing with itself.
    section["tiers"] = dict(getattr(llm_config, "tiers", {}) or {})
    section["tier_params"] = {str(k): dict(v or {})
                              for k, v in dict(getattr(llm_config, "tier_params", {}) or {}).items()}
    section["task_tiers"] = dict(getattr(llm_config, "task_tiers", {}) or {})
    section["residency"] = str(getattr(llm_config, "residency", "") or "")
    section["work_window_cap"] = int(getattr(llm_config, "work_window_cap", 0) or 0)
    section["no_training_optout"] = dict(getattr(llm_config, "no_training_optout", {}) or {})
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    logger.info(
        f"settings: persisted llm config (default={llm_config.default_model}, "
        f"routing={dict(llm_config.routing or {})})"
    )


# ── F usability thin hooks (default off; no business writes) ─────────────────
# Appended for Preferences safety + connector inventory read APIs. Does not
# alter build_settings_payload / plan_model_tiers / apply_model_tiers. Main-
# session model-tier edits stay untouched. Callers must pass enabled=True.


def preferences_safety_gate(preferences_gate=None):
    """Default-off Preferences safety gate (delegate to employee_usability)."""
    from ampuly.plugins.office_ui.employee_usability import apply_preferences_safety_gate

    return apply_preferences_safety_gate(preferences_gate)


def preferences_safety_read(
    *,
    enabled: bool = False,
    preferences=None,
    task_surface_flags=None,
):
    """Default-off Preferences safety read for settings surface."""
    from ampuly.plugins.office_ui.employee_usability import preferences_safety_read_api

    return preferences_safety_read_api(
        enabled=enabled,
        preferences=preferences,
        task_surface_flags=task_surface_flags,
    )


def connector_inventory_read(*, enabled: bool = False, connectors=()):
    """Default-off connector capability inventory read for settings surface."""
    from ampuly.plugins.office_ui.employee_usability import connector_inventory_read_api

    return connector_inventory_read_api(enabled=enabled, connectors=connectors)


def usability_settings_read_payload(
    *,
    enabled: bool = False,
    preferences=None,
    task_surface_flags=None,
    connectors=(),
    pending_items=(),
):
    """Bundle Preferences safety + connector inventory + badge reads (default off).

    Never writes keys, tiers, routing, or business data. When `enabled` is
    False sides report `api_off` without running F contract validation.
    """
    prefs = preferences_safety_read(
        enabled=enabled,
        preferences=preferences,
        task_surface_flags=task_surface_flags,
    )
    inv = connector_inventory_read(enabled=enabled, connectors=connectors)
    from ampuly.plugins.office_ui.employee_usability import pending_approval_badge_read_api

    badge = pending_approval_badge_read_api(
        enabled=enabled,
        pending_items=pending_items,
        preferences=preferences,
    )
    return {
        "ok": True,
        "enabled": bool(enabled),
        "preferences_safety": prefs,
        "connector_inventory": inv,
        "pending_approval_badge": badge,
        "effects_produced": False,
        "settings_written": False,
    }


def task_center_settings_read(
    *,
    enabled: bool = False,
    project_id: str = "",
    actor_scope_ok: bool = False,
    timeline_entries=(),
    **kwargs,
):
    """Default-off task-center read for settings / WS surfaces."""
    from ampuly.plugins.office_ui.employee_task_center import task_center_read_api

    return task_center_read_api(
        enabled=enabled,
        project_id=project_id,
        actor_scope_ok=actor_scope_ok,
        timeline_entries=timeline_entries,
        **kwargs,
    )




# ── G4 / V2 surface / IM readonly / clinic binding thin hooks (default off) ──
# Appended for remaining-push. No business writes; no auto harden/install.


def bla_gray_release_gate(enabled=False):
    from ampuly.plugins.office_ui.bla_admission import apply_bla_g4_gate

    return apply_bla_g4_gate(enabled=enabled)


def bla_gray_release_read_settings(*, enabled: bool = False):
    from ampuly.plugins.office_ui.bla_admission import bla_gray_release_read

    return bla_gray_release_read(enabled=enabled)


def v2_surface_flags_read(*, enabled: bool = False):
    from ampuly.plugins.office_ui.v2_surface_flags import list_v2_surface_flags

    return list_v2_surface_flags(enabled=enabled)


def im_readonly_probe_settings(
    *,
    enabled: bool = False,
    channel: str = "feishu",
    identity_bound: bool = False,
    project_id: str = "",
    channel_configured: bool = False,
    credential_present: bool = False,
    task_id=None,
):
    from ampuly.plugins.office_ui.im_readonly_probe import probe_im_readonly

    return probe_im_readonly(
        channel=channel,
        identity_bound=identity_bound,
        project_id=project_id,
        channel_configured=channel_configured,
        credential_present=credential_present,
        task_id=task_id,
        enabled=enabled,
    )


def clinic_binding_probe_settings(
    *,
    enabled: bool = False,
    api_available: bool = False,
    owner_session: bool = False,
    action: str = "employee_binding_preview",
    api_caller=None,
    params=None,
):
    from ampuly.plugins.office_ui.clinic_binding_probe import probe_clinic_binding

    return probe_clinic_binding(
        enabled=enabled,
        api_available=api_available,
        owner_session=owner_session,
        action=action,
        api_caller=api_caller,
        params=params,
    )
