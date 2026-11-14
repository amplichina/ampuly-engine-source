"""Image generation tool (生图) — provider adapters for role use.

Providers:
- ``qwen``  — 通义万相 / DashScope text-to-image (``DASHSCOPE_API_KEY``).
- ``jimeng`` — 即梦 via Volcano Engine visual API (``VOLC_ACCESSKEY`` +
  ``VOLC_SECRETKEY``, standard Volcano HMAC-V4 request signing).

Images are written into the task workspace (same path resolution as the file
tools), so downstream roles (QA, delivery) handle them like any other
artifact. Keys come from environment variables only, per distribution rule #3.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import os
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from ampuly.layer4_tools.file_ops import _resolve_task_path
from ampuly.layer4_tools.image_prompt_guard import (
    load_style_template,
    merge_negative,
    record_generation,
    render_style_prompt,
    sanitize_prompt,
)
from ampuly.layer4_tools.registry import (
    COMPANY_EFFECT_STRUCTURED_PATHS,
    ToolDefinition,
    ToolInvocationValidationError,
)

QWEN_KEY_ENV = "DASHSCOPE_API_KEY"
JIMENG_AK_ENV = "VOLC_ACCESSKEY"
JIMENG_SK_ENV = "VOLC_SECRETKEY"

_QWEN_DEFAULT_MODEL = "wanx2.1-t2i-turbo"
_JIMENG_DEFAULT_REQ_KEY = "jimeng_high_aes_general_v21_L"

_DASHSCOPE_SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
_DASHSCOPE_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

_VOLC_HOST = "visual.volcengineapi.com"
_VOLC_REGION = "cn-north-1"
_VOLC_SERVICE = "cv"

_MAX_IMAGES = 4
_POLL_INTERVAL_S = 3.0
_POLL_TIMEOUT_S = 180.0


def _parse_size(size: str) -> tuple[int, int]:
    raw = str(size or "").replace("x", "*").replace("×", "*").strip()
    try:
        width_s, height_s = raw.split("*", 1)
        width, height = int(width_s), int(height_s)
    except ValueError:
        raise ToolInvocationValidationError(
            f"invalid size {size!r}; use WIDTH*HEIGHT, e.g. 1024*1024",
            correction="Pass size as 'WIDTH*HEIGHT', e.g. '1024*1024'.",
        ) from None
    if not (256 <= width <= 2048 and 256 <= height <= 2048):
        raise ToolInvocationValidationError(
            f"size {width}*{height} out of range (256–2048 per side)",
            correction="Choose side lengths between 256 and 2048.",
        )
    return width, height


def _jimeng_configured() -> bool:
    return bool(os.environ.get(JIMENG_AK_ENV)) and bool(os.environ.get(JIMENG_SK_ENV))


def _qwen_configured() -> bool:
    return bool(os.environ.get(QWEN_KEY_ENV))


def _pick_provider(provider: str) -> str:
    normalized = str(provider or "auto").strip().lower()
    if normalized in {"qwen", "wanx", "dashscope"}:
        if not _qwen_configured():
            raise ToolInvocationValidationError(
                f"qwen image provider needs {QWEN_KEY_ENV}",
                correction="Configure the DashScope key in 设置→模型与密钥, or use provider='jimeng'.",
            )
        return "qwen"
    if normalized in {"jimeng", "volc", "volcengine"}:
        if not _jimeng_configured():
            raise ToolInvocationValidationError(
                f"jimeng provider needs {JIMENG_AK_ENV} and {JIMENG_SK_ENV}",
                correction="Configure the Volcano Engine AK/SK in 设置→模型与密钥, or use provider='qwen'.",
            )
        return "jimeng"
    if normalized in {"auto", ""}:
        if _jimeng_configured():
            return "jimeng"
        if _qwen_configured():
            return "qwen"
        raise ToolInvocationValidationError(
            "no image provider configured",
            correction=(
                f"Configure {JIMENG_AK_ENV}/{JIMENG_SK_ENV} (即梦) or {QWEN_KEY_ENV} "
                "(千问生图) in 设置→模型与密钥."
            ),
        )
    raise ToolInvocationValidationError(
        f"unknown provider {provider!r}",
        correction="Use provider 'auto', 'jimeng', or 'qwen'.",
    )


# ── DashScope (通义万相 / qwen) ──────────────────────────────────────────────

async def _qwen_generate(
    client: httpx.AsyncClient,
    *,
    prompt: str,
    negative_prompt: str,
    model: str,
    width: int,
    height: int,
    n: int,
    seed: int | None = None,
    key_env: str = QWEN_KEY_ENV,
) -> list[bytes]:
    headers = {
        "Authorization": f"Bearer {os.environ[key_env]}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    body: dict[str, Any] = {
        "model": model or _QWEN_DEFAULT_MODEL,
        "input": {"prompt": prompt},
        "parameters": {"size": f"{width}*{height}", "n": n},
    }
    if negative_prompt:
        body["input"]["negative_prompt"] = negative_prompt
    if seed is not None:
        body["parameters"]["seed"] = int(seed)
    resp = await client.post(_DASHSCOPE_SUBMIT_URL, headers=headers, json=body)
    if resp.status_code != 200:
        raise RuntimeError(f"DashScope submit failed ({resp.status_code}): {resp.text[:300]}")
    task_id = str(resp.json().get("output", {}).get("task_id", "") or "")
    if not task_id:
        raise RuntimeError(f"DashScope returned no task_id: {resp.text[:300]}")

    deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_S
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        poll = await client.get(
            _DASHSCOPE_TASK_URL.format(task_id=task_id),
            headers={"Authorization": headers["Authorization"]},
        )
        output = poll.json().get("output", {}) if poll.status_code == 200 else {}
        status = str(output.get("task_status", "") or "")
        if status == "SUCCEEDED":
            urls = [
                str(item.get("url", "") or "")
                for item in output.get("results", [])
                if isinstance(item, dict) and item.get("url")
            ]
            if not urls:
                raise RuntimeError(f"DashScope task succeeded but returned no image urls: {json.dumps(output)[:300]}")
            images: list[bytes] = []
            for url in urls[:n]:
                download = await client.get(url)
                download.raise_for_status()
                images.append(download.content)
            return images
        if status in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"DashScope task {status}: {json.dumps(output, ensure_ascii=False)[:300]}")
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"DashScope task timed out after {int(_POLL_TIMEOUT_S)}s (last status: {status or 'unknown'})")


# ── Volcano Engine (即梦) ────────────────────────────────────────────────────

def _volc_sign_headers(body: bytes, query: dict[str, str]) -> dict[str, str]:
    """Standard Volcano Engine HMAC-SHA256 V4 request signature."""
    access_key = os.environ[JIMENG_AK_ENV]
    secret_key = os.environ[JIMENG_SK_ENV]
    now = datetime.datetime.now(datetime.timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items())
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:{_VOLC_HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        "POST",
        "/",
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    credential_scope = f"{short_date}/{_VOLC_REGION}/{_VOLC_SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(secret_key.encode(), short_date)
    k_region = _hmac(k_date, _VOLC_REGION)
    k_service = _hmac(k_region, _VOLC_SERVICE)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Content-Type": "application/json",
        "Host": _VOLC_HOST,
        "X-Content-Sha256": payload_hash,
        "X-Date": x_date,
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


async def _jimeng_generate(
    client: httpx.AsyncClient,
    *,
    prompt: str,
    model: str,
    width: int,
    height: int,
    n: int,
) -> list[bytes]:
    query = {"Action": "CVProcess", "Version": "2022-08-31"}
    images: list[bytes] = []
    for _ in range(n):
        payload = {
            "req_key": model or _JIMENG_DEFAULT_REQ_KEY,
            "prompt": prompt,
            "width": width,
            "height": height,
            "return_url": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = _volc_sign_headers(body, query)
        resp = await client.post(
            f"https://{_VOLC_HOST}/",
            params=query,
            content=body,
            headers=headers,
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        code = data.get("code")
        if resp.status_code != 200 or code not in (10000, "10000"):
            message = str(data.get("message", "") or resp.text[:300])
            raise RuntimeError(f"Jimeng CVProcess failed (http {resp.status_code}, code {code}): {message[:300]}")
        inner = data.get("data", {}) or {}
        b64_list = inner.get("binary_data_base64") or []
        urls = inner.get("image_urls") or []
        if b64_list:
            images.append(base64.b64decode(b64_list[0]))
        elif urls:
            download = await client.get(str(urls[0]))
            download.raise_for_status()
            images.append(download.content)
        else:
            raise RuntimeError(f"Jimeng returned no image payload: {json.dumps(data, ensure_ascii=False)[:300]}")
    return images


# ── Tool entry point ─────────────────────────────────────────────────────────

def _ampuly_home_for_templates() -> Any:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home()


def _image_key_env(task: Any | None) -> str:
    """Which environment variable holds the key for *this* customer's images.

    A customer with their own reference must not fall back to the machine
    default when their variable is unset -- that would silently draw on
    somebody else's account. With no reference at all, the machine default is
    what has always been used, and stays.
    """
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    customer_id = str(metadata.get("customer_id", "") or "")
    environment_id = str(metadata.get("environment_id", "") or "")
    if not customer_id and not environment_id:
        return QWEN_KEY_ENV
    try:
        from pathlib import Path as _Path

        from ampuly.core.config import get_ampuly_home
        from ampuly.plugins.office_ui.business_store import BusinessStore
        from ampuly.plugins.office_ui.credential_refs import (
            CredentialUnavailableError,
            require_env_var,
        )

        home = str(metadata.get("ampuly_home", "") or "") or str(get_ampuly_home())
        home_path = _Path(home)
        # Refuse before BusinessStore. A missing directory is not an empty
        # mapping. On Windows ``/nowhere/at/all`` is the current drive plus
        # that tail, and opening the store would mkdir it, create business.db,
        # then look like "no reference" and bill the machine key.
        if not home_path.is_dir():
            raise ToolInvocationValidationError(
                "Customer image credential mapping is unavailable",
                correction="Restore access to the credential mapping before retrying.",
            )
        business = BusinessStore(home_path)
    except Exception:
        # A scoped call cannot guess whose account to spend when its mapping
        # store cannot be opened. This is not the unconfigured-customer case.
        raise ToolInvocationValidationError(
            "Customer image credential mapping is unavailable",
            correction="Restore access to the credential mapping before retrying.",
        ) from None
    try:
        return require_env_var(
            business, purpose="image", customer_id=customer_id,
            environment_id=environment_id, default_env=QWEN_KEY_ENV,
        )
    except CredentialUnavailableError as exc:
        named = exc.resolution.env_var
        raise ToolInvocationValidationError(
            str(exc),
            correction=(
                f"Set {named} in the environment, or change the reference."
                if named else "Check this project's credential references, then try again."
            ),
        ) from exc


async def image_generate(
    prompt: str = "",
    file_path: str = "",
    provider: str = "auto",
    model: str = "",
    size: str = "1024*1024",
    negative_prompt: str = "",
    n: int = 1,
    style: str = "",
    slots: dict[str, Any] | None = None,
    seed: int | None = None,
    task: Any | None = None,
    reference_images: list[str] | None = None,
    reference_sha256: list[str] | None = None,
) -> dict[str, Any]:
    if reference_images is not None:
        from ampuly.layer4_tools.image_reference import generate_reference_image

        if style or slots:
            raise ToolInvocationValidationError(
                "reference editing cannot use the legacy no-text style templates",
                correction="Pass a reference-edit prompt and local reference_images, without style/slots.",
            )
        return await generate_reference_image(
            prompt=prompt, file_path=file_path, reference_images=reference_images,
            reference_sha256=reference_sha256, provider=provider, model=model,
            size=size, negative_prompt=negative_prompt, n=n, seed=seed, task=task,
        )
    if reference_sha256 is not None:
        raise ToolInvocationValidationError("reference_sha256 requires reference_images", correction="Provide local reference_images or omit reference_sha256.")
    prompt = str(prompt or "").strip()
    style = str(style or "").strip()
    slots_used: dict[str, str] = {}
    if style:
        # Deterministic fill: the template owns composition, palette and the
        # negative list; the caller only names the imagery and colours.
        try:
            template = load_style_template(_ampuly_home_for_templates(), style)
        except FileNotFoundError as exc:
            raise ToolInvocationValidationError(
                str(exc),
                correction="Pass a user-provided style skill that ships a template.yaml, or omit style and pass a prompt.",
            ) from exc
        rendered_prompt, rendered_negative, slots_used = render_style_prompt(template, slots)
        prompt = rendered_prompt if not prompt else f"{rendered_prompt}, {prompt}"
        negative_prompt = ", ".join(x for x in (rendered_negative, str(negative_prompt or "")) if x.strip())
    if not prompt:
        raise ToolInvocationValidationError(
            "prompt is required",
            correction="Provide a non-empty prompt, or a style with slots (品牌意象 / 必含元素 / 主色 / 辅色).",
        )
    prompt, removed_words = sanitize_prompt(prompt)
    negative_prompt = merge_negative(negative_prompt)
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = None
    rel_path = str(file_path or "").strip()
    if not rel_path:
        raise ToolInvocationValidationError(
            "file_path is required",
            correction="Provide a workspace-relative output path, e.g. 'assets/logo_a.png'.",
        )
    n = max(1, min(int(n or 1), _MAX_IMAGES))
    width, height = _parse_size(size)
    resolved_provider = _pick_provider(provider)

    # Hold the order's budget across the paid call. A failure after the
    # request left is marked unknown rather than refunded: the image may
    # already be drawn and billed, and refunding would invite a second
    # paid attempt for it. A run with no budget is unlimited, as before.
    from ampuly.layer4_tools.spend_guard import spend_guard

    key_env = _image_key_env(task)

    with spend_guard(task, kind="image", amount=n, note=f"image_generate:{resolved_provider}"):
        async with httpx.AsyncClient(timeout=60.0) as client:
            if resolved_provider == "qwen":
                images = await _qwen_generate(
                    client,
                    prompt=prompt,
                    negative_prompt=str(negative_prompt or "").strip(),
                    model=str(model or "").strip(),
                    width=width,
                    height=height,
                    n=n,
                    seed=seed,
                    key_env=key_env,
                )
            else:
                images = await _jimeng_generate(
                    client,
                    prompt=prompt,
                    model=str(model or "").strip(),
                    width=width,
                    height=height,
                    n=n,
                )

    base = _resolve_task_path(rel_path, task)
    base.parent.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for index, blob in enumerate(images):
        if len(images) == 1:
            target = base
        else:
            target = base.with_name(f"{base.stem}_{index + 1}{base.suffix or '.png'}")
        target.write_bytes(blob)
        saved.append(str(target.resolve()))
        record_generation(target, {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "removed_words": removed_words,
            "style": style,
            "slots": slots_used,
            "provider": resolved_provider,
            "model": str(model or "").strip(),
            "size": f"{width}*{height}",
            "seed": seed,
        })
    logger.info(
        f"image_generate: provider={resolved_provider} images={len(saved)} size={width}*{height}"
        + (f" style={style}" if style else "")
        + (f" removed={removed_words}" if removed_words else "")
    )
    return {
        "provider": resolved_provider,
        "images": saved,
        "count": len(saved),
        "size": f"{width}*{height}",
        "prompt_used": prompt,
        "negative_prompt_used": negative_prompt,
        "removed_words": removed_words,
        "style": style,
        "slots": slots_used,
        "seed": seed,
    }


def create_image_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="image_generate",
            description=(
                "Generate image(s) and save them into the task workspace. Providers: "
                "'jimeng' (即梦/火山引擎), 'qwen' (千问生图/通义万相), or 'auto'. Preferred use: pass "
                "`style` (a user-provided style skill id) plus `slots` "
                "and let the template compose the prompt, or provide a prompt directly. "
                "The prompt guard removes lettering terms, applies a baseline negative prompt, and a "
                "<file>.prompt.json sidecar records the exact prompt and seed."
                " For reference-image editing, pass reference_images (1–3 local paths), provider='qwen', n=1 "
                "and a new .png path. This separate mode preserves approved logos/text, records reference hashes, "
                "and refuses ambiguous retries. reference_sha256 optionally pins approved asset versions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description (Chinese or English). Optional when `style` is given; then it is appended to the template."},
                    "style": {"type": "string", "description": "User-provided style skill id whose template.yaml composes the prompt"},
                    "slots": {"type": "object", "description": "Named values required by the selected template", "additionalProperties": {"type": "string"}},
                    "seed": {"type": "integer", "description": "Optional seed for reproducible output (qwen)"},
                    "file_path": {"type": "string", "description": "Workspace-relative output path, e.g. assets/logo_a.png; multiple images get _1.._n suffixes"},
                    "provider": {"type": "string", "enum": ["auto", "jimeng", "qwen"], "default": "auto"},
                    "model": {"type": "string", "description": "Optional model override (qwen model name or jimeng req_key)"},
                    "size": {"type": "string", "default": "1024*1024", "description": "WIDTH*HEIGHT, 256–2048 per side"},
                    "negative_prompt": {"type": "string", "description": "What to avoid (qwen only)"},
                    "n": {"type": "integer", "default": 1, "minimum": 1, "maximum": _MAX_IMAGES},
                    "reference_images": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3, "description": "Reference edit mode: 1–3 local image paths relative to the task output root; no URLs. Explicit provider=qwen, n=1 required."},
                    "reference_sha256": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3, "description": "Optional approved SHA256 hashes in reference order; any mismatch blocks execution."},
                },
                "required": ["file_path"],
            },
            func=image_generate,
            category="media",
            concurrency_safe=True,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_STRUCTURED_PATHS,
            # Declared, because an undeclared tool is treated as having unknown
            # effects: the operator is asked to approve "a tool with no declared
            # native permission effects" — which says nothing about what it will
            # do — and the approval grants it a full-access sandbox override,
            # exempting it from the confinement every other tool runs under.
            # What it actually does is call an image API and write the result
            # into the task workspace.
            permission_effects=["network_access", "workspace_write", "local_read"],
        ),
    ]
