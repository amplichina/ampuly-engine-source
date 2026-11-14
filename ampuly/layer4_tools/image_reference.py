"""Reference-image editing for the native VI workflow; no ZCOOL dependency.

The legacy text-to-image branch deliberately removes lettering. Editing a
confirmed mark must instead preserve it. This opt-in branch has its own
bounded input contract and immutable output receipt. It never retries an
ambiguous paid POST. Local path checks are lexical on Windows, not a sandbox.
API: https://help.aliyun.com/zh/model-studio/qwen-image-edit-api
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from ampuly.layer4_tools.registry import ToolInvocationValidationError
from ampuly.layer4_tools.workspace_fs import (
    LexicalWorkspace,
    SecureWorkspace,
    is_model_reserved_workspace_path,
    workspace_roots_for_task,
)

ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
MODEL = "qwen-image-edit-plus-2025-12-15"
MAX_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024


def _invalid(message: str) -> ToolInvocationValidationError:
    return ToolInvocationValidationError(message, correction="Use approved local PNG/JPEG/WebP references and a new workspace-relative .png output path; never retry an unknown submission automatically.")


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _checked_path(root: Path, relative: str) -> Path:
    """Reject escaping paths, alternate streams, links and linked ancestry."""
    raw = str(relative)
    parts = raw.replace("\\", "/").split("/")
    if (not raw or Path(raw).is_absolute() or PureWindowsPath(raw).drive
            or any(p in {"", ".", ".."} or ":" in p for p in parts)
            or is_model_reserved_workspace_path(raw.replace("\\", "/"))):
        raise _invalid("reference/output path must be workspace-relative")
    target = root.joinpath(*parts)
    for path in [*reversed(target.parents), target]:
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise _invalid("linked path is not allowed for image editing")
    if not target.resolve().is_relative_to(root.resolve()):
        raise _invalid("image path escapes the output workspace")
    return target


@contextmanager
def _local_file(root: Path, path: Path, mode: str):
    """Use the existing capability, including its explicit Windows opt-in.

    The core has text IO only. Keep this small binary adapter here rather
    than changing the filesystem kernel. POSIX parents remain pinned;
    Windows retains exactly the documented lexical downgrade.
    """
    if mode not in {"rb", "xb"}:
        raise ValueError("image artifacts are read-only or create-only")
    flags = (os.O_RDONLY if mode == "rb" else os.O_WRONLY | os.O_CREAT)
    if mode == "xb":
        flags |= os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with SecureWorkspace.for_roots(str(root)) as workspace:
        target = workspace.resolve(str(path.relative_to(root)))
        if isinstance(workspace, LexicalWorkspace):
            materialized = workspace._materialize(target)
            if mode != "rb":
                materialized.parent.mkdir(parents=True, exist_ok=True)
            if materialized.exists():
                workspace._require_regular(materialized)
            fd = os.open(materialized, flags, 0o600)
        else:
            # Ask the workspace to open it. Both secure backends own this, in
            # their own primitives -- directory descriptors on POSIX, Win32
            # handles on Windows. Reaching past it to os.open(..., dir_fd=...)
            # assumed there were only two shapes in the world, and silently
            # became NotImplementedError on Windows the day a third arrived.
            fd = workspace._open_file(target, flags, create_dirs=(mode != "rb"))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _invalid("image IO requires an unlinked regular file")
            with os.fdopen(fd, mode) as handle:
                fd = -1
                yield handle
                if mode != "rb":
                    handle.flush()
                    os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)


def _read_image(root: Path, path: Path, limit: int = MAX_BYTES) -> bytes:
    with _local_file(root, path, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise _invalid("image must be a regular, unlinked file within size limit")
        blob = handle.read(limit + 1)
    if not blob or len(blob) > limit:
        raise _invalid("image is empty or exceeds size limit")
    return blob


def _mime(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    raise _invalid("reference is not a supported PNG/JPEG/WebP image")


def _download_url(raw: str) -> str:
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    # Model Studio currently returns signed OSS URLs. Fail closed on any
    # different host instead of turning a provider response into arbitrary IO.
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise _invalid("provider returned an unsafe image URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _invalid("literal IP output URLs are not allowed")
    if not host.endswith(".aliyuncs.com"):
        raise _invalid("provider output host is not an allowed OSS host")
    return raw


async def _request_edit(client: httpx.AsyncClient, body: dict, key: str) -> bytes:
    # One POST, no automatic retries, and no redirects carrying credentials.
    try:
        response = await client.post(ENDPOINT, headers={"Authorization": f"Bearer {key}"}, json=body)
    except httpx.HTTPError:
        raise RuntimeError("image edit submission outcome unknown; inspect provider history before retrying") from None
    if response.status_code != 200:
        raise RuntimeError(f"image edit HTTP {response.status_code}; inspect provider history before retrying")
    try:
        content = response.json()["output"]["choices"][0]["message"]["content"]
        urls = [item["image"] for item in content if isinstance(item, dict) and item.get("image")]
        if len(urls) != 1:
            raise ValueError("unexpected count")
        url = _download_url(urls[0])
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("image edit returned no single valid output; do not resubmit automatically") from None
    chunks = bytearray()
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise RuntimeError(f"image download HTTP {response.status_code}; do not resubmit generation")
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_OUTPUT_BYTES:
                    raise RuntimeError("image download exceeds 20 MiB; do not resubmit generation")
    except httpx.HTTPError:
        raise RuntimeError("image download failed; do not resubmit generation") from None
    if _mime(chunks) != "image/png":
        raise RuntimeError("image edit output was not PNG")
    return bytes(chunks)


def _prepare(task: Any, paths: list[str], output: str, hashes: list[str] | None) -> tuple[Path, list[dict], list[dict]]:
    if task is None:
        raise _invalid("reference editing requires a task workspace")
    workspace, output_root = workspace_roots_for_task(task)
    if not workspace or not output_root:
        raise _invalid("reference editing requires explicit workspace and output roots")
    root = Path(output_root)
    if not root.is_absolute() or not root.is_dir() or not root.resolve().is_relative_to(Path(workspace).resolve()):
        raise _invalid("invalid output workspace")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 3 or any(not isinstance(p, str) for p in paths):
        raise _invalid("reference_images requires one to three local paths")
    if hashes is not None and (not isinstance(hashes, list) or len(hashes) != len(paths)):
        raise _invalid("reference_sha256 must match the reference image count")
    target = _checked_path(root, output)
    if target.suffix.lower() != ".png":
        raise _invalid("reference editing outputs PNG only")
    refs, contents = [], []
    for index, raw in enumerate(paths):
        path = _checked_path(root, raw)
        if path == target:
            raise _invalid("output may not overwrite a reference")
        blob = _read_image(root, path)
        digest = _digest(blob)
        if hashes is not None and hashes[index] != digest:
            raise _invalid("reference hash changed; confirm the new brand asset version first")
        refs.append({"path": raw, "sha256": digest})
        contents.append({"image": f"data:{_mime(blob)};base64,{base64.b64encode(blob).decode('ascii')}"})
    return root, refs, contents


async def generate_reference_image(*, prompt: str, file_path: str, reference_images: list[str],
                                   reference_sha256: list[str] | None, provider: str, model: str,
                                   size: str, negative_prompt: str, n: int, seed: int | None,
                                   task: Any) -> dict[str, Any]:
    if provider not in {"qwen", "dashscope", "wanx"}:
        raise _invalid("reference editing requires explicit provider='qwen'; auto fallback is disabled")
    if model and model != MODEL:
        raise _invalid(f"reference adapter currently supports only {MODEL}")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2400:
        raise _invalid("reference prompt must contain 1–2400 characters")
    if type(n) is not int or n != 1:
        raise _invalid("reference editing creates exactly one image per immutable output path")
    try:
        width, height = map(int, size.replace("x", "*").split("*"))
    except (ValueError, AttributeError):
        raise _invalid("size must be WIDTH*HEIGHT") from None
    if not (512 <= width <= 2048 and 512 <= height <= 2048):
        raise _invalid("reference edit dimensions must be 512–2048")
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 2147483647):
        raise _invalid("seed must be an integer from 0 to 2147483647")
    from ampuly.layer4_tools.image_gen import _image_key_env

    key_env = _image_key_env(task)
    key = os.environ.get(key_env, "").strip()
    if not key:
        raise _invalid(f"{key_env} is not configured")
    root, refs, contents = await asyncio.to_thread(_prepare, task, reference_images, file_path, reference_sha256)
    parameters = {"n": 1, "size": f"{width}*{height}", "prompt_extend": False,
                  "negative_prompt": negative_prompt[:500]}
    if seed is not None:
        parameters["seed"] = seed
    body = {"model": MODEL, "input": {"messages": [{"role": "user", "content": [*contents, {"text": prompt}]}]}, "parameters": parameters}
    receipt = {"mode": "reference_edit", "provider": "qwen", "model": MODEL,
               "prompt": prompt, "parameters": parameters, "reference_assets": refs}
    fingerprint = _digest(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode())

    def cached_receipt() -> dict | None:
        target = _checked_path(root, file_path)
        sidecar = _checked_path(root, file_path + ".prompt.json")
        if sidecar.exists():
            previous = json.loads(_read_image(root, sidecar, 100_000).decode("utf-8"))
            if previous.get("request_sha256") != fingerprint or previous.get("status") != "succeeded":
                raise _invalid("output already claimed or outcome unknown; inspect the prior attempt")
            if _digest(_read_image(root, target, MAX_OUTPUT_BYTES)) != previous.get("output_sha256"):
                raise _invalid("cached output changed; manual review required")
            return previous
        return None

    def claim() -> dict | None:
        previous = cached_receipt()
        if previous is not None:
            return previous
        target = _checked_path(root, file_path)
        if target.exists():
            raise _invalid("output already exists without a verified receipt")
        attempt = _checked_path(root, file_path + ".attempt.json")
        if attempt.exists():
            raise _invalid("output already claimed or outcome unknown; inspect the prior attempt")
        # Exclusive creation arbitrates concurrent calls with the same path.
        # Attempt receipts are separate from completed .prompt.json files so
        # existing image billing/artifact counters do not count failures as images.
        with _local_file(root, attempt, "xb") as handle:
            handle.write(json.dumps({**receipt, "request_sha256": fingerprint, "status": "outcome_unknown"}, ensure_ascii=False).encode("utf-8"))
        return None

    previous = await asyncio.to_thread(cached_receipt)
    if previous is None:
        from ampuly.layer4_tools.spend_guard import spend_guard

        with spend_guard(task, kind="image", amount=1, note="image_generate:qwen:reference_edit") as guard:
            try:
                previous = await asyncio.to_thread(claim)
            except BaseException:
                # This block only arbitrates local files; no request was sent.
                guard.release(reason="reference output claim failed before dispatch")
                raise
            if previous is not None:
                guard.release(reason="verified cached image; no request dispatched")
            else:
                async with httpx.AsyncClient(timeout=180.0, follow_redirects=False) as client:
                    blob = await _request_edit(client, body, key)

    if previous is None:
        def save() -> None:
            target = _checked_path(root, file_path)
            # Never overwrite customer files or a different generation.
            with _local_file(root, target, "xb") as handle:
                handle.write(blob)
            sidecar = _checked_path(root, file_path + ".prompt.json")
            with _local_file(root, sidecar, "xb") as handle:
                handle.write(json.dumps({**receipt, "request_sha256": fingerprint, "status": "succeeded", "output_sha256": _digest(blob)}, ensure_ascii=False).encode("utf-8"))
        await asyncio.to_thread(save)
    return {"provider": "qwen", "model": MODEL, "mode": "reference_edit", "count": 1,
            "images": [str(root / file_path)], "size": size, "reference_assets": refs,
            "output_sha256": previous["output_sha256"] if previous is not None else _digest(blob),
            "prompt_used": prompt, "request_sha256": fingerprint, "cached": previous is not None}
