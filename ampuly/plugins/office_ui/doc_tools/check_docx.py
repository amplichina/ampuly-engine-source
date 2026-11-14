"""Inspect DOCX bytes without following relationships or executing content.

This is a structural/text checker, not a renderer, semantic judge or permission
service. The caller must supply authorized, version-bound bytes.
"""

from __future__ import annotations

from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
import re
from typing import Mapping, Sequence
from zipfile import ZipFile

from lxml import etree

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_TEXT_CHARS = 1_000_000
MAX_XML_NODES_PER_PART = 50_000
MAX_XML_NODES = 200_000
MAX_FINDINGS = 2000
DEFAULT_MARKERS = ("{{", "}}", "${", "TODO", "TBD", "待填写", "示例客户", "测试公司")


class PackageRejected(ValueError):
    """Unsafe, unsupported or oversized package; never continue parsing it."""


def _package(raw: bytes) -> ZipFile:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT_BYTES:
        raise PackageRejected("input_size_or_type")
    archive = ZipFile(BytesIO(raw))
    try:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(entries) > MAX_ENTRIES or len(set(names)) != len(names):
            raise PackageRejected("entry_count_or_duplicate")
        if sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
            raise PackageRejected("expanded_size")
        for item in entries:
            name = item.filename
            if len(name) > 260 or name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/"):
                raise PackageRejected("invalid_part_path")
            if item.flag_bits & 1 or (item.file_size > 1024 * 1024 and item.file_size > max(1, item.compress_size) * 200):
                raise PackageRejected("encrypted_or_excessive_compression")
        return archive
    except Exception:
        archive.close()
        raise


def _xml(raw: bytes):
    # Feed in chunks to stop before constructing an unbounded empty-node DOM.
    parser = etree.XMLPullParser(events=("start",), resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)
    nodes = 0
    for offset in range(0, len(raw), 32_768):
        parser.feed(raw[offset:offset + 32_768])
        for _ in parser.read_events():
            nodes += 1
            if nodes > MAX_XML_NODES_PER_PART:
                raise PackageRejected("xml_node_size")
    root = parser.close()
    if root.getroottree().docinfo.doctype:
        raise PackageRejected("xml_doctype")
    return root, nodes


def check_docx(
    raw: bytes,
    *,
    required_sections: Sequence[str] = (),
    required_facts: Mapping[str, str] | None = None,
    placeholder_markers: Sequence[str] = DEFAULT_MARKERS,
    protected_parts: Mapping[str, str] | None = None,
) -> dict:
    """Return machine-checkable findings, never claim Word/WPS or visual QA.

    Facts are literal presence checks in body/table text only, not properties
    or headers/footers. A confirmed-content revision and
    human semantic review must be attached by the caller; this cannot prove
    that every statement in a document is true.
    """
    fingerprint = sha256(raw).hexdigest() if isinstance(raw, bytes) else ""
    report = {
        "status": "failed", "content_hash": fingerprint,
        "checker": {"name": "ampuly_docx_structure", "version": "0.1", "lxml": version("lxml")},
        "checks": [], "findings": [], "visual_status": "unverified",
        "semantic_status": "requires_confirmed_content_and_human_review",
        "opened_with": None, "word_wps_status": "unverified",
        "fact_scope": "word/document.xml paragraph and table text",
    }
    checks = report["checks"]
    findings = report["findings"]

    def finding(code: str, *, part: str = "", detail: str = "") -> None:
        if len(findings) >= MAX_FINDINGS:
            raise PackageRejected("finding_size")
        findings.append({"code": code, "part": part, "detail": detail})

    try:
        if len(required_sections) > 200 or len(required_facts or {}) > 500 or len(placeholder_markers) > 50:
            raise PackageRejected("check_spec_size")
        if any(not isinstance(value, str) or not value or len(value) > 4000 for value in required_sections):
            raise PackageRejected("section_invalid")
        if required_facts is not None and (not isinstance(required_facts, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value or len(value) > 4000
            for key, value in required_facts.items()
        )):
            raise PackageRejected("fact_spec_invalid")
        if protected_parts is not None and (not isinstance(protected_parts, Mapping) or len(protected_parts) > 100
            or any(not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value)
                   for key, value in protected_parts.items())):
            raise PackageRejected("protected_parts_invalid")
        if any(not isinstance(value, str) or not value or len(value) > 1000 for value in placeholder_markers):
            raise PackageRejected("marker_invalid")
        with _package(raw) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise PackageRejected("not_docx")
            paragraphs: list[str] = []
            part_texts: list[tuple[str, str]] = []
            text_length = 0
            node_count = 0
            for name in sorted(names):
                lower = name.lower()
                if "vbaproject" in lower or lower.startswith("word/embeddings/"):
                    finding("macro_or_embedding", part=name)
                if not (lower.endswith(".xml") or lower.endswith(".rels")):
                    continue
                root, nodes = _xml(archive.read(name))
                node_count += nodes
                if node_count > MAX_XML_NODES:
                    raise PackageRejected("xml_node_size")
                if lower == "[content_types].xml":
                    for element in root:
                        content_type = element.get("ContentType", "").lower()
                        if "macroenabled" in content_type or "vba" in content_type:
                            finding("macro_content_type", part=name)
                if lower.endswith(".rels"):
                    for relationship in root:
                        if relationship.get("TargetMode", "").lower() == "external":
                            # Do not return the target: it may contain private information.
                            finding("external_relationship", part=name)
                    continue
                if name.startswith("word/") or name.startswith("docProps/"):
                    # A placeholder may be split into Word runs; preserve the
                    # concatenated text inside a paragraph, not newlines per run.
                    pieces = ["".join(element.text or "" for element in paragraph.iter()
                        if isinstance(element.tag, str) and etree.QName(element).localname in {"t", "instrText"})
                        for paragraph in root.iter() if isinstance(paragraph.tag, str) and etree.QName(paragraph).localname == "p"]
                    if name.startswith("docProps/"):
                        pieces += list(root.itertext())
                    text = "\n".join(pieces)
                    text_length += len(text)
                    if text_length > MAX_TEXT_CHARS:
                        raise PackageRejected("text_size")
                    part_texts.append((name, text))
                    for element in root.iter():
                        if not isinstance(element.tag, str):
                            continue
                        local = etree.QName(element).localname
                        if local in {"ins", "del", "comment", "commentRangeStart", "commentReference"}:
                            finding("review_markup", part=name)
                        if local in {"altChunk", "object", "OLEObject"}:
                            finding("unsupported_embedded_content", part=name)
                    if name == "word/document.xml":
                        for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                            paragraphs.append("".join(paragraph.itertext()))
            for name, text in part_texts:
                for marker in placeholder_markers:
                    if marker in text:
                        finding("placeholder", part=name, detail=marker)
            body_text = "\n".join(text for name, text in part_texts if name == "word/document.xml")
            for section in required_sections:
                present = isinstance(section, str) and section in paragraphs
                checks.append({"kind": "section_text", "value": section, "passed": present})
                if not present:
                    finding("missing_section", detail=str(section))
            for fact_id, value in (required_facts or {}).items():
                present = isinstance(value, str) and bool(value) and value in body_text
                checks.append({"kind": "literal_fact_presence", "id": fact_id, "passed": present})
                if not present:
                    finding("missing_fact", detail=str(fact_id))
            for name, expected in (protected_parts or {}).items():
                actual = sha256(archive.read(name)).hexdigest() if name in names else None
                matches = actual is not None and actual == expected.lower()
                checks.append({"kind": "protected_part_hash", "part": name, "passed": matches})
                if not matches:
                    finding("protected_part_changed", part=name)
        from docx import Document
        Document(BytesIO(raw))
        report["opened_with"] = {"library": "python-docx", "version": version("python-docx")}
        report["status"] = "failed" if findings else "success"
    except Exception:
        # Exception strings can contain document text or targets; keep a stable code.
        findings.append({"code": "invalid_or_unsupported_package", "part": "", "detail": ""})
    return report
