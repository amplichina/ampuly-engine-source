"""Bounded offline checks for employee output, not an approval or semantic judge.

Inputs must come from an authorized host. Version/hash checks prevent accidental
mixing, but these functions do not establish tenant access or human approval.
No model calls, file I/O, expressions, unit conversion or policy inference.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from collections.abc import Mapping

VERSION = "0.1"
KEYS = ("artifact_id", "version", "content_hash", "project_id")
MAX_BLOCKS = 20000
MAX_CHARS = 1000000
MAX_ITEMS = 500


def _ref(value):
    if not isinstance(value, Mapping) or any(not isinstance(value.get(k), str)
            or not value[k] or len(value[k]) > 512 for k in KEYS):
        raise ValueError("invalid_reference")
    result = {k: value[k] for k in KEYS}
    digest = result["content_hash"].removeprefix("sha256:").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_reference_hash")
    result["content_hash"] = digest
    return result


def _string(value, limit=4000):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("invalid_text")
    return value


def _list(value, limit=MAX_ITEMS):
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError("invalid_list_or_size")
    return value


def _canonical(value, kind):
    value = _string(value).strip()
    if kind == "decimal":
        # No comma grouping, exponent, inferred currency or amount substrings.
        if not re.fullmatch(r"[+-]?\d{1,30}(?:\.\d{1,12})?", value):
            raise ValueError("invalid_decimal")
        return Decimal(value)
    if kind == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("invalid_iso_date")
        return date.fromisoformat(value)
    if kind not in {"identifier", "literal"}:
        raise ValueError("unsupported_value_kind")
    return value  # identifiers are case-sensitive; leading zeros are significant


def _blocks(material, expected_source):
    if not isinstance(material, Mapping) or _ref(material.get("source")) != expected_source:
        raise ValueError("source_reference_mismatch")
    blocks = _list(material.get("blocks"), MAX_BLOCKS)
    total = 0
    seen = set()
    ranges = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ValueError("invalid_block")
        text = _string(block.get("text"), MAX_CHARS)
        total += len(text)
        index, start, end = block.get("block_index"), block.get("text_start"), block.get("text_end")
        if any(type(x) is not int for x in (index, start, end)) or index < 0 or start < 0 or end - start != len(text):
            raise ValueError("invalid_block_range")
        key = (index, start, end)
        if key in seen or total > MAX_CHARS:
            raise ValueError("duplicate_block_or_size")
        if not isinstance(block.get("location", {}), Mapping):
            raise ValueError("invalid_block_location")
        seen.add(key)
        ranges.setdefault(index, []).append((start, end))
    for spans in ranges.values():
        spans.sort()
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise ValueError("overlapping_blocks")
    return blocks


def _base(name):
    return {"status": "failed", "checker": {"name": name, "version": VERSION},
            "checks": [], "findings": [], "authorization_checked": False,
            "semantic_status": "requires_human_review", "visual_status": "unverified"}


def _coverage(material):
    coverage = material.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("invalid_reading_coverage")
    return coverage


def merge_reading_pages(pages, *, source):
    """Merge bounded, contiguous reader pages without losing citation offsets.

    The host must supply authentic tool results. This only checks consistency;
    it never attests that source bytes were authorized or visually inspected.
    """
    result = {"status": "failed", "source": {}, "blocks": [], "coverage": {"full_document": False},
              "authorization_checked": False, "warnings": [], "next_offset": None}
    try:
        expected = _ref(source)
        result["source"] = expected
        pages = _list(pages, 2000)
        if not pages:
            raise ValueError("missing_reading_pages")
        cursor = 0
        total_chars = None
        parser = None
        scope = None
        assembled = {}
        metadata = {}
        complete = True
        warnings = []
        for position, page in enumerate(pages):
            if not isinstance(page, Mapping) or page.get("status") not in {"success", "partial"}:
                raise ValueError("failed_reading_page")
            blocks = _blocks(page, expected)
            coverage = page.get("coverage")
            if not isinstance(coverage, Mapping):
                raise ValueError("missing_page_coverage")
            count = coverage.get("parsed_text_chars")
            start, end = coverage.get("returned_start"), coverage.get("returned_end")
            if any(type(n) is not int for n in (count, start, end)) or count < 0 or count > MAX_CHARS or start != cursor or end < start or end > count:
                raise ValueError("page_cursor_mismatch")
            if sum(len(b["text"]) for b in blocks) != end-start:
                raise ValueError("page_cursor_text_mismatch")
            if position == 0:
                total_chars, parser, scope = count, page.get("parser"), coverage.get("scope")
                if not isinstance(parser, Mapping) or not parser.get("name") or not parser.get("version") or not isinstance(scope, str) or not scope:
                    raise ValueError("missing_page_parse_basis")
            elif count != total_chars or parser != page.get("parser") or scope != coverage.get("scope"):
                raise ValueError("page_parse_basis_changed")
            if position < len(pages)-1 and (end == start or page.get("next_offset") != end):
                raise ValueError("page_cursor_no_progress")
            if position == len(pages)-1 and (end != count or page.get("next_offset") is not None):
                raise ValueError("reading_pages_not_exhausted")
            for block in blocks:
                index = block["block_index"]
                length = block.get("block_text_chars")
                if type(length) is not int or length < block["text_end"] or length > MAX_CHARS:
                    raise ValueError("invalid_original_block_size")
                if index not in assembled:
                    if block["text_start"] != 0 or index != len(assembled) or len(assembled) >= MAX_BLOCKS:
                        raise ValueError("block_order_or_size_mismatch")
                    if index and metadata[index-1]["end"] != metadata[index-1]["block_text_chars"]:
                        raise ValueError("unfinished_previous_block")
                    assembled[index] = []
                    metadata[index] = {**block, "end": 0}
                elif index != len(assembled)-1:
                    raise ValueError("block_order_mismatch")
                previous = metadata[index]
                if block["text_start"] != previous["end"] or length != previous["block_text_chars"] or block.get("location") != previous.get("location") or block.get("kind") != previous.get("kind"):
                    raise ValueError("block_continuation_mismatch")
                assembled[index].append(block["text"])
                previous["end"] = block["text_end"]
            for warning in _list(page.get("warnings", []), 1000):
                if warning not in warnings:
                    if len(warnings) >= 1000:
                        raise ValueError("warning_count_limit")
                    warnings.append(warning)
            complete = complete and coverage.get("parsed_text_complete") is True
            cursor = end
        for index, pieces in assembled.items():
            item = dict(metadata[index])
            end = item.pop("end")
            if end != item["block_text_chars"]:
                raise ValueError("incomplete_block")
            item.update(text="".join(pieces), text_start=0, text_end=end, continued=False)
            result["blocks"].append(item)
        result.update(status="success" if complete else "partial", warnings=warnings, parser=parser,
                      coverage={"scope": scope, "parsed_text_complete": complete, "full_document": complete,
                                "returned_start": 0, "returned_end": cursor, "returned_entire_text": True,
                                "parsed_text_chars": cursor, "parsed_blocks": len(assembled)},
                      trust="untrusted_material")
    except (ValueError, TypeError, KeyError) as exc:
        result["blocks"] = []
        result["error"] = {"code": str(exc) if isinstance(exc, ValueError) else "invalid_input"}
    return result


def check_document_facts(material, *, source, approved_facts, approved_content):
    """Compare declared label/value lines, not arbitrary prose or whole semantics.

    Facts: id,label,value,kind and optional unit. Example: 金额：1200.00 元.
    Repeated equal values are allowed; any differing labeled occurrence fails.
    Lines outside complete blocks are unknown. Host must attest the reader output.
    approved_content.hash binds the exact canonical JSON approved_facts bytes;
    this cryptographic binding does NOT prove an actual approval exists.
    """
    result = _base("ampuly_document_exact_facts")
    try:
        ref = _ref(source)
        facts = _list(approved_facts)
        if not facts or not isinstance(approved_content, Mapping):
            raise ValueError("missing_approved_facts")
        revision = _string(approved_content.get("revision_id"), 512)
        blocks = _blocks(material, ref)
        coverage = _coverage(material)
        identifiers, labels = set(), set()
        normalized = []
        for fact in facts:
            if not isinstance(fact, Mapping) or set(fact) - {"id", "label", "value", "kind", "unit"}:
                raise ValueError("invalid_fact_spec")
            identifier, label = _string(fact.get("id"), 128), _string(fact.get("label"), 128)
            if identifier in identifiers or label in labels or any(c in label for c in ":：\r\n"):
                raise ValueError("duplicate_fact_or_invalid_label")
            identifiers.add(identifier)
            labels.add(label)
            expected = _canonical(fact.get("value"), fact.get("kind"))
            unit = fact.get("unit", "")
            if not isinstance(unit, str) or len(unit) > 32 or any(c.isspace() for c in unit):
                raise ValueError("invalid_unit")
            normalized.append((fact, expected, unit))
        encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_CHARS or sha256(encoded).hexdigest() != approved_content.get("content_hash"):
            raise ValueError("approved_content_hash_mismatch")
        field_lines = {}
        for block in blocks:
            if block.get("continued") or block.get("location", {}).get("part", "word/document.xml") != "word/document.xml":
                continue
            for line_no, line in enumerate(block["text"].splitlines(), 1):
                delimiters = [p for p in (line.find(":"), line.find("：")) if p >= 0]
                if not delimiters:
                    continue
                split = min(delimiters)
                label, rendered = line[:split].strip(), line[split+1:].strip()
                if label in labels:
                    field_lines.setdefault(label, []).append((block, line_no, rendered))
        for fact, expected, unit in normalized:
            occurrences = []
            for block, line_no, rendered in field_lines.get(fact["label"], []):
                correct_unit = not unit or rendered.endswith(unit)
                candidate = rendered[:-len(unit)].strip() if unit and correct_unit else rendered
                try:
                    agrees = correct_unit and _canonical(candidate, fact["kind"]) == expected
                except (ValueError, InvalidOperation):
                    agrees = False
                occurrences.append({"block_index": block["block_index"], "line_in_block": line_no,
                                    "location": dict(block.get("location", {})), "matches": agrees})
            state = "matched" if occurrences and all(v["matches"] for v in occurrences) else "mismatch" if occurrences else "not_found"
            result["checks"].append({"id": fact["id"], "state": state, "occurrences": occurrences})
            if state != "matched":
                result["findings"].append({"code": "fact_" + state, "id": fact["id"]})
        full = coverage.get("full_document") is True
        if not full:
            result["findings"].append({"code": "reading_scope_incomplete"})
        result.update(source=ref, approved_content={"revision_id": revision, "content_hash": sha256(encoded).hexdigest()},
                      status="passed" if not result["findings"] else "failed" if any(c["state"] == "mismatch" for c in result["checks"]) or full else "unknown",
                      scope="declared label/value lines in complete body blocks only; no unit conversion")
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        result["findings"].append({"code": str(exc) if isinstance(exc, ValueError) else "invalid_input"})
    return result


def check_material_review(review, *, rules, readings, expected_project, inventory=None):
    """Check checklist, declared read scope and exact source-span citations.

    Valid citations prove that quoted text exists, NOT that it logically supports
    the conclusion. Rule applicability, contradictions and severity correctness
    need independently labeled review. Missing uses a required-artifact inventory
    supplied separately by the host, never an inventory assertion from the
    model-authored review itself or an invented quote for nonexistent material.
    """
    result = _base("ampuly_material_report_evidence")
    try:
        _string(expected_project, 512)
        rule_list, reading_list = _list(rules), _list(readings, 200)
        if not rule_list or not isinstance(review, Mapping):
            raise ValueError("invalid_review")
        items = _list(review.get("items"))
        available_ids = []
        if inventory is not None:
            if not isinstance(inventory, Mapping) or type(inventory.get("complete")) is not bool:
                raise ValueError("invalid_host_inventory")
            available_ids = _list(inventory.get("available_artifact_ids"))
            for identifier in available_ids:
                _string(identifier, 512)
            if len(set(available_ids)) != len(available_ids):
                raise ValueError("duplicate_inventory_item")
        rule_map = {}
        for rule in rule_list:
            if not isinstance(rule, Mapping):
                raise ValueError("invalid_rule")
            identifier = _string(rule.get("id"), 128)
            if identifier in rule_map:
                raise ValueError("duplicate_rule")
            rule_map[identifier] = rule
        sources = {}
        total_blocks = total_chars = 0
        for reading in reading_list:
            if not isinstance(reading, Mapping):
                raise ValueError("invalid_reading")
            ref = _ref(reading.get("source"))
            _coverage(reading)
            if ref["project_id"] != expected_project:
                raise ValueError("cross_project_reading")
            key = tuple(ref[k] for k in KEYS)
            if key in sources:
                raise ValueError("duplicate_reading")
            blocks = _blocks(reading, ref)
            total_blocks += len(blocks)
            total_chars += sum(len(b["text"]) for b in blocks)
            if total_blocks > MAX_BLOCKS or total_chars > MAX_CHARS:
                raise ValueError("reading_batch_size")
            by_index = {}
            for block in blocks:
                by_index.setdefault(block["block_index"], []).append(block)
            sources[key] = (reading, by_index)
        seen = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("invalid_review_item")
            identifier = _string(item.get("rule_id"), 128)
            if identifier in seen or identifier not in rule_map:
                raise ValueError("duplicate_or_unknown_rule_result")
            seen.add(identifier)
            state = item.get("conclusion")
            if state not in {"passed", "missing", "conflict", "unknown", "not_applicable"}:
                raise ValueError("invalid_conclusion")
            citations = _list(item.get("evidence", []), 100)
            problems = []
            valid = 0
            citation_locations = set()
            for evidence in citations:
                if not isinstance(evidence, Mapping):
                    raise ValueError("invalid_evidence")
                ref = _ref(evidence.get("source"))
                matched = sources.get(tuple(ref[k] for k in KEYS))
                if not matched:
                    problems.append("evidence_source_not_read")
                    continue
                index, start, end = (evidence.get(k) for k in ("block_index", "text_start", "text_end"))
                quote = _string(evidence.get("quote"))
                if any(type(v) is not int for v in (index, start, end)) or start < 0 or end <= start or len(quote) != end - start:
                    problems.append("invalid_evidence_range")
                    continue
                candidate = next((b for b in matched[1].get(index, []) if b["text_start"] <= start and b["text_end"] >= end), None)
                if not candidate or candidate["text"][start-candidate["text_start"]:end-candidate["text_start"]] != quote:
                    problems.append("evidence_quote_mismatch")
                elif not isinstance(evidence.get("location"), Mapping) or evidence["location"] != candidate.get("location", {}):
                    problems.append("evidence_location_mismatch")
                else:
                    citation_locations.add((tuple(ref[k] for k in KEYS), index, start, end))
                    valid = len(citation_locations)
            if state in {"passed", "conflict"} and valid < (2 if state == "conflict" else 1):
                problems.append("insufficient_citations")
            if state == "missing":
                rule = rule_map[identifier]
                missing_id = item.get("missing_artifact_id")
                if rule.get("required_artifact_id") != missing_id or not missing_id:
                    problems.append("missing_without_required_artifact_rule")
                elif any(key[0] == missing_id for key in sources):
                    problems.append("missing_artifact_was_read")
                # A complete manifest must be attested by host; reading list alone
                # cannot distinguish absent from not yet read / unauthorized.
                if not isinstance(inventory, Mapping) or inventory.get("complete") is not True or missing_id in available_ids:
                    problems.append("missing_inventory_not_proven")
            if state == "not_applicable":
                basis = item.get("applicability_basis_ref")
                approved_bases = _list(rule_map[identifier].get("applicability_basis_refs", []))
                if not basis or basis not in approved_bases:
                    problems.append("applicability_basis_missing_or_not_fixed")
            result["checks"].append({"rule_id": identifier, "state": "failed" if problems else "machine_checked",
                                      "conclusion": state, "valid_citations": valid})
            result["findings"].extend({"code": p, "rule_id": identifier} for p in problems)
        if seen != set(rule_map):
            result["findings"].append({"code": "checklist_not_covered", "missing_rule_ids": sorted(set(rule_map)-seen)})
        if review.get("claimed_full_read") is True and (not sources or any(r[0]["coverage"].get("full_document") is not True for r in sources.values())
                or not isinstance(inventory, Mapping) or inventory.get("complete") is not True
                or not set(available_ids).issubset({key[0] for key in sources})):
            result["findings"].append({"code": "false_full_read_claim"})
        result["status"] = "passed" if not result["findings"] else "failed"
        result["scope"] = "checklist and source-span consistency; business conclusions require independent human labels"
    except (ValueError, TypeError, KeyError) as exc:
        result["findings"].append({"code": str(exc) if isinstance(exc, ValueError) else "invalid_input"})
    return result
