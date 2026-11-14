"""Additional bounded DOCX text reader: nested tables and annotation stories.

Preserves precise XML paths, not rendered page coordinates. Never follows links,
evaluates fields, grants access or treats comments as approved business facts.
Use as an additional tool; the existing general reader remains unchanged.
"""
from io import BytesIO
from zipfile import ZipFile
from collections.abc import Mapping
import hashlib
import posixpath
import re
from urllib.parse import unquote

from .read_material import _Parsed, _Rejected, _zip_preflight, _word_text, _paginate, LIMITS, _W

VERSION = "0.1"
MAX_DEPTH = 16


class _BoundedParsed(_Parsed):
    def warn(self, code, *, incomplete=False, **detail):
        if len(self.warnings) >= 1000:
            raise _Rejected("warning_count_limit")
        super().warn(code, incomplete=incomplete, **detail)


def read_docx_structure(raw, *, source, offset=0, max_blocks=100, max_chars=40000):
    parsed = _BoundedParsed()
    result = {"status": "failed", "source": {}, "blocks": [], "warnings": [], "next_offset": None,
              "coverage": {"full_document": False}, "parser": {"name": "ampuly.read_docx_structure", "version": VERSION},
              "authorization_checked": False, "trust": "untrusted_material", "visual_status": "unverified"}
    try:
        if not isinstance(source, Mapping) or any(not isinstance(source.get(k), str) or not source[k] or len(source[k]) > 512
                for k in ("artifact_id", "version", "content_hash", "project_id")):
            raise _Rejected("invalid_source")
        result["source"] = {k: source[k] for k in ("artifact_id", "version", "content_hash", "project_id")}
        expected = source["content_hash"].removeprefix("sha256:").lower()
        if not isinstance(raw, bytes) or len(raw) > LIMITS["input_bytes"]:
            raise _Rejected("invalid_input_or_size")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(raw).hexdigest() != expected:
            raise _Rejected("content_hash_mismatch")
        for value, minimum, maximum in ((offset, 0, LIMITS["parsed_text_chars"]),
                (max_blocks, 1, LIMITS["response_blocks"]), (max_chars, 1, LIMITS["response_chars"])):
            if type(value) is not int or not minimum <= value <= maximum:
                raise _Rejected("invalid_pagination")
        _zip_preflight(raw, parsed)
        from lxml import etree

        def parse(data):
            return etree.fromstring(data, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False))

        def walk(container, base, path, depth=0):
            if depth > MAX_DEPTH:
                raise _Rejected("docx_nesting_limit")
            paragraph_no, table_no = 0, 0
            for position, element in enumerate((e for e in container if isinstance(e.tag, str)), 1):
                tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else "comment"
                here = path + f"/*[{position}]"
                if element.tag == _W + "p":
                    paragraph_no += 1
                    location = {**base, "xml_path": here, "paragraph": paragraph_no}
                    links = [dict(kind=node.tag.rsplit("}", 1)[-1], id=node.get(_W+"id"))
                             for node in element.iter() if node.tag in {_W+"footnoteReference", _W+"endnoteReference", _W+"commentReference"}]
                    if links:
                        location["annotation_refs"] = links
                    parsed.add("paragraph", _word_text(element, parsed, location), location)
                elif element.tag == _W + "tbl":
                    table_no += 1
                    row_no = 0
                    for row_pos, row in enumerate((e for e in element if isinstance(e.tag, str)), 1):
                        if row.tag != _W+"tr":
                            continue
                        row_no += 1
                        before = row.find(_W+"trPr/"+_W+"gridBefore")
                        col = int(before.get(_W+"val", "0")) + 1 if before is not None else 1
                        for cell_pos, cell in enumerate((e for e in row if isinstance(e.tag, str)), 1):
                            if cell.tag != _W+"tc":
                                continue
                            span_node = cell.find(_W+"tcPr/"+_W+"gridSpan")
                            span = int(span_node.get(_W+"val", "1")) if span_node is not None else 1
                            if col < 1 or span < 1 or col+span-1 > LIMITS["sheet_columns"]:
                                raise _Rejected("docx_table_geometry_limit")
                            merge = cell.find(_W+"tcPr/"+_W+"vMerge")
                            cellbase = {**base, "table": table_no, "row": row_no, "column": col, "column_span": span}
                            if merge is not None:
                                cellbase["vertical_merge"] = merge.get(_W+"val", "continue")
                            walk(cell, cellbase, here+f"/*[{row_pos}]/*[{cell_pos}]", depth+1)
                            col += span
                elif element.tag not in {_W+"sectPr", _W+"tcPr"}:
                    parsed.warn("docx_structure_not_extracted", incomplete=True, structure=tag, location={**base, "xml_path": here})

        with ZipFile(BytesIO(raw)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                raise _Rejected("not_docx")
            stories = {"word/document.xml": "document"}
            for name in names:
                if re.fullmatch(r"word/(?:header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name):
                    stories[name] = "hdr" if "header" in name else "ftr" if "footer" in name else name.split("/")[-1].split(".")[0]
            relations_part = "word/_rels/document.xml.rels"
            if relations_part in names:
                relation_root = parse(archive.read(relations_part))
                for relation in relation_root:
                    kind = relation.get("Type", "").rsplit("/", 1)[-1]
                    if kind not in {"header", "footer", "footnotes", "endnotes", "comments"}:
                        continue
                    target = unquote(relation.get("Target", ""), errors="strict")
                    resolved = posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join("word", target))
                    if relation.get("TargetMode", "Internal") != "Internal" or not target or any(c in target for c in "\\\x00:#?") or resolved not in names:
                        parsed.warn("docx_story_relationship_unavailable", incomplete=True, story=kind)
                        continue
                    expected = "hdr" if kind == "header" else "ftr" if kind == "footer" else kind
                    if resolved in stories and stories[resolved] != expected:
                        raise _Rejected("conflicting_story_relationship")
                    stories[resolved] = expected
            parts = ["word/document.xml"] + sorted(name for name in stories if name != "word/document.xml")
            for part in parts:
                root = parse(archive.read(part))
                expected_root = stories[part]
                if root.tag != _W+expected_root:
                    raise _Rejected("invalid_word_story_root")
                # Visible cached values can be read, but hidden/revised text and
                # evaluated fields must not silently count as complete coverage.
                for element in root.iter():
                    if element.tag in {_W+n for n in ("ins", "del", "moveFrom", "moveTo", "fldSimple", "fldChar", "instrText")}:
                        parsed.warn("docx_revision_or_field_unverified", incomplete=True, part=part)
                        break
                if part == "word/document.xml":
                    body = root.find(_W+"body")
                    if body is None:
                        raise _Rejected("docx_body_missing")
                    body_position = next(i for i, child in enumerate((e for e in root if isinstance(e.tag, str)), 1) if child is body)
                    walk(body, {"part": part, "story": "body"}, f"/*[1]/*[{body_position}]")
                elif expected_root in {"footnotes", "endnotes", "comments"}:
                    story = expected_root
                    for position, item in enumerate((e for e in root if isinstance(e.tag, str)), 1):
                        identifier = item.get(_W+"id", "")
                        if story != "comments" and item.get(_W+"type") in {"separator", "continuationSeparator", "continuationNotice"}:
                            continue
                        walk(item, {"part": part, "story": story, "annotation_id": identifier}, f"/*[1]/*[{position}]")
                else:
                    walk(root, {"part": part, "story": "header" if expected_root == "hdr" else "footer"}, "/*[1]")
            parsed.coverage["scope"] = "stored Word text and nested tables in body/header/footer/footnotes/endnotes/comments; XML paths, no page/layout guarantee"
            parsed.coverage["parts_read"] = parts
            parsed.coverage["annotation_policy"] = "separate untrusted stories; not approved facts"
            parsed.coverage["xml_path_format"] = "absolute XPath with element child positions, independent of namespace prefixes"
            result.update(_paginate(parsed, offset, max_blocks, max_chars))
    except _Rejected as exc:
        result["status"] = exc.status
        result["error"] = {"code": exc.code}
    except Exception as exc:
        result["error"] = {"code": "parse_failed", "error_type": type(exc).__name__}
    result["warnings"] = parsed.warnings
    if "error" in result:
        result["coverage"] = {**parsed.coverage, "full_document": False}
    return result
