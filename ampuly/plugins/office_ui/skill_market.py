"""在线技能市场（外部库）: SkillHub's official public API, read locally, downloaded into a trial area.

Owner and WorkBuddy decided not to run a mirror server for now: this console calls
SkillHub itself. SkillHub already mirrors ClawHub, is reachable from inside China without a VPN and
needs no key, so it is the one enabled source; a ClawHub adapter can be added behind the same shape.

What this module promises, and why each line is here:

* **Reads only, from one origin.** Every request goes to ``https://api.skillhub.cn``; the one
  redirect it follows is the download's, and only to SkillHub's own object storage host. Nothing is
  sent but the query the owner typed.
* **No upstream program runs.** ``skillhub install`` and ``npx clawhub`` download *and execute*;
  this downloads a zip over HTTP and never extracts it onto a path the engine loads skills from.
* **What lands is the download package, and how it differs from SkillHub's own list is on the record.**
  SkillHub serves a per-file sha256 list and a zip for each version, filled by separate syncs, and the
  two drift apart (: about half of the most-starred free skills -- a regenerated
  ``_meta.json``, a ``SKILL.md`` changed in one sync only, files one side names and the other lacks).
  Refusing on that blocked the skills themselves, so the owner decided (以下载包为准，差异标出来)
  that the zip is authoritative: every file in it is filed and scanned, each difference from the list
  (content differs, listed but missing, in the zip but unlisted, another version) is recorded on the
  trial and shown, and admitting such a trial needs the owner to acknowledge those very differences
  (their fingerprint, ``listing_mismatch_key``). The zip itself is still held to everything else: fetched
  only from SkillHub's storage over https on its own port, SKILL.md at its root, paths confined, names
  with hidden characters, links, duplicates, case twins, folder clashes and oversized archives refused.
* **Paid skills are not fetched.** The ``pay-skill`` category is per-use paid; there is no budget or
  payment channel for it, so its files are never downloaded (policy).
* **A trial is not an admission.** Files go to ``<ampuly_home>/component_trials/`` -- runtime state,
  never committed or shipped -- with the static scan the catalog already applies. Passing a test and
  entering the internal library are separate, later, owner steps.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, unquote, urlencode, urlsplit

from ampuly.core.config import replace_with_retry

API = "https://api.skillhub.cn"
# The download answers with a redirect to SkillHub's own COS bucket (skillhub-<id>.cos.accelerate.myqcloud.com,
# Regression case); that and nothing else is followed. The zip found there is what lands (policy).
DOWNLOAD_HOST_RE = re.compile(r"^skillhub-[0-9]+\.cos\.accelerate\.myqcloud\.com$")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SOURCE = "skillhub"
# Where a trial can come from: the SkillHub market, or the local resource library the component catalog lists.
TRIAL_SOURCES = ("skillhub", "catalog")
PAID_CATEGORY = "pay-skill"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
# A version names a folder: letters and digits at both ends (Windows drops a trailing dot), no "..".
VERSION_RE = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]{0,62}[0-9A-Za-z])?$")
# Names Windows maps to devices whatever the extension (``nul.txt`` is still NUL).
WINDOWS_DEVICE_RE = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9]|conin\$|conout\$)(\..*)?$", re.IGNORECASE)
UNSAFE_NAME_CHARS = set('<>:"|?*') | {chr(code) for code in range(32)}
# Characters that make a name read as another: a bidi override shows "evil<U+202E>gnp.sh" as "evilhs.png", a
# zero-width one hides inside a name that looks like a second. Named here so the rule does not hang on one Python's
# Unicode tables; every other format character (category Cf) is refused too, with the C0/C1 controls and the line and
# paragraph separators. Letters of any script (CJK included) stay allowed.
HIDDEN_NAME_CHARS = (
    {"\u061c", "\u200e", "\u200f", "\u2060", "\ufeff"}
    | {chr(code) for code in range(0x202A, 0x202F)}
    | {chr(code) for code in range(0x2066, 0x206A)}
    | {chr(code) for code in range(0x200B, 0x200E)}
)
HIDDEN_NAME_CATEGORIES = {"Cf", "Cc", "Zl", "Zp"}
MAX_FILES = 500
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
SUMMARY_LIMIT = 80
TIMEOUT = 15.0
TRIALS_DIR = "component_trials"
TRIAL_FILE = "TRIAL.json"
# How many names of each kind of difference between SkillHub's file list and its zip are kept (the counts are whole).
MISMATCH_LIST_LIMIT = 50
# SkillHub's own small record inside a package: {"ownerId", "publishedAt", "slug", "version"} (Regression case).
META_FILE = "_meta.json"
MAX_META_BYTES = 64 * 1024


class MarketError(RuntimeError):
    """A refusal or failure the page can show as it is. ``reason`` is stable."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# (url, max_bytes, follow_redirect) -> (status, headers, body). Injected so tests never touch the network.
Transport = Callable[[str, int], tuple[int, dict[str, str], bytes]]


def http_transport(url: str, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    """The real transport: one GET, no redirects followed, the body read up to ``max_bytes``."""
    import httpx

    headers = {"User-Agent": "SuperOnes-component-library/1", "Accept": "application/json, application/zip"}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers=headers) as client:
        with client.stream("GET", url) as response:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise MarketError("the response is larger than allowed", reason="too_large")
                chunks.append(chunk)
            return response.status_code, {k.lower(): v for k, v in response.headers.items()}, b"".join(chunks)


def _api_url(path: str, params: dict[str, Any] | None = None) -> str:
    query = urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    return f"{API}{path}" + (f"?{query}" if query else "")


def _get_json(transport: Transport, url: str) -> Any:
    status, _headers, body = _fetch(transport, url, MAX_JSON_BYTES)
    if status != 200:
        raise MarketError(f"SkillHub answered {status}", reason="bad_status" if status != 404 else "not_found")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MarketError("SkillHub answered something that is not JSON", reason="bad_response") from exc


def _summary(value: Any) -> str:
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= SUMMARY_LIMIT else flat[: SUMMARY_LIMIT - 1].rstrip() + "…"


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def is_paid(category: str, labels: Any, pricing: Any = None) -> bool:
    """Whether SkillHub charges for a skill.

    SkillHub files a paid skill under its subject (data-analysis, design-media...) and marks the price in
    ``labels.pricing_type``; the pay-skill category is only a listing, and the detail adds a ``pricing`` block.
    Regression case. Anything not plainly free is treated as paid: a free skill wrongly held back costs a
    click, a paid one let through spends money nobody approved.
    """
    if str(category or "") == PAID_CATEGORY:
        return True
    pricing_type = str((labels or {}).get("pricing_type") or "").strip().lower() if isinstance(labels, dict) else ""
    if pricing_type and pricing_type != "free":
        return True
    # A free skill's detail carries no pricing block at all (Regression case); any block is read as a price.
    if pricing in (None, "", {}, []):
        return False
    if not isinstance(pricing, dict):
        return True
    prices = [value for key, value in pricing.items() if any(word in str(key).lower() for word in ("price", "amount", "fee", "cost"))]
    if any(_nonzero(value) for value in prices):
        return True
    mode = str(pricing.get("pricingMode") or pricing.get("billingType") or "").strip().lower()
    if mode:
        return mode != "free"
    # Neither a mode nor a price that says zero: nothing here says it is free.
    return not prices


def _nonzero(value: Any) -> bool:
    if value in (None, "", False):
        return False
    try:
        return float(str(value).strip()) != 0.0
    except ValueError:
        return True


def normalize_listing(item: Any) -> dict[str, Any] | None:
    """One SkillHub list entry as the 组件库 shows it. Fields SkillHub leaves out are empty, not guessed."""
    if not isinstance(item, dict):
        return None
    slug = str(item.get("slug") or "").strip()
    if not SLUG_RE.match(slug):
        return None
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    category = str(item.get("category") or "")
    return {
        "source": SOURCE,
        "slug": slug,
        "name": str(item.get("name") or item.get("displayName") or slug),
        "summary": _summary(item.get("description_zh") or item.get("description") or item.get("summary_zh") or item.get("summary")),
        "category": category,
        "version": str(item.get("version") or ""),
        "stars": _int(item.get("stars")),
        "downloads": _int(item.get("downloads")),
        "installs": _int(item.get("installs")),
        "verified": bool(item.get("verified")),
        "requires_api_key": str(labels.get("requires_api_key", "")).lower() == "true",
        "paid": is_paid(category, labels),
        "upstream": str(item.get("source") or ""),
        "updated_at": _int(item.get("updated_at") or item.get("updatedAt")),
    }


def categories(transport: Transport = http_transport) -> list[dict[str, str]]:
    body = _get_json(transport, _api_url("/api/v1/categories"))
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise MarketError("SkillHub's category list has an unexpected shape", reason="bad_response")
    rows = [
        {"key": str(item.get("key") or ""), "name": str(item.get("name") or ""), "name_en": str(item.get("nameEn") or "")}
        for item in items
        if isinstance(item, dict) and item.get("active", True) and item.get("key")
    ]
    return sorted(rows, key=lambda row: next((i.get("sortOrder", 0) for i in items if isinstance(i, dict) and i.get("key") == row["key"]), 0))


def list_skills(
    transport: Transport = http_transport,
    *,
    keyword: str = "",
    category: str = "",
    sort: str = "stars",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """One page of SkillHub's list: real-time search when ``keyword`` is given, ranking otherwise."""
    if sort not in {"stars", "downloads"}:
        raise MarketError("sort must be stars or downloads", reason="bad_request")
    params = {
        "page": max(1, int(page)),
        "pageSize": min(50, max(1, int(page_size))),
        "sortBy": sort,
        "order": "desc",
        "keyword": str(keyword or "").strip()[:80],
        "category": str(category or "").strip()[:64],
    }
    body = _get_json(transport, _api_url("/api/skills", params))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise MarketError("SkillHub's skill list has an unexpected shape", reason="bad_response")
    skills = [row for row in (normalize_listing(item) for item in data["skills"]) if row]
    if params["category"] == PAID_CATEGORY:
        # Everything SkillHub lists under its paid section is paid, whatever an entry's own fields say.
        for row in skills:
            row["paid"] = True
    return {"skills": skills, "total": _int(data.get("total")), "page": params["page"], "page_size": params["pageSize"]}


def top_skills(transport: Transport = http_transport, *, limit: int = 300, sort: str = "stars") -> list[dict[str, Any]]:
    """The first ``limit`` skills by ranking, a few pages of 50, stopping early when SkillHub runs out."""
    wanted = min(max(1, int(limit)), 1000)
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < wanted:
        batch = list_skills(transport, sort=sort, page=page, page_size=50)
        rows.extend(batch["skills"])
        if len(batch["skills"]) < 50:
            break
        page += 1
    seen: set[str] = set()
    unique = [row for row in rows if not (row["slug"] in seen or seen.add(row["slug"]))]
    return unique[:wanted]


def detail(transport: Transport, slug: str) -> dict[str, Any]:
    """Name, latest version and SkillHub's two security report links. The reports are shown as reference only."""
    _check_slug(slug)
    body = _get_json(transport, _api_url(f"/api/v1/skills/{quote(slug)}"))
    if not isinstance(body, dict) or not isinstance(body.get("skill"), dict):
        raise MarketError("SkillHub's skill detail has an unexpected shape", reason="bad_response")
    skill = body["skill"]
    reports = body.get("securityReports") if isinstance(body.get("securityReports"), dict) else {}
    latest = body.get("latestVersion") if isinstance(body.get("latestVersion"), dict) else {}
    return {
        "slug": slug,
        "name": str(skill.get("displayName") or slug),
        "category": str(skill.get("category") or ""),
        "version": str(latest.get("version") or ""),
        "paid": is_paid(str(skill.get("category") or ""), skill.get("labels"), body.get("pricing")),
        "security_reports": [
            {"vendor": str(vendor), "status": str((report or {}).get("status") or ""), "status_text": str((report or {}).get("statusText") or ""),
             "url": str((report or {}).get("reportUrl") or "") if str((report or {}).get("reportUrl") or "").startswith("https://") else ""}
            for vendor, report in reports.items() if isinstance(report, dict)
        ],
        "upstream_url": str(skill.get("upstream_url") or skill.get("sourceUrl") or ""),
    }


def _check_slug(slug: str) -> None:
    if not SLUG_RE.match(str(slug or "")):
        raise MarketError("that is not a SkillHub skill name", reason="bad_request")


def check_version(version: str) -> str:
    text = str(version or "")
    if not VERSION_RE.match(text) or ".." in text:
        raise MarketError("that is not a version this console can file", reason="bad_request")
    return text


def _safe_member(name: str) -> PurePosixPath | None:
    """A relative path that writes where it says on Windows too, or None.

    Refused: absolute or backslash paths, ``.``/``..``, any ``:`` (a drive or an NTFS stream), characters Windows
    will not store, hidden characters (bidi controls, zero-width and other format characters: the name on screen
    would not be the name on disk), a part ending in a dot or space (Windows drops them, so two names become one),
    device names (``sub/nul`` opens the NUL device, not a file) and git metadata (``.git``: git never commits it, so
    an admitted skill would list files no clone has).
    """
    if not name or name.startswith("/") or "\\" in name or any(char in UNSAFE_NAME_CHARS for char in name) or _hides_characters(name):
        return None
    path = PurePosixPath(name)
    if path.is_absolute():
        return None
    for part in name.split("/"):
        if part in {"", ".", ".."} or part.endswith((".", " ")) or WINDOWS_DEVICE_RE.match(part) or part.casefold() == ".git":
            return None
    return path


def _hides_characters(name: str) -> bool:
    """Whether a name holds a character that does not show as itself (HIDDEN_NAME_CHARS, HIDDEN_NAME_CATEGORIES)."""
    return any(_is_hidden(char) for char in name)


def _is_hidden(char: str) -> bool:
    return char in HIDDEN_NAME_CHARS or unicodedata.category(char) in HIDDEN_NAME_CATEGORIES


def shown_name(name: str, limit: int = 80) -> str:
    """A refused name as an owner may safely read it: every hidden character written out as <U+XXXX>, the rest (CJK
    included) as it is, so a refusal never displays a name reordered or disguised (Regression case)."""
    return "".join(f"<U+{ord(char):04X}>" if _is_hidden(char) else char for char in str(name or "")[:limit])


def _case_collision(paths: Any) -> bool:
    """Two paths that are one file on a case-insensitive disk."""
    seen: set[str] = set()
    for path in paths:
        folded = str(path).casefold()
        if folded in seen:
            return True
        seen.add(folded)
    return False


def _layout_clash(paths: Any) -> str:
    """What stops a set of relative paths being written as they are named on a Windows disk, in words, or "".

    Two paths that are one file once case is folded (``A.md`` / ``a.md``); one folder spelled two ways (``Docs/a.md`` /
    ``docs/b.md``: one folder on disk, so a record would name files by a path that is not theirs); a name that is a
    file and a folder at once (``x`` / ``x/y``, in any case: one of the two cannot be written).
    """
    files: set[str] = set()
    folders: dict[str, str] = {}
    for path in paths:
        text = str(path)
        folded = text.casefold()
        if folded in files:
            return "two files that differ only in case"
        files.add(folded)
        parts = text.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            if folders.setdefault(prefix.casefold(), prefix) != prefix:
                return "one folder spelled in two cases"
    if files & set(folders):
        return "a name that is both a file and a folder"
    return ""


def _fetch(transport: Transport, url: str, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    try:
        return transport(url, max_bytes)
    except MarketError:
        raise
    except Exception as exc:  # noqa: BLE001 - a network failure is something to show, not a crash
        raise MarketError(f"SkillHub is unreachable: {type(exc).__name__}", reason="unreachable") from exc


def fetch_verified_files(transport: Transport, slug: str) -> tuple[str, dict[str, bytes], str, dict[str, Any]]:
    """(version, {path: bytes}, zip_sha256, listing_mismatch): the download package's files, checked, or a refusal.

    The package is what lands (policy: 以下载包为准，差异标出来). SkillHub's per-file sha256 list still names
    the version -- the trial's folder -- and is held to its own sanity checks (plain version, safe paths and digests,
    no file twice, no case or folder clash, within the file and size limits); how the package differs from it comes
    back in ``listing_mismatch`` ({} when they agree) instead of refusing the download. Each side is checked on its own:
    a path one side spells ``Docs/a.md`` and the other ``docs/a.md`` is drift, recorded, not a clash. The package itself
    is refused for anything structural: a redirect anywhere but SkillHub's storage over https on its own port (no other
    port, no user in the address), a missing package (``package_missing``), links, unsafe or duplicate paths, names
    with hidden characters, case twins, a folder in two cases, a name that is a file and a folder, too many or too
    large files, sizes that do not add up, no SKILL.md.
    """
    _check_slug(slug)
    manifest = _get_json(transport, _api_url(f"/api/v1/skills/{quote(slug)}/files"))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    version = str(manifest.get("version") or "") if isinstance(manifest, dict) else ""
    if not isinstance(files, list) or not version:
        raise MarketError("SkillHub did not list this skill's files", reason="bad_response")
    if not VERSION_RE.match(version) or ".." in version:
        # The version becomes a folder name in the trial area; "../../x" or "C:/x" would file it elsewhere.
        raise MarketError("SkillHub's file list names a version that is not a plain version", reason="unsafe_archive")
    if len(files) > MAX_FILES:
        raise MarketError(f"the skill has more than {MAX_FILES} files", reason="too_large")
    expected: dict[str, str] = {}
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise MarketError("SkillHub's file list has an unexpected entry", reason="bad_response")
        path = _safe_member(str(entry.get("path") or ""))
        digest = str(entry.get("sha256") or "").lower()
        if path is None or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MarketError("SkillHub's file list names an unsafe path or digest", reason="unsafe_archive")
        if path.as_posix() in expected:
            raise MarketError("SkillHub's file list names a file twice", reason="unsafe_archive")
        expected[path.as_posix()] = digest
        total += _int(entry.get("size"))
    if total > MAX_TOTAL_BYTES:
        raise MarketError("the skill's files are larger than allowed", reason="too_large")
    # The list naming no SKILL.md is not refused here: when that is only the list's drift, the package has one; the
    # package is checked for it below.
    clash = _layout_clash(expected)
    if clash:
        raise MarketError(f"SkillHub's file list names {clash}", reason="unsafe_archive")

    status, headers, _body = _fetch(transport, _api_url("/api/v1/download", {"slug": slug}), 64 * 1024)
    location = headers.get("location", "")
    if status not in REDIRECT_STATUSES or not location:
        raise MarketError(f"SkillHub's download did not redirect to its storage (status {status})", reason="bad_status")
    parts = urlsplit(location)
    try:
        port = parts.port
    except ValueError:  # not a number, or out of range
        port = -1
    # The host alone is not enough: the storage host on another port (:8443) is another service, and a user in the
    # address (user@host) is not how SkillHub's storage is reached. Only https on its own port (none written, or 443).
    if (parts.scheme != "https" or not DOWNLOAD_HOST_RE.fullmatch(str(parts.hostname or "")) or port not in (None, 443)
            or "@" in parts.netloc):
        raise MarketError("the download points somewhere other than SkillHub's storage", reason="unexpected_host")
    status, _headers, archive = _fetch(transport, location, MAX_ZIP_BYTES)
    if status == 404:
        # Regression case: the storage answers 404 <Error><Code>NoSuchKey</Code> for a version SkillHub lists but never
        # stored. Nothing to retry here; the page says SkillHub is missing the package.
        raise MarketError("SkillHub's storage has no package for this version", reason="package_missing")
    if status != 200:
        raise MarketError(f"the download answered {status}", reason="bad_status")
    zip_sha256 = hashlib.sha256(archive).hexdigest()
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise MarketError("the download is not a zip archive", reason="unsafe_archive") from exc
    contents: dict[str, bytes] = {}
    with bundle:
        members = bundle.infolist()
        if len(members) > MAX_FILES * 2:
            raise MarketError("the archive has too many entries", reason="too_large")
        unpacked = 0
        for info in members:
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise MarketError("the archive contains a link", reason="unsafe_archive")
            path = _safe_member(info.filename)
            if path is None:
                # Escaped: a name hiding a bidi control would otherwise read as something else wherever this is shown.
                raise MarketError(f"the archive contains an unsafe path: {info.filename[:80]!a}", reason="unsafe_archive")
            key = path.as_posix()
            if key in contents:
                raise MarketError("the archive names a file twice", reason="unsafe_archive")
            unpacked += info.file_size
            if unpacked > MAX_TOTAL_BYTES:
                raise MarketError("the archive unpacks larger than allowed", reason="too_large")
            data = bundle.read(info)
            if len(data) != info.file_size:
                raise MarketError("the archive's sizes do not add up", reason="unsafe_archive")
            contents[key] = data
    if len(contents) > MAX_FILES:
        # What the list was held to, and what an admission record can list (workflow_packages.ADMISSION_MAX_FILES).
        raise MarketError(f"the archive has more than {MAX_FILES} files", reason="too_large")
    clash = _layout_clash(contents)
    if clash:
        raise MarketError(f"the archive names {clash}", reason="unsafe_archive")
    if "SKILL.md" not in contents:
        raise MarketError("the archive has no SKILL.md at its root", reason="not_a_skill")
    return version, contents, zip_sha256, listing_mismatch(expected, contents, version, location)


def listing_mismatch(expected: dict[str, str], contents: dict[str, bytes], version: str, location: str = "") -> dict[str, Any]:
    """How a download package differs from SkillHub's per-file sha256 list for the same version; {} when they agree.

    SkillHub fills the list and the package from separate syncs (both "Synced by skillhub pipeline"), so they drift --
    Regression case on about half of the most-starred free skills. That is SkillHub's drift, not evidence of tampering,
    and it is recorded rather than refused: ``different`` (in both, other bytes), ``missing`` (listed, not in the
    package) and ``extra`` (in the package, not listed), each sorted and capped at MISMATCH_LIST_LIMIT names with its
    whole count beside it, and the versions the package names itself (its storage path, its ``_meta.json``), with
    ``version_differs`` when either is not the list's. A version the package does not name is "" -- unknown, not a
    difference (a storage path ending ``latest.zip`` names none).
    """
    listed, packed = set(expected), set(contents)
    groups = {
        "different": sorted(key for key in listed & packed if hashlib.sha256(contents[key]).hexdigest() != expected[key]),
        "missing": sorted(listed - packed),
        "extra": sorted(packed - listed),
    }
    package_version = _package_version(location)
    meta_version = _meta_version(contents.get(META_FILE))
    version_differs = any(found and found != version for found in (package_version, meta_version))
    if not any(groups.values()) and not version_differs:
        return {}
    record: dict[str, Any] = {
        "listing_version": version, "package_version": package_version, "meta_version": meta_version, "version_differs": version_differs,
    }
    for name, paths in groups.items():
        record[name] = paths[:MISMATCH_LIST_LIMIT]
        record[f"{name}_count"] = len(paths)
    return record


def _plain_version(text: str) -> str:
    return text if VERSION_RE.match(text) and ".." not in text else ""


def _package_version(location: str) -> str:
    """The version a storage path names (``.../<slug>/<version>.zip``), or "" when it names none -- unknown.

    Only a name that looks like a version counts: a digit first and a dot in it (``1.0.46``). ``latest.zip`` or
    ``download.zip`` says nothing about the version; read as one, it flagged every such package as another version.
    """
    name = unquote(PurePosixPath(urlsplit(location).path).name) if location else ""
    stem = name[: -len(".zip")] if name.endswith(".zip") else ""
    return _plain_version(stem) if re.match(r"[0-9]", stem) and "." in stem else ""


def _meta_version(data: bytes | None) -> str:
    """The version a package's own small ``_meta.json`` names, or "" when it has none, is large or is not JSON."""
    if not data or len(data) > MAX_META_BYTES:
        return ""
    try:
        meta = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return ""
    return _plain_version(str(meta.get("version") or "")) if isinstance(meta, dict) else ""


def check_source(source: str) -> str:
    text = str(source or SOURCE)
    if text not in TRIAL_SOURCES:
        raise MarketError("that is not a trial source this console knows", reason="bad_request")
    return text


def trials_root(ampuly_home: Path, source: str = SOURCE) -> Path:
    return Path(ampuly_home) / TRIALS_DIR / check_source(source)


def trial_folder(ampuly_home: Path, slug: str, version: str, source: str = SOURCE) -> Path:
    """Where one downloaded version lives, checked to stay inside the trial area whatever the names say."""
    _check_slug(slug)
    check_version(version)
    root = trials_root(ampuly_home, source)
    folder = root / slug / version
    if not folder.resolve().is_relative_to(root.resolve()):
        raise MarketError("that trial would be filed outside the trial area", reason="bad_request")
    return folder


def download_to_trial(
    transport: Transport,
    ampuly_home: Path,
    slug: str,
    *,
    actor: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch, check and scan one skill into the trial area. Refuses paid skills before any file is taken."""
    info = detail(transport, slug)
    if info["paid"]:
        raise MarketError("按次付费的技能暂不下载：还没有预算与支付通道", reason="paid")
    version, contents, zip_sha256, mismatch = fetch_verified_files(transport, slug)
    return file_trial(
        ampuly_home, SOURCE, slug, version, contents, name=info["name"], category=info["category"], actor=actor, now=now,
        # How the package differs from SkillHub's own file list ({} when they agree): shown on the trial card, and
        # admitting the trial needs the owner to acknowledge it (component_admission).
        extra={"zip_sha256": zip_sha256, "security_reports": info["security_reports"], "listing_mismatch": mismatch},
    )


def file_trial(
    ampuly_home: Path,
    source: str,
    slug: str,
    version: str,
    contents: dict[str, bytes],
    *,
    name: str,
    category: str,
    actor: str,
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """File checked bytes as one trial: staged, scanned, recorded, then moved into place in one step.

    Every file in ``contents`` is written and both scans (scan_skill, check_secrets) walk all of them -- for a SkillHub
    package that includes files its own list does not name. A version already filed and not discarded is returned as
    it is. A discarded one is replaced by the fresh copy (its files may have been changed, which is why 重新试用
    refused), and its history of tests and decisions carries over.
    """
    from ampuly.cli.distribution import check_secrets
    from ampuly.plugins.office_ui.component_catalog import scan_skill, skill_governance

    source = check_source(source)
    root = trials_root(ampuly_home, source)
    target = trial_folder(ampuly_home, slug, version, source)
    previous: dict[str, Any] | None = None
    if (target / TRIAL_FILE).is_file():
        existing = load_trial(target)
        if existing.get("status") != "discarded":
            return existing
        previous = existing
    staging = root / f".staging-{slug}-{uuid.uuid4().hex[:8]}"
    try:
        for key, data in contents.items():
            destination = staging / "files" / PurePosixPath(key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        files_root = staging / "files"
        # The scan reads a <category>/<skill> layout; the trial's own folder stands in for both.
        facts = scan_skill(files_root, staging)
        # scan_skill names a skill after its folder and category; a trial's folder is neither, so say what it is.
        facts["id"] = slug
        facts["category"] = category
        if not facts.get("frontmatter") or facts.get("name") == files_root.name:
            facts["name"] = name
        facts["source"] = {"path": f"{source}/{slug}/{version}", "marketplace_id": slug, "skill_md_sha256": hashlib.sha256(contents["SKILL.md"]).hexdigest()}
        secrets = check_secrets(staging, roots=("files",), every_file=True)
        record = {
            "schema_version": 1,
            "source": source,
            "slug": slug,
            "name": name,
            "version": version,
            "category": category,
            "downloaded_by": str(actor or "").strip(),
            "downloaded_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "zip_sha256": "",
            "files_sha256": {key: hashlib.sha256(data).hexdigest() for key, data in sorted(contents.items())},
            "security_reports": [],
            # The caller refused a paid skill before filing it; recorded so admission refuses a record that says otherwise.
            "paid": False,
            "facts": facts,
            "governance": skill_governance(facts),
            # Where each finding is (file and line), never the matched text.
            "secret_findings": [hit.removeprefix("files/") for hit in secrets[:20]],
            "status": "downloaded",
            "tests": [],
            **(extra or {}),
        }
        if previous is not None:
            for key in ("tests", "discards", "restored", "redownloads"):
                if previous.get(key):
                    record[key] = list(previous[key])
            record["redownloads"] = [*list(record.get("redownloads") or []), {
                "by": str(actor or "").strip(), "at": record["downloaded_at"], "replaced_zip_sha256": str(previous.get("zip_sha256") or ""),
            }]
        (staging / TRIAL_FILE).write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        if previous is not None:
            replaced = root / f".replaced-{slug}-{uuid.uuid4().hex[:8]}"
            replace_with_retry(target, replaced)
            try:
                replace_with_retry(staging, target)
            except OSError:
                replace_with_retry(replaced, target)
                raise
            shutil.rmtree(replaced, ignore_errors=True)
        else:
            try:
                replace_with_retry(staging, target)
            except OSError:
                # Two filings of one version at once: the other one filed it first.
                if not (target / TRIAL_FILE).is_file():
                    raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return load_trial(target)


def load_trial(folder: Path) -> dict[str, Any]:
    record = json.loads((Path(folder) / TRIAL_FILE).read_text(encoding="utf-8"))
    return record if isinstance(record, dict) else {}


MAX_VIEW_CHARS = 60000


def read_trial_file(ampuly_home: Path, slug: str, version: str, path: str, source: str = SOURCE) -> dict[str, Any]:
    """One file of a trial for a person to read: only a path the download recorded, text shown, binaries described."""
    try:
        folder = trial_folder(ampuly_home, slug, version, source)
    except MarketError as exc:
        raise MarketError("试用区里没有这个技能版本", reason="not_found") from exc
    if not (folder / TRIAL_FILE).is_file():
        raise MarketError("试用区里没有这个技能版本", reason="not_found")
    record = load_trial(folder)
    if str(path) not in dict(record.get("files_sha256") or {}):
        raise MarketError("这个文件不在下载记录里", reason="not_found")
    data = (folder / "files" / PurePosixPath(str(path))).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": path, "binary": True, "text": "", "truncated": False, "size": len(data)}
    return {"path": path, "binary": False, "text": text[:MAX_VIEW_CHARS], "truncated": len(text) > MAX_VIEW_CHARS, "size": len(data)}


def list_trials(ampuly_home: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in TRIAL_SOURCES:
        rows.extend(_list_source_trials(ampuly_home, source))
    return rows[:500]


def _list_source_trials(ampuly_home: Path, source: str) -> list[dict[str, Any]]:
    root = trials_root(ampuly_home, source)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for trial in sorted(root.glob(f"*/*/{TRIAL_FILE}"))[:500]:
        slug, version = trial.parent.parent.name, trial.parent.name
        # Only <slug>/<version>/TRIAL.json: a staging folder's files/TRIAL.json is a downloaded file, not our record.
        if not SLUG_RE.match(slug) or not VERSION_RE.match(version) or ".." in version:
            continue
        try:
            record = load_trial(trial.parent)
        except (OSError, ValueError):
            continue
        if record.get("slug") != slug or record.get("version") != version or (record.get("source") or SOURCE) != source:
            continue
        mismatch = trial_listing_mismatch(record)
        rows.append({key: record.get(key) for key in (
            "source", "slug", "name", "version", "category", "downloaded_at", "status", "governance", "secret_findings", "security_reports", "tests",
        )} | {"files": sorted(record.get("files_sha256", {}))}
            # The SKILL.md a catalog import was checked against: the page tells a trial the library has since changed.
            | ({"catalog_skill_md_sha256": str(record.get("catalog_skill_md_sha256") or "")} if source == "catalog" else {})
            # How SkillHub's package differed from its own file list ({} when they agreed): the card warns, admission asks;
            # the page sends the key back with the owner's yes, so the yes is to these differences ("" when none).
            | ({"listing_mismatch": mismatch, "listing_mismatch_key": listing_mismatch_key(mismatch)} if source == SOURCE else {}))
    return rows


def trial_listing_mismatch(record: dict[str, Any]) -> dict[str, Any]:
    """The differences a trial record carries between SkillHub's file list and its package; {} for none (or a catalog trial)."""
    value = record.get("listing_mismatch")
    return dict(value) if isinstance(value, dict) else {}


def listing_mismatch_key(mismatch: dict[str, Any]) -> str:
    """The fingerprint of one set of differences ("" for none): sha256 of its canonical JSON (sorted keys, compact).

    What the owner's acknowledgement is bound to. The page shows a trial's differences and sends this key back with the
    owner's yes; admission refuses a key that is not the trial's current one (component_admission), so a yes given to
    one set of differences -- an older page, a trial downloaded again since -- never admits another.
    """
    if not mismatch:
        return ""
    return hashlib.sha256(json.dumps(mismatch, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
