"""企业微信客户通道：受审批控制的订单成果交付。

An order that came in through 企业微信客服 (source
``wecom_kf``) and has entered 客户验收中 (``delivering``: the owner released its last step and the run finished) gets its
delivery to the customer drafted here -- and sent only after the owner releases it on a Feishu card:

1. **draft** (the duty round, ``announce_delivery_cards``). The files are the order's accepted delivery, by the rules
   the acceptance round already keeps (``collect_candidates``): the frozen copies of its round when that round was
   already passed with its files frozen; else the files the round's evidence is bound to (by content); else the
   receipt rule of ``acceptance_round.receipt_evidence`` -- the deliverables of the order's last step, found by the one
   order-folder rule -- walking back past last steps that deliver only records (验收记录 / 交付清单 /
   检查单) to the last step that delivered a customer file. Never an earlier step's drafts beside it, and never a
   candidate the flow did not choose: a folder whose ``selection.json`` names its current file (``selected_path``, the
   flows' own format) gives only that file. Only what a customer reads goes (pictures, PDF, design and office files --
   never the step records: ``.json``, ``.md``, ``.txt``...), the overview first (``overview`` / 总图 / 展示大图), then
   in the step's declared order. 企业微信's limits decide how each goes: a JPG / PNG up to 10 MB as a picture, anything
   else up to 20 MB as a file, a larger one is listed for the owner to forward by hand. They are copied into the order's
   package (``delivery-exports/<order>/wecom-<n>/``, with a manifest of each copy's SHA-256 and the file it came from)
   -- that package is what is sent, and what the owner forwards by hand when the system may not send. At most 4 files go
   with the message (企业微信: 5 messages after the customer's last one); with more, the card has a chooser with the first
   4 ticked, never a silent ZIP. The message is one plain sentence: what is delivered, how many files follow, how to
   confirm or ask for changes.
2. **the card** (``delivery_card.build_wecom_delivery_card``): the order, the customer in words, whether 企业微信 would let
   the system send right now, the message, the files, the package; 放行发给客户 / 先不发. The owner only.
3. **release** (``GateHooks.answer``): the package's files are read and each checked against its digest; the channel
   (``WecomChannel.send_delivery``) asks 企业微信 for the session state, checks the window (W1 ``can_send``), uploads
   every file first, then sends the message and the files, each recorded. Nothing sent (the 48 hours are over, the 5
   are used, a person has the session, no link to the relay, an upload refused) -> the card keeps its buttons and its
   thread says why and where the package is, to forward by hand in 企业微信. Something sent -> the card reads the outcome;
   a message with no answer is 结果未知 and never sent again, not even by a later press (the press is recorded before the
   first send, ``SENDING_EVENT``). After anything went, the acceptance round is submitted with the delivered files as its
   evidence (the customer's 确认 is then the round's decision -- ``wecom_channel`` records it, never the close). The
   evidence names the order's own file a package copy came from while its bytes are still the ones sent -- the file the
   owner's own 确认收货并结案 names -- so freezing the round copies the same path it would; only a file changed since is
   bound to its package copy instead.
4. **reads** for the console: the conversation of an order (read only, ``conversation_view``) and the 客户确认 lane's
   facts (``WecomKfStore.lane_facts``, read by ``lane`` for ``project_connections``).

Nothing here runs unless the owner switched the channel on (``wecom_channel.switch_on``) and the Feishu group is set up;
nothing reaches 企业微信 except through the channel's relay link. "The console" below is a ``host.ConsoleHost``, or the
console's handler behind the console's adapter (``host.as_console_host``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from loguru import logger

from ampuly.core.order_layout import DELIVERY_EXPORTS, DELIVERY_HISTORY, ORDERS, OUTPUT
from ampuly.plugins.office_ui.feishu_gate import GateSpec, delivery_spell, order_inputs_from_fields
from ampuly.plugins.wecom_service.delivery_card import (
    WECOM_DELIVERY_KIND,
    WECOM_DELIVERY_LABEL,
    WECOM_DELIVERY_MAX_FILES,
    WECOM_HOLD,
    WECOM_RELEASE,
    wecom_delivery_key,
    wecom_delivery_target,
)
from ampuly.plugins.wecom_service.host import as_console_host
from ampuly.plugins.wecom_service.wecom_kf import IMAGE_CONTENT_TYPES, MEDIA_MAX_BYTES, MEDIA_MIN_BYTES_EXCLUSIVE

# What a customer reads. The step records (.json selections, .md check lists, .txt notes ...) never go.
CUSTOMER_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".svg", ".ai", ".eps", ".psd", ".tif", ".tiff",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".mp4", ".mov",
})
# The files a package lists at most (the chooser's options); the rest are counted on the card.
MAX_LISTED = 12
MAX_TOO_LARGE_LISTED = 10
# How long a file's name in the package (and to the customer) may be: short enough that the package copy, and the
# acceptance round's frozen copy of a file changed after the draft, stay well inside Windows' 260-character paths.
NAME_MAX_CHARS = 60
# A step's current choice among its candidates (``selection.json``: ``selected_path``), read when small.
SELECTION_FILE = "selection.json"
SELECTION_MAX_BYTES = 64 * 1024
MANIFEST = "manifest.json"
PACKAGE_SCHEMA = 1
# The press recorded before the first message goes, and what it came to. A press that finds the first without the
# second (the console stopped mid-send) is told the outcome is unknown and sends nothing.
SENDING_EVENT = "wecom_delivery_sending"
SETTLED_EVENT = "wecom_delivery_settled"
# How many delivery cards one duty round sends at most (like the close cards).
CARDS_PER_ROUND = 5
EVIDENCE_NOTE = "经企业微信发给客户的交付"
_OVERVIEW_WORDS = ("overview", "总图", "总览", "展示大图", "整套")
_SAFE_ORDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILE_NAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
_MB = 1024 * 1024


class PackageProblem(Exception):
    """The package cannot be read or a file in it is no longer the one drafted. The text is the owner's."""


# ── the files ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One delivered file the customer may get: where to copy it from, how the owner knows it, where it came from."""

    source: str          # workspace-relative POSIX path of the bytes to copy
    shown: str           # the path as the order delivered it, below its output tree (for the name and the overview rule)
    step: str = ""       # the step's name (or id) that declared it
    order: int = 0       # the step's place in the flow
    rank: int = 0        # the deliverable's place in the step's list (the declared order), or the evidence's
    size: int = 0
    sha256: str = ""     # known already for a frozen round's copy and for the round's evidence


def customer_facing(path: str) -> bool:
    """A file a customer reads: a known customer format, no hidden part."""
    parts = PurePosixPath(str(path or "")).parts
    if not parts or any(part.startswith(".") for part in parts):
        return False
    return PurePosixPath(parts[-1]).suffix.lower() in CUSTOMER_SUFFIXES


def is_overview(candidate: Candidate) -> bool:
    """The overview goes first: a file whose path says so (``overview/``, 总图 ...). Its step's name does not count --
    every file of the delivery comes from one step, which may well be called 「整套交付」."""
    text = str(candidate.shown or "").lower()
    return any(word in text for word in _OVERVIEW_WORDS)


def media_kind(name: str, size: int) -> tuple[str, str] | None:
    """How 企业微信 takes this file: ("image", content type) for a JPG / PNG up to 10 MB, ("file", ...) for anything up to
    20 MB, None when it is too large (or too small) to send."""
    suffix = PurePosixPath(str(name or "")).suffix.lower()
    if size <= MEDIA_MIN_BYTES_EXCLUSIVE:
        return None
    if suffix in IMAGE_CONTENT_TYPES and size <= MEDIA_MAX_BYTES["image"]:
        return "image", IMAGE_CONTENT_TYPES[suffix]
    if size <= MEDIA_MAX_BYTES["file"]:
        return "file", "application/octet-stream"
    return None


def ranked(candidates: Iterable[Candidate]) -> list[Candidate]:
    """The overview first, then the flow's order, then the declared order within a step, then the path."""
    unique: dict[str, Candidate] = {}
    for item in candidates:
        unique.setdefault(os.path.normcase(item.source), item)
    return sorted(unique.values(), key=lambda item: (not is_overview(item), item.order, item.rank, item.shown))


def order_shown(path: str, order_id: str) -> str:
    """A workspace-relative path as the order delivered it: below ``orders/<order>/output/`` (or the order folder)."""
    text = str(path or "").replace("\\", "/")
    for prefix in (f"{ORDERS}/{order_id}/{OUTPUT}/", f"{ORDERS}/{order_id}/"):
        if order_id and text.startswith(prefix):
            return text[len(prefix):]
    return text


def selected_name(root: Path, folder: Path) -> str | None:
    """The file a step's ``selection.json`` in ``folder`` names as its current choice (``selected_path``, the format the
    workflows write), as a file name; None when the folder has no readable selection naming one."""
    path = folder / SELECTION_FILE
    try:
        if not _plain(root, path) or not path.is_file() or path.stat().st_size > SELECTION_MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    chosen = data.get("selected_path") if isinstance(data, dict) else None
    if not isinstance(chosen, str) or not chosen.strip():
        return None
    return PurePosixPath(chosen.strip().replace("\\", "/")).name or None


def current_only(root: Path, candidates: list[Candidate]) -> list[Candidate]:
    """Each folder's current file only, where the folder's ``selection.json`` names one: the candidates the flow did not
    choose never go. A folder without a selection keeps all its files; one whose selection names a file that is not
    there gives none (the owner forwards by hand what the flow has not settled)."""
    choices: dict[str, str | None] = {}
    kept: list[Candidate] = []
    for item in candidates:
        path = root.joinpath(*item.source.split("/"))
        folder = os.path.normcase(str(path.parent))
        if folder not in choices:
            choices[folder] = selected_name(root, path.parent)
        chosen = choices[folder]
        if chosen is None or os.path.normcase(path.name) == os.path.normcase(chosen):
            kept.append(item)
    return kept


def _evidence_candidates(root: Path, order_id: str, evidence: Iterable[Any]) -> list[Candidate]:
    """The customer files the round's evidence is bound to (path and digest), in the evidence's order."""
    from ampuly.plugins.office_ui.business_store import acceptance_evidence_path

    found: list[Candidate] = []
    for rank, item in enumerate(list(evidence or [])):
        if not isinstance(item, dict):
            continue
        path = acceptance_evidence_path(item)
        digest = str(item.get("artifact_hash", "") or "").strip().lower()
        shown = order_shown(path, order_id)
        if not path or not digest or not customer_facing(shown) or path.startswith(f"{DELIVERY_HISTORY}/"):
            continue
        try:
            size = int(root.joinpath(*path.split("/")).stat().st_size)
        except (OSError, ValueError):
            continue
        found.append(Candidate(source=path, shown=shown, rank=rank, size=size, sha256=digest))
    return found


def _last_step_files(root: Path, order_id: str, steps: list[dict[str, Any]] | None) -> list[Candidate]:
    """The receipt rule (``acceptance_round.receipt_evidence``): the deliverables of the order's last step, walking back
    past last steps that delivered no customer file (only records); each folder's current file only."""
    from ampuly.plugins.office_ui.order_folder import deliverables_of, resolve

    order_root = root / ORDERS / order_id
    ordered = [step for step in list(steps or []) if isinstance(step, dict)]
    for index in range(len(ordered) - 1, -1, -1):
        step = ordered[index]
        name = str(step.get("name", "") or step.get("id", "") or "")
        found: list[Candidate] = []
        for rank, pattern in enumerate(deliverables_of(step)):
            for path in sorted(resolve(order_root, pattern).paths, key=lambda item: item.as_posix()):
                try:
                    relative = path.relative_to(root).as_posix()
                    info = path.stat()
                except (OSError, ValueError):
                    continue
                if not customer_facing(relative) or path.is_symlink():
                    continue
                found.append(Candidate(source=relative, shown=order_shown(relative, order_id), step=name, order=index,
                                       rank=rank, size=int(info.st_size)))
        if found:
            return current_only(root, found)
    return []


def collect_candidates(root: Path, order_id: str, steps: list[dict[str, Any]] | None,
                       frozen_files: list[dict[str, Any]] | None = None,
                       evidence: list[dict[str, Any]] | None = None) -> list[Candidate]:
    """The order's accepted delivery a customer may get, ranked: a passed round's verified frozen copies
    (``frozen_files``); else the customer files the round's evidence is bound to (``evidence``); else the last step's
    deliverables by the receipt rule, each folder's current choice only (``_last_step_files``)."""
    if frozen_files:
        found: list[Candidate] = []
        for item in frozen_files:
            source = str(item.get("archive_path", "") or "")
            shown = order_shown(str(item.get("source_path", "") or ""), order_id)
            if source and customer_facing(shown):
                found.append(Candidate(source=source, shown=shown, size=int(item.get("size", 0) or 0),
                                       sha256=str(item.get("sha256", "") or "")))
        return ranked(found)
    from_evidence = _evidence_candidates(root, order_id, evidence or [])
    if from_evidence:
        return ranked(from_evidence)
    return ranked(_last_step_files(root, order_id, steps))


def _display_names(candidates: list[Candidate]) -> list[str]:
    """A name each file goes to the customer under: its own when unique, else with its folders in front."""
    base = [PurePosixPath(item.shown).name for item in candidates]
    names: list[str] = []
    for item, name in zip(candidates, base):
        if base.count(name) > 1:
            parts = [part for part in PurePosixPath(item.shown).parts if part not in (OUTPUT,)]
            name = "-".join(parts[-3:]) if len(parts) > 1 else name
        clean = _FILE_NAME_BAD.sub("_", name).strip() or "file"
        names.append(clean[-NAME_MAX_CHARS:])
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        count = seen.get(name, 0)
        seen[name] = count + 1
        if count:
            stem, suffix = os.path.splitext(name)
            name = f"{stem}-{count + 1}{suffix}"
        unique.append(name)
    return unique


def delivery_text(title: str, count: int) -> str:
    """The one message that goes before the files: what is delivered, how many files follow, how to confirm."""
    what = f"您的「{title}」" if str(title or "").strip() else "您的订单"
    files = f"这次一共发给您 {count} 个文件，就在这条消息后面" if count else "交付的文件由负责的同事另外发给您"
    return f"您好，{what}已经完成交付，{files}。请查收；确认没问题请回复「确认收货」，需要修改请直接告诉我们要改哪里。"


def package_relative(order_id: str, spell: int) -> str:
    return f"{DELIVERY_EXPORTS}/{order_id}/wecom-{max(1, int(spell or 1))}"


def package_words(order_id: str, spell: int) -> str:
    """Where the package is, for the owner: the project's 资料 area, 导出, this order."""
    return f"项目资料区的「导出」里，{package_relative(order_id, spell)}/files/"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _plain(root: Path, path: Path) -> bool:
    """No component from ``root`` down to ``path`` is a link of any kind (a package is never read or written through one)."""
    from ampuly.plugins.office_ui.artifacts_service import redirected

    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    current = root
    if redirected(current):
        return False
    for part in parts:
        current = current / part
        if redirected(current):
            return False
    return True


def load_package(root: Path, order_id: str, spell: int) -> dict[str, Any] | None:
    """The package's manifest, or None when there is none (or it is not one this module wrote)."""
    if not _SAFE_ORDER.fullmatch(str(order_id or "")):
        return None
    target = root.joinpath(*package_relative(order_id, spell).split("/"))
    path = target / MANIFEST
    try:
        if not _plain(root, target) or not path.is_file() or path.is_symlink():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != PACKAGE_SCHEMA or data.get("order_id") != order_id:
        return None
    return data


def build_package(root: Path, order_id: str, spell: int, candidates: list[Candidate], *, title: str) -> dict[str, Any]:
    """Copy the files into the order's package once and write its manifest; an existing package is returned as it is.

    The copy is written beside the target and moved in whole (``replace_with_retry``), so a package is either complete
    or absent. A file that cannot be read now is left out and named in ``skipped``.
    """
    from ampuly.core.config import replace_with_retry
    from ampuly.plugins.office_ui.artifacts_service import multiply_linked

    if not _SAFE_ORDER.fullmatch(str(order_id or "")):
        raise PackageProblem("工单编号不能用作文件夹名")
    existing = load_package(root, order_id, spell)
    if existing is not None:
        return existing
    target = root.joinpath(*package_relative(order_id, spell).split("/"))
    if not _plain(root, target.parent):
        raise PackageProblem("交付包的位置经过了一个链接，系统不往那里写，请在资料区里核对")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _plain(root, target.parent) or target.exists():
        raise PackageProblem("交付包的位置被占用了（也许是上次没做完留下的），请在资料区里核对")
    sendable: list[tuple[Candidate, tuple[str, str]]] = []
    too_large: list[dict[str, Any]] = []
    for item in candidates:
        kind = media_kind(item.shown, item.size)
        if kind is None:
            if item.size > MEDIA_MIN_BYTES_EXCLUSIVE and len(too_large) < MAX_TOO_LARGE_LISTED:
                too_large.append({"name": PurePosixPath(item.shown).name, "size": item.size})
            continue
        sendable.append((item, kind))
    listed = sendable[:MAX_LISTED]
    names = _display_names([item for item, _kind in listed])
    temporary = target.parent / f".{target.name}-{uuid.uuid4().hex}.tmp"
    (temporary / "files").mkdir(parents=True)
    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        for index, ((item, (media_type, content_type)), name) in enumerate(zip(listed, names), 1):
            source = root.joinpath(*item.source.split("/"))
            stored = f"files/{index:02d}-{name}"
            destination = temporary.joinpath(*stored.split("/"))
            try:
                # Never through a link, never a file with a second name elsewhere (a hard link can bring one in).
                if not _plain(root, source) or not source.is_file() or multiply_linked(source):
                    raise OSError("not a plain file")
                shutil.copyfile(source, destination)
                digest = _digest(destination)
            except OSError:
                skipped.append(name)
                continue
            if item.sha256 and digest != item.sha256:
                destination.unlink(missing_ok=True)
                skipped.append(name)
                continue
            files.append({"id": str(index), "name": name, "stored": stored, "shown": item.shown, "source": item.source,
                          "size": int(destination.stat().st_size), "sha256": digest,
                          "media_type": media_type, "content_type": content_type})
        manifest = {
            "schema": PACKAGE_SCHEMA, "order_id": order_id, "spell": int(spell), "title": str(title or ""),
            "created_at": datetime.now().isoformat(timespec="seconds"), "files": files,
            "default": [item["id"] for item in files[:WECOM_DELIVERY_MAX_FILES]], "too_large": too_large,
            "more_than_listed": max(0, len(sendable) - len(listed)), "skipped": skipped,
        }
        (temporary / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        replace_with_retry(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def read_files(root: Path, manifest: Mapping[str, Any], ids: list[str]) -> list[Any]:
    """The chosen files of a package as ``wecom_channel.DeliveryFile``, each checked against its recorded digest."""
    from ampuly.plugins.wecom_service.wecom_channel import DeliveryFile

    order_id = str(manifest.get("order_id", "") or "")
    target = root.joinpath(*package_relative(order_id, int(manifest.get("spell", 1) or 1)).split("/"))
    by_id = {str(item.get("id", "")): dict(item) for item in list(manifest.get("files", []) or []) if isinstance(item, dict)}
    out: list[Any] = []
    for file_id in ids:
        item = by_id.get(str(file_id))
        if item is None:
            raise PackageProblem("选中的文件不在交付包里")
        stored = str(item.get("stored", "") or "")
        if not stored.startswith("files/") or ".." in stored.split("/"):
            raise PackageProblem(f"交付包里的文件「{item.get('name', '')}」位置不对")
        path = target.joinpath(*stored.split("/"))
        try:
            if not _plain(root, path) or not path.is_file():
                raise OSError("missing")
            content = path.read_bytes()
        except OSError:
            raise PackageProblem(f"交付包里的文件「{item.get('name', '')}」找不到了") from None
        if hashlib.sha256(content).hexdigest() != str(item.get("sha256", "") or ""):
            raise PackageProblem(f"交付包里的文件「{item.get('name', '')}」在备好之后被改过，已经不是当时那一份")
        out.append(DeliveryFile(name=str(item["name"]), content=content, media_type=str(item.get("media_type", "file")),
                                content_type=str(item.get("content_type", "") or "application/octet-stream"),
                                sha256=str(item["sha256"])))
    return out


def chosen_ids(manifest: Mapping[str, Any], form: Mapping[str, Any]) -> list[str]:
    """The files the owner chose on the card (the chooser's value), else the drafted default; in the package's order."""
    raw = dict(form or {}).get("files")
    if raw is None:
        return [str(item) for item in list(manifest.get("default", []) or [])]
    picked = {str(item) for item in (raw if isinstance(raw, (list, tuple)) else [raw]) if str(item).strip()}
    return [str(item.get("id", "")) for item in list(manifest.get("files", []) or []) if str(item.get("id", "")) in picked]


# ── the card ────────────────────────────────────────────────────────────────


def delivery_spec(order: Any, manifest: Mapping[str, Any], *, spell: int, root: Path, customer: str = "",
                  window: str = "") -> GateSpec:
    """The delivery card of ``order`` for its ``spell``-th arrival in 客户验收中, from its package."""
    order_id = str(getattr(order, "order_id", "") or "")
    files = [dict(item) for item in list(manifest.get("files", []) or []) if isinstance(item, dict)]
    default = [str(item) for item in list(manifest.get("default", []) or [])]
    package = root.joinpath(*package_relative(order_id, spell).split("/"))
    return GateSpec(
        checkpoint_id=wecom_delivery_key(order_id, spell),
        checkpoint_type=WECOM_DELIVERY_KIND,
        task_id=str(getattr(order, "launched_task_id", "") or ""),
        session_id="",
        waiting_task_id="",
        order_id=order_id,
        order_title=str(getattr(order, "customer_name", "") or getattr(order, "brand_name", "") or ""),
        node_id="",
        node_name=WECOM_DELIVERY_LABEL,
        node_type=WECOM_DELIVERY_KIND,
        assignee="",
        version=max(1, int(spell or 1)),
        standard=[],
        deliverables=[str(item.get("name", "")) for item in files],
        revision_path="",
        workspace_root=str(package),
        prompt=delivery_text(str(manifest.get("title", "") or ""), len(default)),
        # The default's pictures go on the card itself (feishu-in-place-review): the owner sees what the customer gets.
        present=[str(item.get("stored", "")) for item in files if str(item.get("id", "")) in default and item.get("media_type") == "image"],
        project_id=str(getattr(order, "project_id", "") or "default"),
        order_inputs=order_inputs_from_fields(dict(getattr(order, "fields", None) or {})),
        card_kind=WECOM_DELIVERY_KIND,
        delivery={
            "customer": customer, "window": window, "files": files, "default": default,
            "too_large": list(manifest.get("too_large", []) or []),
            "more_than_listed": int(manifest.get("more_than_listed", 0) or 0),
            "package": package_words(order_id, spell),
        },
    )


def _window_words(conv: Mapping[str, Any] | None, now: float) -> str:
    from ampuly.plugins.wecom_service.wecom_channel import _window

    if not conv:
        return "找不到这位客户在企业微信里的对话"
    decision = _window(conv).can_send(now)
    if decision.ok:
        left = decision.window_closes_at - now if decision.window_closes_at else 0
        hours = max(0, int(left // 3600))
        return f"能发（离 48 小时的期限还有约 {hours} 小时，还能发 {decision.remaining} 条）"
    return "现在发不了：" + decision.message


def _customer_of(conv: Mapping[str, Any] | None, order: Any) -> str:
    from ampuly.plugins.wecom_service.wecom_channel import customer_words

    if conv:
        return customer_words(conv)
    name = str(getattr(order, "customer_name", "") or "").strip()
    return f"「{name}」" if name else "一位企业微信客户"


def _title(console: Any, order: Any) -> str:
    from ampuly.plugins.office_ui.master_order_draft import find_package
    from ampuly.plugins.wecom_service.wecom_channel import order_title

    fields = dict(getattr(order, "fields", None) or {})
    try:
        package = find_package(list(as_console_host(console).packages()), str(getattr(order, "workflow_id", "") or ""))
    except Exception:  # noqa: BLE001 - no flow: the order's own names
        package = None
    if package is not None:
        return order_title(package, fields)
    return str(getattr(order, "brand_name", "") or getattr(order, "customer_name", "") or "")


async def _root_and_steps(console: Any, order: Any) -> tuple[Path, list[dict[str, Any]], Any]:
    from ampuly.plugins.office_ui.acceptance_round import order_steps
    from ampuly.plugins.office_ui.artifacts_service import workspace_root_for_project

    host = as_console_host(console)
    project_id = str(getattr(order, "project_id", "") or "default")
    engine, _pid = await host.request_engine(project_id)
    root = workspace_root_for_project(engine, project_id)
    steps, _version = await asyncio.to_thread(order_steps, host.console_api(), engine, project_id, order)
    return root, list(steps or []), engine


async def _frozen_files(engine: Any, project_id: str, record: Any) -> list[dict[str, Any]]:
    """A passed round's verified frozen copies, when the round was passed with its files frozen; [] otherwise."""
    from ampuly.plugins.office_ui import artifact_versions

    snapshot = dict(getattr(record, "artifact_snapshot", None) or {}) if record is not None else {}
    if not snapshot or str(getattr(record, "status", "") or "") not in {"accepted", "accepted_with_exceptions"}:
        return []
    try:
        verified = await asyncio.to_thread(artifact_versions.verify_delivery_snapshot, engine, project_id=project_id,
                                           snapshot=snapshot)
    except Exception:  # noqa: BLE001 - not verifiable: the order's files are read instead
        logger.opt(exception=True).warning("wecom delivery: a passed round's frozen files could not be verified")
        return []
    return list(verified.get("files", []) or [])


def _now(console: Any) -> float:
    """The channel's clock when the channel runs (the one its window rules use), else the machine's."""
    import time

    from ampuly.plugins.wecom_service.wecom_channel import runtime_for

    clock = getattr(getattr(runtime_for(console), "channel", None), "_clock", None)
    try:
        return float(clock()) if callable(clock) else time.time()
    except Exception:  # noqa: BLE001 - a clock that fails is no clock
        return time.time()


async def draft(console: Any, order: Any, *, spell: int) -> GateSpec | None:
    """The package and the card of one order's delivery (read, copy, no send). None when there is nothing to deliver to:
    no conversation with this order's customer."""
    from ampuly.plugins.wecom_service.wecom_kf_store import WecomKfStore

    business = as_console_host(console).business
    order_id = str(getattr(order, "order_id", "") or "")
    project_id = str(getattr(order, "project_id", "") or "default")
    conv = WecomKfStore(business).conversation_for_order(order_id)
    if conv is None:
        return None
    root, steps, engine = await _root_and_steps(console, order)
    manifest = await asyncio.to_thread(load_package, root, order_id, spell)
    if manifest is None:
        record = business.acceptance(order_id)
        frozen = await _frozen_files(engine, project_id, record)
        evidence = list(getattr(record, "evidence", None) or []) if record is not None and str(
            getattr(record, "status", "") or "") in {"criteria_defined", "evidence_collecting", "submitted"} else []
        candidates = await asyncio.to_thread(collect_candidates, root, order_id, steps, frozen, evidence)
        manifest = await asyncio.to_thread(build_package, root, order_id, spell, candidates, title=_title(console, order))
    return delivery_spec(order, manifest, spell=spell, root=root, customer=_customer_of(conv, order),
                         window=_window_words(conv, _now(console)))


async def announce_delivery_cards(console: Any) -> int:
    """Every 企业微信 order in 客户验收中 gets its delivery drafted and one card in the Feishu group; returns how many
    this round sent. Oldest first, at most ``CARDS_PER_ROUND`` a round; a card sent, answered or retired for an arrival is
    never sent again. Nothing with the channel switched off, Feishu not set up, outbound muted, the database below
    version 4, or for a project its owner archived."""
    from ampuly.plugins.office_ui.business_store import ORDER_LIST_HARD_CAP
    from ampuly.plugins.wecom_service.wecom_channel import ORDER_SOURCE, switch_on
    from ampuly.plugins.wecom_service.wecom_kf_store import WecomKfStore

    host = as_console_host(console)
    if not host.gate_configured():
        return 0
    service = host.gate_service()
    if service.client is None or not service.outbound_open():
        return 0
    business = service.business
    if not switch_on(business) or not WecomKfStore(business).ready():
        return 0
    orders = [order for order in list(await asyncio.to_thread(business.list_orders, status="delivering", limit=ORDER_LIST_HARD_CAP) or [])
              if str(getattr(order, "source", "") or "") == ORDER_SOURCE and str(getattr(order, "order_id", "") or "").strip()]
    if not orders:
        return 0
    frozen_projects: set[str] = set()
    check = host.frozen_project_check()
    if callable(check):
        try:
            frozen_projects = set(await asyncio.to_thread(check, sorted({host.normalize_project_id(getattr(o, "project_id", None)) for o in orders})))
        except Exception:  # noqa: BLE001 - statuses unreadable: nothing sent this round (as the close cards)
            logger.opt(exception=True).warning("wecom delivery: project statuses unreadable; no delivery card this round")
            return 0
    sent = 0
    for order in reversed(orders):
        if sent >= CARDS_PER_ROUND:
            break
        order_id = str(order.order_id)
        if callable(check) and host.normalize_project_id(getattr(order, "project_id", None)) in frozen_projects:
            continue
        try:
            record = business.acceptance(order_id)
            if record is not None and str(getattr(record, "status", "") or "") == "rejected":
                continue  # sent back: its next round comes first
            spell = delivery_spell(business.events(order_id))
            if business.gate_sent_or_decided(wecom_delivery_key(order_id, spell)):
                continue
            spec = await draft(host, order, spell=spell)
            if spec is None:
                continue
            if sent == 0:
                host.ensure_feishu_listener()
            await service.announce(spec)
        except Exception:  # noqa: BLE001 - one order's card waits for the next round
            logger.opt(exception=True).warning("wecom delivery: the delivery card of order {} was not sent; tried again next round", order_id)
            continue
        sent += 1
        logger.info("wecom delivery: delivery card sent for order {}", order_id)
    return sent


# ── the owner's answer ──────────────────────────────────────────────────────


def _sending_unsettled(events: list[dict[str, Any]]) -> bool:
    sending = sum(1 for event in events if event.get("kind") == SENDING_EVENT)
    settled = sum(1 for event in events if event.get("kind") == SETTLED_EVENT)
    return sending > settled


def _stamp(actor: str) -> str:
    return f"{actor or '老板'} · {datetime.now().strftime('%H:%M')}"


class GateHooks:
    """What the Feishu gate service asks of the channel for a delivery card (``GateService(card_hooks=[...])``, built
    by the card kind's ``hooks``, ``delivery_card.CARD_KIND``)."""

    # The card kind these hooks answer for (``GateService`` keys its hooks by it).
    card_kind = WECOM_DELIVERY_KIND

    def __init__(self, console: Any) -> None:
        self._console = as_console_host(console)

    @property
    def business(self) -> Any:
        return self._console.business

    async def load_spec(self, checkpoint_id: str) -> GateSpec | None:
        """The card rebuilt from its order and its package (after a restart); None when either is gone."""
        from ampuly.plugins.wecom_service.wecom_kf_store import WecomKfStore

        target = wecom_delivery_target(checkpoint_id)
        if target is None:
            return None
        order_id, spell = target
        order = self.business.get_order(order_id)
        if order is None:
            return None
        root, _steps, _engine = await _root_and_steps(self._console, order)
        manifest = await asyncio.to_thread(load_package, root, order_id, spell)
        if manifest is None:
            return None
        conv = WecomKfStore(self.business).conversation_for_order(order_id)
        return delivery_spec(order, manifest, spell=spell, root=root, customer=_customer_of(conv, order),
                             window=_window_words(conv, _now(self._console)))

    async def answer(self, spec: GateSpec, kind: str, person: Mapping[str, Any], form: Mapping[str, Any]) -> dict[str, Any]:
        """放行发给客户 / 先不发, pressed by the owner (the gate service checked who). ``final`` False gives the card back."""
        actor = str(person.get("name") or person.get("login") or "老板")
        target = wecom_delivery_target(spec.checkpoint_id)
        if target is None:
            return {"final": False, "say": "这张卡认不出是哪张工单的交付，这次什么都没有发。"}
        order_id, spell = target
        where = package_words(order_id, spell)
        if kind == WECOM_HOLD:
            self.business.record_gate_event(spec.checkpoint_id, "wecom_delivery_held", actor=actor, note={"package": where})
            return {"final": True, "outcome": "held",
                    "status_line": f"先不发 · {_stamp(actor)} · 交付包留在{where}，需要时在企业微信里手动转给客户"}
        if kind != WECOM_RELEASE:
            return {"final": False, "say": "不认识这个按钮，这次什么都没有发。"}
        return await self._release(spec, actor, order_id, spell, where, form)

    async def _release(self, spec: GateSpec, actor: str, order_id: str, spell: int, where: str,
                       form: Mapping[str, Any]) -> dict[str, Any]:
        from ampuly.plugins.office_ui.notifier import channels_muted
        from ampuly.plugins.wecom_service.wecom_channel import runtime_for, switch_on

        business = self.business
        key = spec.checkpoint_id
        if _sending_unsettled(business.gate_events(key)):
            # An earlier press stopped between its first send and its record (the console stopped): never again.
            business.record_gate_event(key, SETTLED_EVENT, actor=actor, note={"outcome": "unknown", "cause": "earlier_press_cut_off"})
            return {"final": True, "outcome": "unknown", "status_line": (
                f"结果未知 · {_stamp(actor)} · 上一次放行发到一半时控制台停了，不知道客户收到了哪些，系统不会再发；"
                f"请到企业微信里核对，缺的从{where}手动转")}
        order = business.get_order(order_id)
        if order is None:
            return {"final": False, "say": "找不到这张工单了，这次什么都没有发。"}
        root, steps, engine = await _root_and_steps(self._console, order)
        manifest = await asyncio.to_thread(load_package, root, order_id, spell)
        if manifest is None:
            return {"final": False, "say": f"交付包找不到了（应在{where}），这次什么都没有发。"}
        ids = chosen_ids(manifest, form)
        if not ids and list(manifest.get("files", []) or []) and dict(form or {}).get("files") is not None:
            # The chooser was sent back empty: most likely not "the message only" -- the owner says that by hand.
            return {"final": False, "say": (
                "这次一个文件都没勾，什么都没有发；勾上要发的文件再点「放行发给客户」。"
                f"只想发一句说明的话，请在企业微信里自己发，交付包在{where}。")}
        if len(ids) > WECOM_DELIVERY_MAX_FILES:
            return {"final": False, "say": (
                f"一次最多发 {WECOM_DELIVERY_MAX_FILES} 个文件（企业微信在客户发消息后最多让发 5 条：1 条说明加 4 个文件），"
                f"这次勾了 {len(ids)} 个，什么都没有发；少勾几个再点「放行发给客户」，其余的可以在企业微信里手动转。")}
        try:
            files = await asyncio.to_thread(read_files, root, manifest, ids)
        except PackageProblem as exc:
            return {"final": False, "say": f"{str(exc).rstrip('。')}，这次什么都没有发。交付包在{where}。"}
        channel = getattr(runtime_for(self._console), "channel", None)
        if channel is not None and (not switch_on(business) or channels_muted()):
            # Switched off or muted since the card was drafted: the link may still run until the next duty round,
            # but nothing goes out while the owner has the channel closed.
            return {"final": False, "reason": "channel_off", "say": (
                f"企业微信通道现在关着（或外发渠道已静音），这次什么都没有发。打开后可以再点一次「放行发给客户」，"
                f"或在企业微信里手动转：交付包在{where}。")}
        if channel is None:
            return {"final": False, "reason": "channel_off", "say": (
                f"企业微信通道现在没有开着，或还没连上中转站，这次什么都没有发。可以等连上后再点一次「放行发给客户」，"
                f"或在企业微信里手动转：交付包在{where}。")}
        text = delivery_text(str(manifest.get("title", "") or ""), len(files))
        names = [item.name for item in files]
        business.record_gate_event(key, SENDING_EVENT, actor=actor, note={"files": names})
        try:
            result = await channel.send_delivery(order_id=order_id, text=text, files=files)
        except BaseException as exc:
            # The send stopped on an error of its own: whatever went is recorded on each message; the card says unknown.
            business.record_gate_event(key, SETTLED_EVENT, actor=actor, note={"outcome": "unknown", "cause": type(exc).__name__})
            if not isinstance(exc, Exception):
                raise
            logger.opt(exception=exc).warning("wecom delivery: sending the delivery of order {} stopped on an error", order_id)
            return {"final": True, "outcome": "unknown", "status_line": (
                f"结果未知 · {_stamp(actor)} · 发送中途出错，不知道客户收到了哪些，系统不会再发；请到企业微信里核对，缺的从{where}手动转")}
        business.record_gate_event(key, SETTLED_EVENT, actor=actor, note={
            "outcome": result.outcome, "reason": result.reason, "sent": result.sent, "unknown": result.unknown,
            "unsent": result.unsent})
        if not result.reached:
            say = f"{str(result.words).rstrip('。')}。交付包已经备好，在{where}，可以在企业微信里手动转给客户"
            if result.reason in ("window_closed", "quota_used", "not_connected"):
                say += "；等客户再发一句（或连上中转站）后，也可以再点一次「放行发给客户」"
            return {"final": False, "reason": result.reason, "say": say + "。"}
        # The round's evidence is what reached the customer (or may have): after a partial or unknown send, a file that
        # never went is not bound -- a later 确认 would otherwise freeze a file the customer never got (review of W3b).
        reached = set(result.sent) | ({result.unknown} if result.unknown else set())
        submitted = await self._submit_round(order, engine, [item for item in files if item.name in reached],
                                             actor=actor, root=root, manifest=manifest)
        return {"final": True, "outcome": result.outcome, "status_line": self._status_line(result, actor, where),
                **({"say": submitted} if submitted else {})}

    @staticmethod
    def _status_line(result: Any, actor: str, where: str) -> str:
        files = [name for name in result.sent if name != "说明文字"]
        if result.outcome == "sent":
            what = f"1 条说明加 {len(files)} 个文件" if files else "1 条说明"
            return f"已发给客户 · {_stamp(actor)} · {what}；客户回复后 AI 客服会接着处理，确认收货还是由你结案"
        sent = "、".join(result.sent) if result.sent else "什么也没有"
        unsent = "、".join(result.unsent)
        if result.outcome == "unknown":
            return (f"结果未知 · {_stamp(actor)} · 已发出：{sent}；「{result.unknown}」发出去后没有回音，系统不会再发"
                    + (f"；没发的：{unsent}" if unsent else "") + f"。请到企业微信里核对，缺的从{where}手动转")
        return (f"只发出了一部分 · {_stamp(actor)} · 已发出：{sent}；没发出的：{unsent}（{result.words}）。"
                f"请在企业微信里手动补发，交付包在{where}")

    @staticmethod
    async def _bind_sent(engine: Any, project_id: str, source: str, sha256: str) -> dict[str, Any] | None:
        """The order's own file a sent package copy came from, bound by content -- only while its bytes are still the
        ones sent. That is the file the owner's 确认收货并结案 names, and freezing the round copies it under its own
        (short) path, not the package's; None when it is gone, changed, or a frozen round's copy already."""
        from ampuly.plugins.office_ui import artifact_versions

        clean = str(source or "").strip().replace("\\", "/")
        if not clean or clean.startswith(f"{DELIVERY_HISTORY}/") or clean.startswith(f"{DELIVERY_EXPORTS}/"):
            return None
        try:
            bound = await asyncio.to_thread(artifact_versions.bind_artifact_evidence, engine, project_id, clean)
        except (artifact_versions.SnapshotError, OSError, ValueError):
            return None
        return bound if str(bound.get("artifact_hash", "") or "") == str(sha256 or "") else None

    async def _submit_round(self, order: Any, engine: Any, files: list[Any], *, actor: str, root: Path,
                            manifest: Mapping[str, Any]) -> str:
        """After a delivery reached the customer: the round's missing evidence is the delivered files (bound by content)
        and the round is submitted, so the customer's 确认 can be its decision. Best effort: "" when done or nothing to
        do, else a line for the card's thread."""
        from ampuly.plugins.office_ui import acceptance_round, artifact_versions
        from ampuly.plugins.office_ui.business_store import OrderTransitionError

        business = self.business
        order_id = str(order.order_id)
        project_id = str(getattr(order, "project_id", "") or "default")
        who = f"{actor}（飞书）"
        try:
            record = business.acceptance(order_id, project_id=project_id)
            if record is None:
                steps, version = await asyncio.to_thread(acceptance_round.order_steps, self._console.console_api(), engine,
                                                         project_id, order)
                try:
                    business.define_acceptance(order_id, [acceptance_round.generated_criterion(steps)],
                                               actor=acceptance_round.GENERATED_ACTOR, workflow_version=version,
                                               project_id=project_id)
                except OrderTransitionError:
                    pass
                record = business.acceptance(order_id, project_id=project_id)
            if record is None or record.status not in {"criteria_defined", "evidence_collecting"}:
                return ""
            run_id = str(getattr(order, "launched_task_id", "") or "").strip() or f"order:{order_id}"
            package = package_relative(order_id, int(manifest.get("spell", 1) or 1))
            by_name = {str(item.get("name", "")): dict(item) for item in list(manifest.get("files", []) or [])
                       if isinstance(item, dict)}
            evidence: list[dict[str, Any]] = []
            for item in files:
                entry = by_name.get(item.name)
                stored = str((entry or {}).get("stored", "") or "")
                if not stored:
                    continue
                bound = await self._bind_sent(engine, project_id, str((entry or {}).get("source", "") or ""), item.sha256)
                if bound is None:
                    bound = await asyncio.to_thread(artifact_versions.bind_artifact_evidence, engine, project_id, f"{package}/{stored}")
                evidence.append({"run_id": run_id, "note": EVIDENCE_NOTE, **bound})
            if not evidence:
                evidence = [{"run_id": run_id, "event_id": f"order:{order_id}:wecom-delivery", "note": EVIDENCE_NOTE + "（只有说明，没有文件）"}]
            covered = {str(item.get("criterion", "") or "") for item in record.evidence}
            for criterion in [item for item in record.criteria if item not in covered]:
                for entry in evidence:
                    business.add_acceptance_evidence(order_id, criterion=criterion, evidence=entry, actor=who, project_id=project_id)
            business.submit_acceptance(order_id, actor=who, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 - the delivery went; the round is the owner's to finish
            logger.opt(exception=True).warning("wecom delivery: the round of order {} was not submitted after its delivery", order_id)
            words = acceptance_round.plain_reason(exc, order_id) if isinstance(exc, OrderTransitionError) else "系统出错了"
            return f"交付已经发出，但这一轮验收没能提交（{words}）：客户回复确认时系统记不下，请在业务工单的验收里处理。"
        return ""


# ── reads for the console ───────────────────────────────────────────────────


_SPEAKERS = {3: "客户", 5: "人工客服"}


def conversation_view(business: Any, order_id: str, *, limit: int = 200) -> dict[str, Any] | None:
    """The 企业微信 conversation of an order, read only, for the order record: who said what and when, what we sent and
    how it went. No customer id, no account id, no message id. None when the order has no conversation."""
    from ampuly.plugins.wecom_service.wecom_kf_store import OUT_FAILED, OUT_PENDING, OUT_REFUSED, OUT_SENT, OUT_UNKNOWN, STAGE_WORDS, WecomKfStore

    store = WecomKfStore(business)
    conv = store.conversation_for_order(order_id)
    if conv is None:
        return None
    outcome_words = {OUT_SENT: "已送达企业微信", OUT_FAILED: "企业微信没让发", OUT_REFUSED: "没有发出",
                     OUT_UNKNOWN: "结果未知", OUT_PENDING: "结果未知"}
    lines: list[dict[str, Any]] = []
    for row in store.messages(conv["conversation_id"], limit=limit):
        if row["direction"] == "in":
            if row["origin"] == 4 or row["msgtype"] == "event":
                speaker, text = "系统", row["text"] or "系统事件"
            else:
                speaker = _SPEAKERS.get(int(row["origin"] or 0), "客户")
                text = row["text"] or ("[图片、语音或文件，系统看不到内容]" if row["msgtype"] else "[读不了的消息]")
            at = datetime.fromtimestamp(row["send_time"]).isoformat(timespec="seconds") if row["send_time"] else row["recorded_at"]
            lines.append({"at": at, "from": speaker, "text": text, "outcome": ""})
        else:
            lines.append({"at": row["recorded_at"], "from": "我们", "text": row["text"],
                          "outcome": outcome_words.get(row["outcome"], ""), "kind": row["event_type"]})
    return {"stage": conv["stage"], "stage_word": STAGE_WORDS.get(conv["stage"], conv["stage"]),
            "handoff_reason": conv["handoff_reason"], "messages": lines}
