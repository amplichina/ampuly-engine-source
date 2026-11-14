"""Bounded, bytes-only material extraction. Authorization belongs to the caller.

``offset``/``next_offset`` are Unicode-character offsets in the concatenation of
nonempty structural-block texts. A large block is returned in fragments carrying
its original location and block-relative span; continuation never discards text.
The output is untrusted material, not executable instructions or authorization.
Limits bound inputs/expansion/structures; the host must also enforce process CPU,
memory and wall-clock limits for third-party parsers. No path or network is used.
"""

from __future__ import annotations

import hashlib
import io
import inspect
import json
import ntpath
import re
import zipfile
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

PARSER_VERSION = "1.0.1"
LIMITS = {
    "input_bytes": 16 * 1024 * 1024,
    "zip_entries": 512,
    "zip_expanded_bytes": 32 * 1024 * 1024,
    "zip_part_bytes": 8 * 1024 * 1024,
    "zip_compression_ratio": 200,
    "xml_elements": 200_000,
    "parsed_blocks": 20_000,
    "parsed_text_chars": 1_000_000,
    "sheets": 32,
    "sheet_rows": 20_000,
    "sheet_columns": 512,
    "sheet_grid_cells": 250_000,
    "pdf_pages": 200,
    "pdf_stream_bytes": 4 * 1024 * 1024,
    "response_blocks": 1000,
    "response_chars": 200_000,
    "warnings": 1000,
    "warning_chars": 200_000,
}
_SOURCE_KEYS = ("artifact_id", "version", "content_hash", "project_id")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class _Rejected(Exception):
    def __init__(self, code: str, status: str = "failed") -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class _Parsed:
    blocks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    libraries: dict[str, str] = field(default_factory=dict)
    complete: bool = True
    chars: int = 0
    warning_chars: int = 0
    _warning_keys: set[str] = field(default_factory=set, repr=False)

    def warn(self, code: str, *, incomplete: bool = False, **detail: Any) -> None:
        item = {"code": code, **detail}
        # Internal warning details are small JSON primitives/locations. Canonical
        # keys avoid repeated deep scans of a growing list while bounding both
        # the number of findings and their total returned text.
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if key not in self._warning_keys:
            if len(self.warnings) >= LIMITS["warnings"]:
                raise _Rejected("warning_count_limit")
            if self.warning_chars + len(key) > LIMITS["warning_chars"]:
                raise _Rejected("warning_size_limit")
            self.warnings.append(item)
            self._warning_keys.add(key)
            self.warning_chars += len(key)
        if incomplete:
            self.complete = False

    def add(self, kind: str, text: str, location: dict[str, Any], **detail: Any) -> None:
        if not text:
            return
        if len(self.blocks) >= LIMITS["parsed_blocks"] or self.chars + len(text) > LIMITS["parsed_text_chars"]:
            raise _Rejected("parsed_content_limit")
        self.blocks.append({"kind": kind, "text": text, "location": location, **detail})
        self.chars += len(text)

    def library(self, name: str) -> None:
        try:
            self.libraries[name] = version(name)
        except PackageNotFoundError:
            self.libraries[name] = "unknown"


def _sheet_coordinate(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Z]+)\$?([1-9][0-9]*)", value)
    if match is None or len(match[1]) > 3 or len(match[2]) > 7:
        raise _Rejected("invalid_sheet_coordinate")
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - ord("A") + 1
    row = int(match[2])
    if row > LIMITS["sheet_rows"] or column > LIMITS["sheet_columns"]:
        raise _Rejected("sheet_dimension_limit")
    return row, column


def _check_sheet_dimensions(root: Any) -> None:
    """Do not trust OOXML dimension hints to bound or describe actual cells."""
    dimension = root.find(_S + "dimension")
    reference = dimension.get("ref", "") if dimension is not None else ""
    bounds = reference.split(":")
    if not reference or len(bounds) > 2:
        raise _Rejected("sheet_dimensions_unavailable")
    first = _sheet_coordinate(bounds[0])
    last = _sheet_coordinate(bounds[-1])
    if first[0] > last[0] or first[1] > last[1] or last[0] * last[1] > LIMITS["sheet_grid_cells"]:
        raise _Rejected("sheet_dimension_limit")
    seen: set[tuple[int, int]] = set()
    for cell in root.iter(_S + "c"):
        coordinate = _sheet_coordinate(cell.get("r", ""))
        if coordinate in seen or not (first[0] <= coordinate[0] <= last[0] and first[1] <= coordinate[1] <= last[1]):
            raise _Rejected("sheet_dimension_or_coordinate_mismatch")
        seen.add(coordinate)
    # read_only parsing groups cells by the physical row; reject inconsistent
    # row declarations as well as misleading outer dimensions.
    for row in root.iter(_S + "row"):
        declared = row.get("r", "")
        if not re.fullmatch(r"[1-9][0-9]{0,6}", declared) or int(declared) > LIMITS["sheet_rows"]:
            raise _Rejected("invalid_sheet_row")
        if any(_sheet_coordinate(cell.get("r", ""))[0] != int(declared) for cell in row.findall(_S + "c")):
            raise _Rejected("sheet_row_coordinate_mismatch")


def _bounded_xml_root(data: bytes, remaining: int):
    """Count in bounded pull-parser chunks before constructing the final DOM.

    Completed preflight nodes are cleared and detached; empty siblings cannot
    accumulate to the full XML size. The same safe parser options are applied
    again only after the package-wide node allowance has been verified.
    """
    from lxml import etree

    parser = etree.XMLPullParser(events=("start", "end", "comment", "pi"),
                                 resolve_entities=False, load_dtd=False,
                                 no_network=True, huge_tree=False)
    count = 0
    for offset in range(0, len(data), 32_768):
        parser.feed(data[offset:offset + 32_768])
        for event, element in parser.read_events():
            if event != "end":
                count += 1
                if count > remaining:
                    raise _Rejected("xml_element_limit")
            if event in {"end", "comment", "pi"}:
                element.clear()
                parent = element.getparent()
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]
    preflight_root = parser.close()
    if preflight_root.getroottree().docinfo.doctype:
        raise _Rejected("xml_dtd_or_entity")
    safe = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)
    return etree.fromstring(data, safe), count


def _zip_preflight(raw: bytes, parsed: _Parsed) -> None:
    from lxml import etree

    parsed.library("lxml")
    elements = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > LIMITS["zip_entries"]:
                raise _Rejected("zip_entry_limit")
            if sum(entry.file_size for entry in entries) > LIMITS["zip_expanded_bytes"]:
                raise _Rejected("zip_expansion_limit")
            names: set[str] = set()
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                lower = name.lower()
                if name.startswith("/") or ":" in name or ".." in name.split("/") or lower in names:
                    raise _Rejected("unsafe_zip_member")
                names.add(lower)
                if entry.flag_bits & 1:
                    raise _Rejected("encrypted_zip", "unsupported")
                if entry.file_size > LIMITS["zip_part_bytes"]:
                    raise _Rejected("zip_part_limit")
                if entry.file_size > max(1, entry.compress_size) * LIMITS["zip_compression_ratio"]:
                    raise _Rejected("zip_compression_ratio_limit")
                if "vbaproject" in lower or "/embeddings/" in lower:
                    raise _Rejected("macro_or_embedded_active_content", "unsupported")
                if not lower.endswith((".xml", ".rels")):
                    continue
                data = archive.read(entry)
                # Also recognizes UTF-16/32 ASCII markup, before library parsing.
                if re.search(br"<\s*!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\0", b""), re.I):
                    raise _Rejected("xml_dtd_or_entity")
                root, count = _bounded_xml_root(data, LIMITS["xml_elements"] - elements)
                elements += count
                for element in root.iter():
                    if element.get("TargetMode") == "External":
                        parsed.warn("external_relationship_not_followed", part=name)
                if root.tag == _S + "worksheet":
                    _check_sheet_dimensions(root)
                if b"macroenabled" in data.lower():
                    raise _Rejected("macro_content_type", "unsupported")
                if "/externallinks/" in lower:
                    parsed.warn("external_workbook_links_not_resolved", part=name)
    except zipfile.BadZipFile as exc:
        raise _Rejected("invalid_zip") from exc


def _word_text(element: Any, parsed: _Parsed, location: dict[str, Any]) -> str:
    skipped = {_W + name for name in ("drawing", "pict", "object", "txbxContent", "sdt", "altChunk", "del")}
    bits: list[str] = []

    def walk(node: Any) -> None:
        if node.tag in skipped:
            parsed.warn("docx_substructure_omitted", incomplete=True, structure=node.tag.rsplit("}", 1)[-1], location=location)
            return
        if isinstance(node.tag, str) and "officeDocument/2006/math" in node.tag:
            parsed.warn("docx_math_omitted", incomplete=True, location=location)
            return
        if node.tag == _W + "t":
            bits.append(node.text or "")
        elif node.tag == _W + "tab":
            bits.append("\t")
        elif node.tag in {_W + "br", _W + "cr"}:
            bits.append("\n")
        elif node.tag in {_W + "fldSimple", _W + "instrText"}:
            parsed.warn("docx_fields_not_evaluated")
        for child in node:
            walk(child)

    walk(element)
    return "".join(bits)


def _word_container(container: Any, parsed: _Parsed, base: dict[str, Any]) -> None:
    paragraph = table = 0
    for element in container:
        if element.tag == _W + "p":
            paragraph += 1
            location = {**base, "paragraph": paragraph}
            parsed.add("paragraph", _word_text(element, parsed, location), location)
        elif element.tag == _W + "tbl":
            table += 1
            for row_index, row in enumerate(element.findall(_W + "tr"), 1):
                before = row.find(_W + "trPr/" + _W + "gridBefore")
                column = int(before.get(_W + "val", "0")) + 1 if before is not None else 1
                if column < 1 or column > LIMITS["sheet_columns"]:
                    raise _Rejected("docx_table_geometry_limit")
                for cell in row.findall(_W + "tc"):
                    span = cell.find(_W + "tcPr/" + _W + "gridSpan")
                    width = int(span.get(_W + "val", "1")) if span is not None else 1
                    if width < 1 or column + width - 1 > LIMITS["sheet_columns"]:
                        raise _Rejected("docx_table_geometry_limit")
                    location = {**base, "table": table, "row": row_index, "column": column, "column_span": width}
                    texts: list[str] = []
                    if cell.find(_W + "tcPr/" + _W + "vMerge") is not None:
                        parsed.warn("docx_vertical_merge_geometry_unverified", incomplete=True, location=location)
                    for child in cell:
                        if child.tag == _W + "p":
                            texts.append(_word_text(child, parsed, location))
                        elif child.tag != _W + "tcPr":
                            parsed.warn("docx_cell_substructure_omitted", incomplete=True, location=location)
                    parsed.add("table_cell", "\n".join(texts), location)
                    column += max(1, width)
        elif element.tag != _W + "sectPr":
            parsed.warn("docx_container_substructure_omitted", incomplete=True, location=base)


def _docx(raw: bytes, parsed: _Parsed) -> None:
    from docx import Document

    parsed.library("python-docx")
    document = Document(io.BytesIO(raw))
    _word_container(document.element.body, parsed, {"region": "body", "part": "word/document.xml"})
    seen: set[str] = set()
    for relation in document.part.rels.values():
        if relation.is_external:
            continue
        region = relation.reltype.rsplit("/", 1)[-1]
        if region in {"header", "footer"}:
            part = relation.target_part
            name = str(part.partname).lstrip("/")
            if name not in seen:
                seen.add(name)
                _word_container(part.element, parsed, {"region": region, "part": name})
        elif region in {"footnotes", "endnotes", "comments"}:
            parsed.warn("docx_auxiliary_text_omitted", incomplete=True, structure=region)
    parsed.warn("docx_page_positions_unavailable")
    parsed.coverage["scope"] = "body/header/footer paragraphs and top-level table cells; not rendered layout"


def _xlsx(raw: bytes, parsed: _Parsed) -> None:
    from openpyxl import load_workbook

    parsed.library("openpyxl")
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False, keep_vba=False)
    try:
        if len(workbook.worksheets) > LIMITS["sheets"]:
            raise _Rejected("sheet_count_limit")
        parsed.coverage["sheets"] = []
        for sheet in workbook.worksheets:
            rows, columns = sheet.max_row or 0, sheet.max_column or 0
            if rows > LIMITS["sheet_rows"] or columns > LIMITS["sheet_columns"] or rows * columns > LIMITS["sheet_grid_cells"]:
                raise _Rejected("sheet_dimension_limit")
            # A missing dimension is not a permit for an unbounded row iterator.
            if not rows or not columns:
                raise _Rejected("sheet_dimensions_unavailable")
            parsed.coverage["sheets"].append({"sheet": sheet.title, "rows": rows, "columns": columns})
            for row in sheet.iter_rows(min_row=1, max_row=rows, min_col=1, max_col=columns):
                for cell in row:
                    if cell.value is None:
                        continue
                    formula = str(cell.value) if cell.data_type == "f" else None
                    parsed.add("cell", str(cell.value), {"sheet": sheet.title, "cell": cell.coordinate}, formula=formula, data_type=cell.data_type)
                    if formula is not None:
                        parsed.warn("formulas_not_evaluated")
        # Workbook drawings/comments are outside this parser's declared scope.
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if any("/drawings/" in item or "/comments" in item for item in archive.namelist()):
                parsed.warn("xlsx_drawings_or_comments_omitted", incomplete=True)
        parsed.coverage["scope"] = "stored cell values and formula expressions; no calculation or layout"
    finally:
        workbook.close()


def _pdf_configuration_context(pypdf):
    """Probe supported public configuration and reader features before PDF I/O.

    Configuration is built without changing the active execution context. A
    library lacking any required cap is unsupported, not a malformed PDF and
    never a reason to silently fall back to unbounded parsing.
    """
    cap = LIMITS["pdf_stream_bytes"]
    options = dict(maximum_declared_stream_length=cap, array_based_stream_maximum_output_length=cap,
                   zlib_maximum_output_length=cap, lzw_maximum_output_length=cap,
                   run_length_maximum_output_length=cap, image_maximum_buffer_size=cap,
                   zlib_maximum_recovery_input_length=cap, xmp_maximum_input_length=cap,
                   page_tree_maximum_entries=1000, page_tree_maximum_depth=50,
                   xform_maximum_invocations_per_extraction=100, jbig2dec_binary=None,
                   disable_legacy_handling=True)
    try:
        if not callable(pypdf.apply_configuration) or not callable(pypdf.get_configuration):
            raise TypeError("bounded configuration APIs unavailable")
        inspect.signature(pypdf.PdfReader).bind(io.BytesIO(b""), strict=True, root_object_recovery_limit=1000)
        configuration = pypdf.get_configuration().with_overwrites(**options)
        if any(not hasattr(configuration, key) or getattr(configuration, key) != value for key, value in options.items()):
            raise TypeError("bounded configuration was not applied")
        inspect.signature(pypdf.apply_configuration).bind(configuration=configuration)
        return pypdf.apply_configuration(configuration=configuration), options
    except (AttributeError, TypeError, ValueError) as exc:
        raise _Rejected("pdf_bounded_configuration_unavailable", "unsupported") from exc


def _pdf(raw: bytes, parsed: _Parsed) -> None:
    import pypdf

    parsed.library("pypdf")
    with ExitStack() as stack:
        try:
            context, options = _pdf_configuration_context(pypdf)
            stack.enter_context(context)
            actual = pypdf.get_configuration()
            if any(not hasattr(actual, key) or getattr(actual, key) != value for key, value in options.items()):
                raise TypeError("active bounded configuration was not applied")
        except (AttributeError, TypeError, ValueError) as exc:
            raise _Rejected("pdf_bounded_configuration_unavailable", "unsupported") from exc
        reader = pypdf.PdfReader(io.BytesIO(raw), strict=True, root_object_recovery_limit=1000)
        if reader.is_encrypted:
            raise _Rejected("encrypted_pdf", "unsupported")
        if len(reader.pages) > LIMITS["pdf_pages"]:
            raise _Rejected("pdf_page_limit")
        parsed.coverage.update(pages_total=len(reader.pages), pages_read=[], pages_failed=[], pages_without_text=[])
        root = reader.trailer["/Root"]
        if any(key in root for key in ("/OpenAction", "/AA", "/AcroForm", "/Names")):
            parsed.warn("pdf_active_or_auxiliary_content_not_executed", incomplete=True)
        for index in range(len(reader.pages)):
            page_number = index + 1
            try:
                page = reader.pages[index]
                # DictionaryObject.__getitem__ resolves indirect objects; .get
                # does not, and would wrongly count a normal page as failed.
                resources = page["/Resources"] if "/Resources" in page else {}
                xobjects = resources["/XObject"] if "/XObject" in resources else {}
                if xobjects:
                    parsed.warn("pdf_xobject_content_coverage_unverified", incomplete=True, page=page_number)
                text = page.extract_text()
            except Exception as exc:
                parsed.coverage["pages_failed"].append({"page": page_number, "error_type": type(exc).__name__})
                parsed.warn("pdf_page_extraction_failed", incomplete=True, page=page_number)
                continue
            if not text or not text.strip():
                parsed.coverage["pages_without_text"].append(page_number)
                parsed.warn("pdf_page_has_no_extractable_text", incomplete=True, page=page_number)
                continue
            parsed.add("page_text", text, {"page": page_number})
            parsed.coverage["pages_read"].append(page_number)
        parsed.coverage["scope"] = "extractable page text, page location only; no OCR/layout/annotation guarantees"
        if not parsed.blocks:
            raise _Rejected("pdf_no_extractable_text", "unsupported" if not parsed.coverage["pages_failed"] else "failed")


def _text(raw: bytes, parsed: _Parsed) -> None:
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = raw.decode(encoding, errors="strict")
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        parsed.add("line", line, {"line": line_number})
    parsed.coverage["scope"] = "decoded text lines, including line endings"
    parsed.coverage["encoding"] = encoding


def _paginate(parsed: _Parsed, offset: int, max_blocks: int, max_chars: int) -> dict[str, Any]:
    if offset > parsed.chars:
        raise _Rejected("offset_out_of_range")
    returned: list[dict[str, Any]] = []
    absolute = 0
    cursor = offset
    remaining = max_chars
    for block_index, block in enumerate(parsed.blocks):
        end = absolute + len(block["text"])
        if end <= cursor:
            absolute = end
            continue
        if not remaining or len(returned) >= max_blocks:
            break
        start_in_block = max(0, cursor - absolute)
        take = min(len(block["text"]) - start_in_block, remaining)
        returned.append({**block, "text": block["text"][start_in_block:start_in_block + take],
                         "block_index": block_index, "text_start": start_in_block,
                         "text_end": start_in_block + take, "block_text_chars": len(block["text"]),
                         "continued": start_in_block > 0 or start_in_block + take < len(block["text"])})
        cursor = absolute + start_in_block + take
        remaining -= take
        absolute = end
    exhausted = cursor == parsed.chars
    whole = offset == 0 and exhausted
    return {
        "status": "success" if parsed.complete and whole else "partial",
        "blocks": returned,
        "next_offset": None if exhausted else cursor,
        "coverage": {**parsed.coverage, "parsed_text_complete": parsed.complete,
                     "parsed_blocks": len(parsed.blocks), "parsed_text_chars": parsed.chars,
                     "returned_start": offset, "returned_end": cursor,
                     "returned_entire_text": whole, "full_document": parsed.complete and whole},
    }


def read_material(raw: bytes, filename: str, *, source: Mapping[str, str], offset: int = 0,
                  max_blocks: int = 100, max_chars: int = 40000) -> dict[str, Any]:
    """Extract authorized bytes; does not open paths or verify access rights.

    All blocks/metadata are untrusted. ``full_document`` means complete extraction
    in the declared coverage scope, not validation of facts or rendered layout.
    The caller must keep source identity and hash fixed between cursor requests.
    """
    parsed = _Parsed()
    result: dict[str, Any] = {
        "status": "failed", "source": {}, "filename": "", "blocks": [], "next_offset": None,
        "warnings": [], "coverage": {"full_document": False, "parsed_text_complete": False},
        "parser": {"name": "ampuly.read_material", "version": PARSER_VERSION, "libraries": parsed.libraries},
        "limits": dict(LIMITS), "trust": "untrusted_material", "authorization_checked": False,
    }
    try:
        if not isinstance(source, Mapping) or any(not isinstance(source.get(key), str) or not source[key].strip() or len(source[key]) > 512 for key in _SOURCE_KEYS):
            raise _Rejected("invalid_source")
        result["source"] = {key: source[key] for key in _SOURCE_KEYS}
        if not isinstance(raw, bytes) or not isinstance(filename, str) or not filename or len(filename) > 512:
            raise _Rejected("invalid_input")
        result["filename"] = ntpath.basename(filename)
        if len(raw) > LIMITS["input_bytes"]:
            raise _Rejected("input_size_limit")
        expected = source["content_hash"].lower()
        if expected.startswith("sha256:"):
            expected = expected[7:]
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(raw).hexdigest() != expected:
            raise _Rejected("content_hash_mismatch")
        for value, lower, upper in ((offset, 0, LIMITS["parsed_text_chars"]),
                                    (max_blocks, 1, LIMITS["response_blocks"]),
                                    (max_chars, 1, LIMITS["response_chars"])):
            if type(value) is not int or not lower <= value <= upper:
                raise _Rejected("invalid_pagination")
        suffix = filename.rsplit(".", 1)[-1].lower()
        if suffix in {"docm", "xlsm", "xltm", "pptm"}:
            raise _Rejected("macro_format", "unsupported")
        if suffix in {"docx", "xlsx"}:
            _zip_preflight(raw, parsed)
        if suffix == "docx":
            _docx(raw, parsed)
        elif suffix == "xlsx":
            _xlsx(raw, parsed)
        elif suffix == "pdf":
            _pdf(raw, parsed)
        elif suffix in {"txt", "md", "markdown"}:
            _text(raw, parsed)
        else:
            raise _Rejected("format_not_supported", "unsupported")
        result.update(_paginate(parsed, offset, max_blocks, max_chars))
    except _Rejected as exc:
        result["status"] = exc.status
        result["error"] = {"code": exc.code}
        result["coverage"] = {**parsed.coverage, "full_document": False, "parsed_text_complete": False}
    except ImportError as exc:
        result["status"] = "unsupported"
        result["error"] = {"code": "parser_library_unavailable", "error_type": type(exc).__name__}
    except Exception as exc:
        # Avoid echoing parser messages that can contain source text or paths.
        result["error"] = {"code": "parse_failed", "error_type": type(exc).__name__}
    result["warnings"] = parsed.warnings
    return result
