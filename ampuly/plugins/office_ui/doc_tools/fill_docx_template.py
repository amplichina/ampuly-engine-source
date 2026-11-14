"""Fill declared plain DOCX body/table placeholders from caller-confirmed text.

This bytes-only leaf has no file, model, approval, registration or delivery API.
Only UTF-8 ``w:t`` text in ordinary body paragraphs/table cells is editable.
Run properties and every other package part retain their original bytes. That
does not prove Word/WPS layout, semantic correctness or complete DOCX fidelity.
All media, including python-docx's usual preview thumbnail, is unsupported; a
stock python-docx package therefore needs an explicitly prepared plain template.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from hashlib import sha256
from io import BytesIO
import json
import posixpath
import re
from typing import Mapping, Sequence
from xml.parsers import expat
from xml.sax.saxutils import escape
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from .check_docx import check_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main|"
XML_SPACE = "http://www.w3.org/XML/1998/namespace|space"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships|"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types|"
MAIN_PART = "word/document.xml"
DOCX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_KEY = re.compile(r"[\w.-]{1,80}", re.UNICODE)
_PLACEHOLDER = re.compile(r"\{\{([\w.-]{1,80})\}\}", re.UNICODE)
_HASH = re.compile(r"[a-fA-F0-9]{64}")


class TemplateFillRejected(ValueError):
    """A stable error code; never include supplied content or relationship URLs."""


@dataclass(frozen=True)
class TemplateFillLimits:
    """Callers may lower these hard budgets, never increase them."""

    input_bytes: int = 8 * 1024 * 1024
    output_bytes: int = 8 * 1024 * 1024
    entries: int = 100
    expanded_bytes: int = 16 * 1024 * 1024
    part_bytes: int = 4 * 1024 * 1024
    xml_nodes_per_part: int = 40_000
    xml_nodes: int = 100_000
    xml_depth: int = 128
    text_chars: int = 500_000
    value_chars: int = 10_000
    replacements: int = 500
    placeholders: int = 100


@dataclass
class _Budget:
    limits: TemplateFillLimits
    nodes: int = 0
    text: int = 0


@dataclass
class _TextSpan:
    start: int
    open_end: int
    close_start: int = 0
    text: str = ""
    chunks: list[str] = field(default_factory=list)
    space: str | None = None
    self_closing: bool = False


@dataclass
class _Paragraph:
    index: int
    source_offset: int
    in_table: bool
    supported: bool
    spans: list[_TextSpan] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


def _reject(code: str) -> None:
    raise TemplateFillRejected(code)


def _valid_limits(limits: TemplateFillLimits | None) -> TemplateFillLimits:
    limits = TemplateFillLimits() if limits is None else limits
    if not isinstance(limits, TemplateFillLimits):
        _reject("limits_invalid")
    maximum = TemplateFillLimits()
    for item in fields(limits):
        value = getattr(limits, item.name)
        if type(value) is not int or not 0 < value <= getattr(maximum, item.name):
            _reject("limits_invalid")
    return limits


def _content_spec(allowlist: Sequence[str], values: Mapping[str, str], revision: str,
                  limits: TemplateFillLimits) -> tuple[list[str], dict[str, str]]:
    if (not isinstance(allowlist, (list, tuple, set, frozenset))
            or not 1 <= len(allowlist) <= limits.placeholders
            or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in allowlist)
            or len(set(allowlist)) != len(allowlist)):
        _reject("placeholder_allowlist_invalid")
    if (not isinstance(revision, str) or not revision.strip() or len(revision) > 512
            or not isinstance(values, Mapping) or set(values) != set(allowlist)):
        _reject("confirmed_content_or_mapping_invalid")
    copied: dict[str, str] = {}
    total = 0
    for key in sorted(allowlist):
        value = values[key]
        if not isinstance(value, str) or len(value) > limits.value_chars:
            _reject("value_size_or_type")
        # New lines/tabs require actual OOXML break/tab nodes: this leaf does
        # not pretend that literal XML whitespace renders like Word controls.
        if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF)
               for char in value) or "{{" in value or "}}" in value:
            _reject("value_control_or_placeholder")
        total += len(value)
        if total > limits.text_chars:
            _reject("value_text_size")
        copied[key] = value
    return sorted(allowlist), copied


def template_content_hash(placeholder_allowlist: Sequence[str], values: Mapping[str, str],
                          *, revision_id: str) -> str:
    """Hash the exact allowlist, text values and caller-confirmed revision.

    This binds a reference to content; it cannot grant approval or validate that
    the caller actually obtained it. The caller retains that responsibility.
    """
    keys, copied = _content_spec(placeholder_allowlist, values, revision_id, TemplateFillLimits())
    payload = {"schema": "ampuly.docx.template-content.v1", "revision_id": revision_id,
               "placeholder_allowlist": keys, "values": copied}
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _part_name(name: str) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 260 and not name.startswith("/")
            and not any(char in name for char in "\\:\x00%?#")
            and not any(ord(char) < 32 or ord(char) == 127 for char in name)
            and all(piece not in ("", ".", "..") for piece in name.split("/")))


def _tag_end(raw: bytes, start: int) -> int:
    quote = 0
    for index in range(start, len(raw)):
        byte = raw[index]
        if quote:
            if byte == quote:
                quote = 0
        elif byte in (34, 39):
            quote = byte
        elif byte == 62:
            return index + 1
    _reject("xml_tag_invalid")


def _scan_xml(raw: bytes, name: str, budget: _Budget, part_names: set[str]) -> tuple[str, list[_Paragraph]]:
    """Stream with expat budgets; capture byte spans without serializing XML."""
    if b"\x00" in raw:
        _reject("xml_encoding_unsupported")
    parser = expat.ParserCreate(namespace_separator="|")
    stack: list[str] = []
    paragraphs: list[_Paragraph] = []
    paragraph: _Paragraph | None = None
    paragraph_depth = 0
    active_span: _TextSpan | None = None
    text_depth = 0
    text_chunks: list[str] = []
    part_nodes = 0
    main_type = False
    body_count = 0

    def declaration(version, encoding, standalone):
        if version != "1.0" or (encoding and encoding.lower() not in ("utf-8", "utf8")):
            _reject("xml_encoding_unsupported")

    def forbidden(*args):
        _reject("xml_dtd_entity_or_processing_instruction")

    def start(tag, attrs):
        nonlocal part_nodes, paragraph, paragraph_depth, active_span, text_depth, main_type, body_count
        part_nodes += 1
        budget.nodes += 1
        if (part_nodes > budget.limits.xml_nodes_per_part or budget.nodes > budget.limits.xml_nodes
                or len(stack) >= budget.limits.xml_depth):
            _reject("xml_node_or_depth_size")
        # Attribute text is bounded and checked too: no placeholders can hide
        # in bookmarks, field attributes, or uneditable XML metadata.
        for value in attrs.values():
            budget.text += len(value)
            if "{{" in value or "}}" in value:
                _reject("placeholder_location_unsupported")
        if budget.text > budget.limits.text_chars:
            _reject("xml_text_size")
        if active_span is not None:
            _reject("text_child_unsupported")
        local = tag.rsplit("|", 1)[-1]
        if local in {"drawing", "pict", "object", "OLEObject", "altChunk", "ins", "del",
                     "comment", "commentRangeStart", "commentRangeEnd", "commentReference"}:
            _reject("media_embedding_or_review_unsupported")
        if name.endswith(".rels"):
            if not stack and tag != REL_NS + "Relationships":
                _reject("relationships_invalid")
            if stack and (stack != [REL_NS + "Relationships"] or tag != REL_NS + "Relationship"):
                _reject("relationships_invalid")
            if tag == REL_NS + "Relationship":
                target = attrs.get("Target", "")
                mode = attrs.get("TargetMode", "Internal")
                source_dir = "" if name == "_rels/.rels" else posixpath.dirname(posixpath.dirname(name))
                resolved = posixpath.normpath(posixpath.join(source_dir, target))
                if (mode != "Internal" or not target or target.startswith("/")
                        or any(char in target for char in "\\:\x00%?#") or not _part_name(resolved)
                        or resolved not in part_names):
                    _reject("external_or_unsupported_relationship")
        if name == "[Content_Types].xml":
            if not stack and tag != CT_NS + "Types":
                _reject("content_types_invalid")
            content_type = attrs.get("ContentType", "").lower()
            if (content_type.startswith("image/") or any(token in content_type for token in
                    ("macro", "vba", "oleobject", "activex", "embedded"))):
                _reject("media_macro_or_embedding_unsupported")
            if tag == CT_NS + "Override" and attrs.get("PartName") == "/" + MAIN_PART:
                if content_type != DOCX_MAIN_TYPE:
                    _reject("main_content_type_unsupported")
                main_type = True
        if name == MAIN_PART:
            if not stack and tag != W + "document":
                _reject("main_document_invalid")
            if tag == W + "body":
                body_count += 1
                if stack != [W + "document"] or body_count != 1:
                    _reject("main_document_invalid")
            if tag == W + "p":
                if paragraph is not None:
                    _reject("nested_paragraph_unsupported")
                path = tuple(item.removeprefix(W) for item in stack)
                tail = path[2:]
                supported = path[:2] == ("document", "body") and (
                    not tail or (len(tail) % 3 == 0 and tail == ("tbl", "tr", "tc") * (len(tail) // 3)))
                paragraph = _Paragraph(len(paragraphs), parser.CurrentByteIndex, bool(tail), supported)
                paragraphs.append(paragraph)
                paragraph_depth = len(stack)
            elif paragraph is not None:
                ancestors = stack[paragraph_depth + 1:]
                within_properties = W + "pPr" in ancestors or W + "rPr" in ancestors
                normal = ((not ancestors and tag in (W + "r", W + "pPr"))
                          or (ancestors == [W + "r"] and tag in (W + "t", W + "rPr")))
                if not (within_properties or normal):
                    paragraph.supported = False
            if tag in (W + "t", W + "instrText"):
                text_depth = len(stack) + 1
                if tag == W + "t" and paragraph is not None:
                    offset = parser.CurrentByteIndex
                    open_end = _tag_end(raw, offset)
                    active_span = _TextSpan(offset, open_end, space=attrs.get(XML_SPACE),
                                            self_closing=raw[offset:open_end].rstrip().endswith(b"/>"))
                    paragraph.spans.append(active_span)
        stack.append(tag)

    def text(value):
        budget.text += len(value)
        if budget.text > budget.limits.text_chars:
            _reject("xml_text_size")
        text_chunks.append(value)
        if active_span is not None:
            active_span.chunks.append(value)
        if paragraph is not None and text_depth and len(stack) == text_depth:
            paragraph.text.append(value)

    def end(tag):
        nonlocal paragraph, active_span, text_depth
        if tag == W + "t" and active_span is not None:
            active_span.close_start = parser.CurrentByteIndex
            active_span.text = "".join(active_span.chunks)
            active_span.chunks.clear()
            active_span = None
        if tag in (W + "t", W + "instrText"):
            text_depth = 0
        if tag == W + "p":
            paragraph = None
        stack.pop()

    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.ProcessingInstructionHandler = forbidden
    parser.StartElementHandler = start
    parser.CharacterDataHandler = text
    parser.EndElementHandler = end
    # CDATA/comments inside text must not vanish during replacement.
    parser.StartCdataSectionHandler = lambda: _reject("xml_cdata_unsupported")
    parser.CommentHandler = lambda value: _reject("xml_comment_unsupported") if active_span else None
    try:
        for offset in range(0, len(raw), 16_384):
            parser.Parse(raw[offset:offset + 16_384], False)
        parser.Parse(b"", True)
    except expat.ExpatError:
        _reject("xml_invalid")
    if name == "[Content_Types].xml" and not main_type:
        _reject("main_content_type_missing")
    if name == MAIN_PART and body_count != 1:
        _reject("main_document_invalid")
    return "".join(text_chunks), paragraphs


def _load_parts(raw: bytes, limits: TemplateFillLimits):
    if not isinstance(raw, bytes) or not raw or len(raw) > limits.input_bytes:
        _reject("input_size_or_type")
    archive = ZipFile(BytesIO(raw))
    try:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > limits.entries or len(set(name.casefold() for name in names)) != len(names):
            _reject("entry_count_or_duplicate")
        if sum(entry.file_size for entry in entries) > limits.expanded_bytes:
            _reject("expanded_size")
        if not {MAIN_PART, "[Content_Types].xml", "_rels/.rels"}.issubset(names):
            _reject("not_docx")
        # All metadata is checked before a single entry is expanded.
        for entry in entries:
            name = entry.filename
            lower = name.lower()
            if not _part_name(name) or entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                _reject("part_path_or_link_invalid")
            if (entry.flag_bits & 1 or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                    or entry.file_size > limits.part_bytes
                    or entry.file_size > max(1, entry.compress_size) * 200):
                _reject("encrypted_compression_or_part_size")
            if (not lower.endswith((".xml", ".rels")) or any(token in lower for token in
                    ("/media/", "/embeddings/", "vbaproject", "activex", "_xmlsignatures/"))):
                _reject("binary_media_macro_or_signature_unsupported")
        parts = {entry.filename: archive.read(entry) for entry in entries}
        return entries, parts, archive.comment
    finally:
        archive.close()


def _fill_main(raw: bytes, paragraphs: list[_Paragraph], values: Mapping[str, str],
               revision: str, limits: TemplateFillLimits):
    edits: list[tuple[int, int, bytes]] = []
    content_map: list[dict] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        joined = "".join(paragraph.text)
        matches = list(_PLACEHOLDER.finditer(joined))
        remainder = _PLACEHOLDER.sub("", joined)
        if "{{" in remainder or "}}" in remainder:
            _reject("malformed_or_unresolved_placeholder")
        if not matches:
            continue
        if not paragraph.supported:
            _reject("placeholder_structure_unsupported")
        texts = [span.text for span in paragraph.spans]
        offsets = []
        offset = 0
        for text in texts:
            offsets.append((offset, offset + len(text)))
            offset += len(text)
        for match in reversed(matches):
            key = match.group(1)
            if key not in values:
                _reject("unmapped_placeholder")
            seen.add(key)
            first = next(index for index, (start, end) in enumerate(offsets) if start <= match.start() < end)
            last = next(index for index, (start, end) in enumerate(offsets) if start < match.end() <= end)
            left = match.start() - offsets[first][0]
            right = match.end() - offsets[last][0]
            suffix = texts[last][right:]
            texts[first] = texts[first][:left] + values[key] + (suffix if first == last else "")
            if first != last:
                for index in range(first + 1, last):
                    texts[index] = ""
                texts[last] = suffix
            content_map.append({"placeholder": key, "part": MAIN_PART, "paragraph": paragraph.index,
                                "in_table": paragraph.in_table, "source_revision": revision,
                                "value_hash": sha256(values[key].encode("utf-8")).hexdigest(),
                                "replacement_text_node": first, "last_text_node": last,
                                "paragraph_xml_offset": paragraph.source_offset,
                                "source_char_start": match.start(), "source_char_end": match.end(),
                                "first_text_xml_offset": paragraph.spans[first].start})
            if len(content_map) > limits.replacements:
                _reject("replacement_size")
        for span, text in zip(paragraph.spans, texts):
            if span.text == text:
                continue
            if span.self_closing:
                _reject("empty_text_span_unsupported")
            edits.append((span.open_end, span.close_start, escape(text).encode("utf-8")))
            if text and (text[0].isspace() or text[-1].isspace()) and span.space != "preserve":
                opening = raw[span.start:span.open_end]
                if span.space is None:
                    opening = opening[:-1] + b' xml:space="preserve">'
                else:
                    opening, count = re.subn(rb"xml:space\s*=\s*(['\"]).*?\1", b'xml:space="preserve"', opening)
                    if count != 1:
                        _reject("xml_space_attribute_unsupported")
                edits.append((span.start, span.open_end, opening))
    if seen != set(values):
        _reject("declared_placeholder_unused")
    if len(raw) + sum(len(replacement) - (end - start) for start, end, replacement in edits) > limits.part_bytes:
        _reject("output_part_size")
    # Assemble once: a placeholder split over many runs must not induce a
    # quadratic series of copies of the whole XML document.
    pieces = []
    position = 0
    for start, end, replacement in sorted(edits):
        if start < position or end < start:
            _reject("text_spans_overlap")
        pieces.extend((raw[position:start], replacement))
        position = end
    pieces.append(raw[position:])
    output = b"".join(pieces)
    content_map.sort(key=lambda item: (item["paragraph"], item["source_char_start"]))
    return output, content_map


def fill_docx_template(
    template_bytes: bytes, *, template_sha256: str, placeholder_allowlist: Sequence[str],
    values: Mapping[str, str], approved_content_revision: str, approved_content_sha256: str,
    protected_parts: Sequence[str], limits: TemplateFillLimits | None = None,
) -> dict:
    """Return a new bytes artifact, hashes, content mapping and structural QA.

    ``template_content_hash`` defines the confirmation hash scheme. References
    are caller supplied, never independently approved here. Each declared key
    must occur, every occurrence is replaced, and leftovers fail closed. Text
    uses the first occupied run's formatting; remaining runs keep properties.
    No template path is accepted and no original file can be overwritten.
    """
    result = {"status": "failed", "errors": [], "visual_status": "unverified",
              "word_wps_status": "unverified", "semantic_status": "requires_human_review",
              "approval_status": "caller_supplied_reference_only", "delivery_status": "not_sent",
              "fidelity_scope": "plain body/table w:t text; run properties and other part bytes preserved",
              "limitations": ["no header/footer/textbox/field/hyperlink placeholders", "no media, preview thumbnails or embedded content",
                              "replacement adopts first occupied run formatting", "no renderer or Word/WPS validation"]}
    try:
        limits = _valid_limits(limits)
        _, values = _content_spec(placeholder_allowlist, values, approved_content_revision, limits)
        if (not isinstance(template_bytes, bytes) or not template_bytes or len(template_bytes) > limits.input_bytes):
            _reject("input_size_or_type")
        if (not isinstance(template_sha256, str) or not _HASH.fullmatch(template_sha256)
                or sha256(template_bytes).hexdigest() != template_sha256.lower()):
            _reject("source_template_hash_mismatch")
        if (not isinstance(approved_content_sha256, str) or not _HASH.fullmatch(approved_content_sha256)
                or template_content_hash(placeholder_allowlist, values, revision_id=approved_content_revision)
                != approved_content_sha256.lower()):
            _reject("confirmed_content_hash_mismatch")
        if (not isinstance(protected_parts, (list, tuple, set, frozenset)) or len(protected_parts) > limits.entries
                or any(not _part_name(name) for name in protected_parts)
                or len(set(protected_parts)) != len(protected_parts)):
            _reject("protected_parts_invalid")
        entries, parts, comment = _load_parts(template_bytes, limits)
        if not set(protected_parts).issubset(parts):
            _reject("protected_part_missing")
        protected_hashes = {name: sha256(parts[name]).hexdigest() for name in protected_parts}
        original_hashes = {name: sha256(part).hexdigest() for name, part in parts.items()}
        budget = _Budget(limits)
        part_names = set(parts)
        paragraphs = []
        for name, raw in parts.items():
            text, current = _scan_xml(raw, name, budget, part_names)
            if name == MAIN_PART:
                paragraphs = current
            elif "{{" in text or "}}" in text:
                _reject("placeholder_location_unsupported")
        filled, content_map = _fill_main(parts[MAIN_PART], paragraphs, values, approved_content_revision, limits)
        if MAIN_PART in protected_hashes and sha256(filled).hexdigest() != protected_hashes[MAIN_PART]:
            _reject("protected_part_would_change")
        parts[MAIN_PART] = filled
        # Recheck all XML after substitution: detect tokens assembled across
        # uneditable locations and enforce output node/text/expanded budgets.
        if sum(len(part) for part in parts.values()) > limits.expanded_bytes:
            _reject("output_expanded_size")
        output_budget = _Budget(limits)
        for name, raw in parts.items():
            text, _ = _scan_xml(raw, name, output_budget, part_names)
            if "{{" in text or "}}" in text:
                _reject("unresolved_placeholder")
        buffer = BytesIO()
        with ZipFile(buffer, "w") as output:
            output.comment = comment
            for entry in entries:
                output.writestr(entry, parts[entry.filename])
        document_bytes = buffer.getvalue()
        if len(document_bytes) > limits.output_bytes:
            _reject("output_size")
        with ZipFile(BytesIO(document_bytes)) as verify:
            part_hashes = {name: {"before": original_hashes[name],
                                  "after": sha256(verify.read(name)).hexdigest()}
                           for name in parts}
        if any(item["before"] != item["after"] for name, item in part_hashes.items() if name != MAIN_PART):
            _reject("unmodified_part_changed")
        report = check_docx(document_bytes, placeholder_markers=("{{", "}}"), protected_parts=protected_hashes)
        result["check_report"] = report
        if report.get("status") != "success":
            _reject("filled_document_checks_failed")
        # ZIP containers are rebuilt; the promise concerns uncompressed part
        # bytes, explicitly not the compressed ZIP/container identity.
        preserved = {name: original_hashes[name] for name in parts if name != MAIN_PART}
        result.update({"status": "success", "document_bytes": document_bytes,
                       "content_hash": sha256(document_bytes).hexdigest(), "size_bytes": len(document_bytes),
                       "source_template_hash": template_sha256.lower(),
                       "approved_content": {"revision_id": approved_content_revision,
                                            "content_hash": approved_content_sha256.lower()},
                       "content_map": content_map, "protected_part_hashes": protected_hashes,
                       "preserved_part_hashes": preserved, "part_hashes": part_hashes})
    except TemplateFillRejected as error:
        result["errors"] = [str(error)]
    except (BadZipFile, expat.ExpatError, ValueError, TypeError, KeyError, OSError, RuntimeError):
        result["errors"] = ["invalid_or_unsupported_template"]
    return result
