"""Whole ToolRegistry invocation schemas for the two native read tools."""
from copy import deepcopy

_TEXT = {"type": "string"}
_COUNT = {"type": "integer", "minimum": 0}
_ERROR = {"type": "object", "required": ["success", "error"],
          "properties": {"success": {"const": False}, "error": _TEXT}}
_META = {"truncated": {"type": "boolean"}, "omitted_chars": _COUNT,
         "original_size_chars": _COUNT, "full_output_path": _TEXT}
_PREVIEW = {"type": "object", "required": ["preview", "truncated"],
            "properties": {"preview": _TEXT, **_META}}
_READ = {"type": "object", "required": ["success", "content", "path", "total_lines",
    "returned_start_line", "returned_line_count", "next_offset", "truncated", "omitted_chars"],
    "properties": {"success": {"const": True}, "content": _TEXT, "path": _TEXT,
        "total_lines": _COUNT, "returned_start_line": _COUNT, "returned_line_count": _COUNT,
        "next_offset": {"type": ["integer", "null"], "minimum": 0}, **_META}}
_LIST = {"type": "object", "required": ["success", "path", "entries", "items", "total"],
    "properties": {"success": {"const": True}, "path": _TEXT, "total": _COUNT,
        "entries": {"type": "array", "maxItems": 500, "items": _TEXT},
        "items": {"type": "array", "maxItems": 500, "items": {
            "type": "object", "required": ["path", "is_dir", "size"],
            "properties": {"path": _TEXT, "is_dir": {"type": "boolean"}, "size": _COUNT}}}, **_META}}


def read_tool_return_schema(name):
    if name not in {"file_read", "list_dir"}:
        raise ValueError("unsupported read tool")
    success = {"type": "object", "required": ["success", "result"],
        "properties": {"success": {"const": True}, "result": {
            "anyOf": [_READ if name == "file_read" else _LIST, _ERROR, _PREVIEW]}, **_META}}
    # A long registry error can also be represented by an output preview.
    failed_preview = {"type": "object", "required": ["success", "result", "truncated"],
        "properties": {"success": {"const": False}, "result": _PREVIEW, **_META}}
    return deepcopy({"anyOf": [success, _ERROR, failed_preview]})
