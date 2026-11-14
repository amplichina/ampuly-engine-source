"""Produce DOCX bytes and exclusively create a new file in an approved directory.

No model, renderer, network access or delivery occurs here. The caller supplies
confirmed content and resolves authorization before invoking this primitive.
"""

from __future__ import annotations

import os
import re
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from typing import Mapping

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from .check_docx import MAX_INPUT_BYTES, check_docx


def generate_docx(spec: Mapping, *, approved_content: Mapping[str, str]) -> dict:
    """Generate a new document from a bounded, structured content specification.

    Templates, embedded media/macros, raw XML and HTML are unsupported in this
    first primitive. A reference is recorded, not independently approved here.
    """
    result = {
        "status": "failed", "generator": {"name": "ampuly_docx", "version": "0.1", "python-docx": version("python-docx")},
        "visual_status": "unverified", "delivery_status": "not_sent", "errors": [],
    }
    if not isinstance(spec, Mapping) or not isinstance(approved_content, Mapping):
        result["errors"] = ["invalid_input"]
        return result
    if not all(isinstance(approved_content.get(key), str) and approved_content[key] and len(approved_content[key]) <= 512 for key in ("revision_id", "content_hash")):
        result["errors"] = ["confirmed_content_reference_required"]
        return result
    if not re.fullmatch(r"[a-fA-F0-9]{64}", approved_content["content_hash"]):
        result["errors"] = ["confirmed_content_hash_invalid"]
        return result
    if any(key in spec for key in ("template", "template_path", "raw_xml", "html", "images", "attachments")):
        result["status"] = "unsupported"
        result["errors"] = ["template_or_embedded_content_not_supported"]
        return result
    title = spec.get("title")
    sections = spec.get("sections")
    if not isinstance(title, str) or not title.strip() or len(title) > 300 or not isinstance(sections, list) or not 1 <= len(sections) <= 50:
        result["errors"] = ["title_or_sections_invalid"]
        return result
    total_chars = len(title)
    total_paragraphs = total_tables = total_cells = 0
    content_map = []
    try:
        for section in sections:
            if not isinstance(section, Mapping) or not isinstance(section.get("heading"), str) or not section["heading"].strip():
                raise ValueError("section_invalid")
            total_chars += len(section["heading"])
            paragraphs = section.get("paragraphs", [])
            tables = section.get("tables", [])
            if not isinstance(paragraphs, list) or len(paragraphs) > 500 or any(not isinstance(text, str) for text in paragraphs):
                raise ValueError("paragraphs_invalid")
            if not isinstance(tables, list) or len(tables) > 20:
                raise ValueError("tables_invalid")
            total_chars += sum(len(text) for text in paragraphs)
            total_paragraphs += len(paragraphs) + 1
            total_tables += len(tables)
            if total_paragraphs > 1000 or total_tables > 40:
                raise ValueError("structure_size")
            for table in tables:
                headers = table.get("headers") if isinstance(table, Mapping) else None
                rows = table.get("rows") if isinstance(table, Mapping) else None
                if not isinstance(headers, list) or not 1 <= len(headers) <= 20 or any(not isinstance(cell, str) for cell in headers):
                    raise ValueError("table_headers_invalid")
                if not isinstance(rows, list) or len(rows) > 500:
                    raise ValueError("table_rows_invalid")
                # Empty strings still create costly OOXML nodes. Bound the
                # entire document before constructing any python-docx object.
                total_cells += (len(rows) + 1) * len(headers)
                if total_cells > 4000:
                    raise ValueError("structure_size")
                if any(not isinstance(row, list) or len(row) != len(headers) or any(not isinstance(cell, str) for cell in row) for row in rows):
                    raise ValueError("table_row_invalid")
                total_chars += sum(map(len, headers)) + sum(len(cell) for row in rows for cell in row)
            if total_chars > 100_000:
                raise ValueError("content_size")
        document = Document()
        section = document.sections[0]
        section.top_margin = section.bottom_margin = Cm(2.2)
        section.left_margin = section.right_margin = Cm(2.5)
        normal = document.styles["Normal"]
        normal.font.name = "Arial"
        normal.font.size = Pt(10.5)
        normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft YaHei")
        normal.paragraph_format.line_spacing = 1.3
        document.add_heading(title, 0)
        for section_index, section_spec in enumerate(sections):
            document.add_heading(section_spec["heading"], level=1)
            for paragraph_index, text in enumerate(section_spec.get("paragraphs", [])):
                document.add_paragraph(text)
                content_map.append({"section": section_index, "paragraph": paragraph_index, "source_revision": approved_content["revision_id"]})
            for table_spec in section_spec.get("tables", []):
                table = document.add_table(rows=1, cols=len(table_spec["headers"]))
                table.style = "Table Grid"
                for cell, text in zip(table.rows[0].cells, table_spec["headers"]):
                    cell.text = text
                repeat = OxmlElement("w:tblHeader")
                table.rows[0]._tr.get_or_add_trPr().append(repeat)
                for row in table_spec["rows"]:
                    for cell, text in zip(table.add_row().cells, row):
                        cell.text = text
        document.core_properties.title = title
        document.core_properties.author = "Ampuly"
        document.core_properties.comments = ""
        buffer = BytesIO()
        document.save(buffer)
        raw = buffer.getvalue()
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("output_size")
        report = check_docx(raw, required_sections=[section["heading"] for section in sections], required_facts=spec.get("required_facts", {}))
        if report["status"] != "success":
            result["errors"] = ["generated_document_checks_failed"]
            result["check_report"] = report
            return result
        result.update({"status": "success", "document_bytes": raw, "content_hash": sha256(raw).hexdigest(),
                       "size_bytes": len(raw), "approved_content": {key: approved_content[key] for key in ("revision_id", "content_hash")},
                       "content_map": content_map, "check_report": report})
    except (ValueError, TypeError, KeyError):
        result["errors"] = ["invalid_or_unsupported_content_spec"]
    return result


def save_docx_new(result: Mapping, *, output_dir: Path, filename: str) -> dict:
    """Never replace a file; output_dir must already be approved by the caller.

    The created file is retained if a write fails, to allow reconciliation.
    Directory authorization and race-safe workspace handles belong to A line.
    """
    if not isinstance(result, Mapping):
        return {"status": "failed", "error": "invalid_generation_result"}
    raw = result.get("document_bytes")
    if result.get("status") != "success" or not isinstance(raw, bytes) or sha256(raw).hexdigest() != result.get("content_hash"):
        return {"status": "failed", "error": "invalid_generation_result"}
    if (not isinstance(filename, str) or len(filename) > 180 or not filename.lower().endswith(".docx")
            or any(char in filename for char in '/\\:\x00<>"|?*') or filename.rstrip(" .") != filename
            or filename.startswith(".") or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", filename)):
        return {"status": "failed", "error": "invalid_output_filename"}
    try:
        directory = Path(output_dir)
        directory_valid = directory.is_dir() and not directory.is_symlink() and os.path.normcase(str(directory.absolute())) == os.path.normcase(str(directory.resolve()))
    except (TypeError, ValueError, OSError):
        directory_valid = False
    if not directory_valid:
        return {"status": "failed", "error": "output_directory_invalid"}
    destination = directory / filename
    try:
        with destination.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # A line registers the actual artifact id/version in its controlled store.
        return {"status": "success", "path": str(destination), "content_hash": result["content_hash"],
                "size_bytes": len(raw), "created_new": True, "delivery_status": "not_sent"}
    except FileExistsError:
        return {"status": "failed", "error": "output_already_exists", "created_new": False}
    except OSError:
        return {"status": "failed", "error": "write_outcome_needs_inspection", "path": str(destination)}
