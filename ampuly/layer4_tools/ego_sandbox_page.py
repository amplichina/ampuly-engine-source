"""Fixed read/guard scripts for the existing local sandbox SPA, never model JS.

This adapter does not authorize writes. Its expected record must be constructed
by the server Host from a current inspection and an already approved Effect.
Only this trusted local application is supported; it is not an Internet fence.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ampuly.layer4_tools.ego_backend import EgoBackend, EgoActionFailed, _bound_script
from ampuly.layer4_tools.browser_backend import Action

FIELD_IDS = ("f_title", "f_sub", "f_price", "f_orig", "f_stock", "f_cat", "f_emoji", "f_status")
FIELD_TO_PRODUCT_KEY = dict(zip(FIELD_IDS, (
    "title", "subtitle", "price", "originalPrice", "stock", "category", "emoji", "status")))
SELECT_OPTIONS = {"f_cat": ("牛排", "火锅", "快手菜", "海鲜", "面点", "速食", "轻食"),
                  "f_status": ("上架", "下架")}
GUARD_SCHEMA = "ampuly.ego.sandbox.guard/v1"
PAGE_SCHEMA = "ampuly.ego.sandbox.page/v1"
KINDS = frozenset({"save_existing", "status", "menu_products", "open_existing", "close_modal"})
_BATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_OPEN = re.compile(r"openProduct\(([1-9][0-9]{0,14})\)\Z")
_STATUS = re.compile(r"toggleStatus\(([1-9][0-9]{0,14}),'(上架|下架)'\)\Z")


class EgoSandboxRefused(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _text(value: Any, limit: int, *, empty: bool = True) -> bool:
    return (type(value) is str and len(value) <= limit and (empty or bool(value))
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _selector(value: str, *, empty: bool = False) -> str:
    if not _text(value, 1024, empty=empty):
        raise EgoSandboxRefused("sandbox_selector_invalid")
    value = value[4:] if value.startswith("css:") else value
    if not value and not empty:
        raise EgoSandboxRefused("sandbox_selector_invalid")
    return value


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or not parsed.port
                or not 1024 <= parsed.port <= 65535):
            raise ValueError()
        return f"http://127.0.0.1:{parsed.port}"
    except (TypeError, ValueError):
        raise EgoSandboxRefused("sandbox_loopback_origin_required") from None


def _source_url(value: str, origin: str, batch: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (_text(value, 4096, empty=False) and not parsed.username and not parsed.password
                 and f"{parsed.scheme}://{parsed.netloc}" == origin and parsed.path == "/admin"
                 and not parsed.fragment and parse_qs(parsed.query, keep_blank_values=True).get("batch") == [batch])
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise EgoSandboxRefused("sandbox_page_out_of_scope")


def validate_guard(expected: dict[str, Any], *, selector: str, origin: str, batch_id: str) -> dict[str, Any]:
    """Validate data syntax, not authority; the Host still owns Effect approval."""
    if (type(expected) is not dict or set(expected) != {
            "schema", "kind", "url", "batch_id", "target", "edit_id", "fields"}
            or expected.get("schema") != GUARD_SCHEMA or expected.get("kind") not in KINDS
            or expected.get("batch_id") != batch_id):
        raise EgoSandboxRefused("sandbox_guard_schema_invalid")
    _source_url(expected["url"], origin, batch_id)
    target = expected["target"]
    if (type(target) is not dict or set(target) != {"selector", "label", "tag", "onclick", "disabled"}
            or target.get("selector") != selector or not _text(target.get("label"), 180, empty=False)
            or target.get("tag") not in {"button", "span", "div", "a"}
            or not _text(target.get("onclick"), 200, empty=False) or target.get("disabled") is not False):
        raise EgoSandboxRefused("sandbox_guard_target_invalid")
    if any(word in target["label"].lower() for word in ("删除", "支付", "付款", "退款", "清空", "密码", "delete", "pay", "refund")):
        raise EgoSandboxRefused("sandbox_action_forbidden")
    edit_id = expected["edit_id"]
    if edit_id is not None and (type(edit_id) is not int or not 1 <= edit_id <= 9007199254740991):
        raise EgoSandboxRefused("sandbox_edit_identity_invalid")
    fields = expected["fields"]
    if (type(fields) is not dict or set(fields) != set(FIELD_IDS)
            or any(value is not None and not _text(value, 2048) for value in fields.values())):
        raise EgoSandboxRefused("sandbox_guard_fields_invalid")
    kind, onclick = expected["kind"], target["onclick"]
    allowed = (
        kind == "save_existing" and onclick == "saveProduct()" and target["tag"] == "button"
        and target["label"] == "保存" and edit_id is not None and all(value is not None for value in fields.values())
        or kind == "status" and bool(_STATUS.fullmatch(onclick)) and target["tag"] == "span"
        and target["label"] == _STATUS.fullmatch(onclick).group(2)
        or kind == "menu_products" and onclick == "go('products')" and target["tag"] == "div"
        or kind == "open_existing" and bool(_OPEN.fullmatch(onclick)) and target["tag"] == "span" and target["label"] == "编辑"
        or kind == "close_modal" and onclick == "closeModal()" and target["tag"] == "button" and target["label"] in {"取消", "×"}
    )
    if not allowed:
        raise EgoSandboxRefused("sandbox_action_not_supported")
    return copy.deepcopy(expected)


def guard_from_inspection(observation: dict[str, Any], *, selector: str, kind: str,
                          origin: str, batch_id: str) -> dict[str, Any]:
    """Server convenience only; calling this function grants no execution."""
    selector = _selector(selector)
    if (type(observation) is not dict or observation.get("ok") is not True
            or observation.get("schema") != PAGE_SCHEMA or observation.get("resource_state") != "active"):
        raise EgoSandboxRefused("sandbox_current_inspection_required")
    target = observation.get("target")
    if not isinstance(target, dict) or target.get("count") != 1:
        raise EgoSandboxRefused("sandbox_unique_target_required")
    expected = {"schema": GUARD_SCHEMA, "kind": kind, "url": observation.get("url"),
                "batch_id": batch_id, "target": {key: target.get(key) for key in (
                    "selector", "label", "tag", "onclick", "disabled")},
                "edit_id": observation.get("edit_id"), "fields": observation.get("fields")}
    return validate_guard(expected, selector=selector, origin=_origin(origin), batch_id=batch_id)


_READ = r"""(request) => {
  const here = new URL(location.href);
  if (here.origin !== request.origin || here.pathname !== '/admin' || here.hash ||
      here.searchParams.getAll('batch').length !== 1 || here.searchParams.get('batch') !== request.batch)
    throw new Error('sandbox_page_out_of_scope');
  const visible = (el) => !!el && !!el.getClientRects().length &&
    getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden';
  const cssPath = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const tag = node.tagName.toLowerCase();
      let index = 1;
      for (let prev = node.previousElementSibling; prev; prev = prev.previousElementSibling)
        if (prev.tagName === node.tagName) index++;
      parts.unshift(tag + ':nth-of-type(' + index + ')');
      if (parts.length > 16) return '';
    }
    return parts.join(' > ');
  };
  const describe = (el, selector) => ({selector: selector || cssPath(el),
    label: String(el.innerText || el.getAttribute('aria-label') || el.title || el.value || '').trim().replace(/\s+/g,' ').slice(0,180),
    tag: el.tagName.toLowerCase(), onclick: String(el.getAttribute('onclick') || '').slice(0,200),
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
    input_type: el.tagName.toLowerCase()==='input' ? String(el.type).toLowerCase() : '',
    editable: ['input','textarea','select'].includes(el.tagName.toLowerCase()) && !el.disabled && !el.readOnly,
    readonly: !!el.readOnly,
    options: el.tagName.toLowerCase()==='select' ? [...el.options].slice(0,32).map(o=>({label:o.label,value:o.value,disabled:!!o.disabled})) : []});
  const fields = {};
  for (const id of ['f_title','f_sub','f_price','f_orig','f_stock','f_cat','f_emoji','f_status']) {
    const nodes = document.querySelectorAll('#' + id);
    if (nodes.length > 1) throw new Error('sandbox_duplicate_field');
    fields[id] = nodes.length ? String(nodes[0].value) : null;
    if (fields[id] !== null && fields[id].length > 2048) throw new Error('sandbox_field_overflow');
  }
  const all = [...document.querySelectorAll('button,a,[onclick]')].filter(visible);
  if (all.length > 200) throw new Error('sandbox_elements_overflow');
  const elements = all.map(el => describe(el, '')).filter(el => el.selector);
  const rows = [];
  for (const row of document.querySelectorAll('#main tbody tr')) {
    const edit = [...row.querySelectorAll('[onclick]')].find(el => /^openProduct\([1-9][0-9]{0,14}\)$/.test(el.getAttribute('onclick') || ''));
    if (!edit) continue;
    const productId = Number(edit.getAttribute('onclick').match(/[0-9]+/)[0]);
    rows.push({product_id: productId, columns: [...row.querySelectorAll('td')].slice(0,7).map(td => String(td.innerText || '').trim().slice(0,512))});
    if (rows.length > 100) throw new Error('sandbox_rows_overflow');
  }
  let target = null;
  if (request.selector) {
    const found = document.querySelectorAll(request.selector);
    target = {count: found.length};
    if (found.length === 1 && visible(found[0])) Object.assign(target, describe(found[0],request.selector));
  }
  return {url:location.href, title:document.title, edit_id: typeof editId === 'number' ? editId : null,
          fields, elements, rows, target};
}"""


def _inspect_body(*, selector: str, origin: str, batch_id: str) -> str:
    data = json.dumps({"selector": selector, "origin": origin, "batch": batch_id}, ensure_ascii=True)
    return (
        "try {\nconst urlBefore = await page.url();\n"
        f"const inspection = await page.evaluate({_READ}, {data});\n"
        'const snapshot = await page.snapshot({scope:"full_page",includeStableLocator:true});\n'
        "if (await page.url() !== urlBefore || inspection.url !== urlBefore) throw new Error('sandbox_page_changed');\n"
        'console.log(JSON.stringify({...ampulyEffect,ok:true,phase:"completed",inspection,snapshot}));\n'
        '} catch (_) { console.log(JSON.stringify({...ampulyEffect,ok:false,phase:"before_effect",error:"sandbox_inspection_failed"})); }\n'
    )


def _click_body(expected: dict[str, Any]) -> str:
    data = json.dumps(expected, ensure_ascii=True)
    selector = json.dumps("css:" + expected["target"]["selector"], ensure_ascii=True)
    return (
        'let phase = "before_effect";\ntry {\n'
        "const holds = await page.evaluate((expected) => {\n"
        " if (location.href !== expected.url) return false;\n"
        " const here = new URL(location.href); if (here.searchParams.getAll('batch').length !== 1 || here.searchParams.get('batch') !== expected.batch_id) return false;\n"
        " const found=document.querySelectorAll(expected.target.selector); if(found.length!==1) return false;\n"
        " const el=found[0]; if(!el.getClientRects().length || getComputedStyle(el).display==='none' || getComputedStyle(el).visibility==='hidden') return false;\n"
        " const label=String(el.innerText || el.getAttribute('aria-label') || el.title || el.value || '').trim().replace(/\\s+/g,' ').slice(0,180);\n"
        " if(label!==expected.target.label || el.tagName.toLowerCase()!==expected.target.tag || String(el.getAttribute('onclick')||'')!==expected.target.onclick || !!el.disabled || el.getAttribute('aria-disabled')==='true') return false;\n"
        " if((typeof editId==='number'?editId:null)!==expected.edit_id) return false;\n"
        " return Object.entries(expected.fields).every(([id,value])=>{const nodes=document.querySelectorAll('#'+id);return value===null?nodes.length===0:nodes.length===1 && String(nodes[0].value)===value;});\n"
        f"}}, {data});\n"
        "if (!holds) throw new Error('sandbox_guard_changed');\n"
        'phase = "effect_possible";\n'
        f"await page.click({selector});\n"
        'phase = "after_effect";\n'
        'const snapshot=await page.snapshot({scope:"full_page",includeStableLocator:true});\n'
        'console.log(JSON.stringify({...ampulyEffect,ok:true,phase:"completed",snapshot}));\n'
        '} catch (_) { console.log(JSON.stringify({...ampulyEffect,ok:false,phase,error:phase==="before_effect"?"sandbox_guard_changed":"sandbox_click_outcome_unknown"})); }\n'
    )


def _draft_contract(action: Action, observation: dict[str, Any], *, origin: str, batch_id: str) -> tuple[dict[str, Any], str]:
    if (type(action) is not Action or action.kind not in {"fill", "select", "scroll", "navigate"}
            or type(action.meta) is not dict):
        raise EgoSandboxRefused("sandbox_draft_action_unsupported")
    if (type(observation) is not dict or observation.get("schema") != PAGE_SCHEMA
            or observation.get("ok") is not True or observation.get("unknown") is not False
            or observation.get("resource_state") != "active"):
        raise EgoSandboxRefused("sandbox_current_inspection_required")
    _source_url(observation.get("url"), origin, batch_id)
    old_fields = observation.get("fields")
    if (type(old_fields) is not dict or set(old_fields) != set(FIELD_IDS)
            or any(value is not None and not _text(value, 2048) for value in old_fields.values())):
        raise EgoSandboxRefused("sandbox_guard_fields_invalid")
    edit_id = observation.get("edit_id")
    if edit_id is not None and (type(edit_id) is not int or not 1 <= edit_id <= 9007199254740991):
        raise EgoSandboxRefused("sandbox_edit_identity_invalid")
    expected = {"url": observation["url"], "batch_id": batch_id, "fields": copy.deepcopy(old_fields),
                "edit_id": edit_id, "target": None}
    if action.kind == "navigate":
        if action.target != observation["url"] or action.value or action.meta:
            raise EgoSandboxRefused("sandbox_only_exact_current_refresh_allowed")
        return expected, f"await page.goto({json.dumps(action.target, ensure_ascii=True)});\n"
    if action.kind == "scroll":
        dy = action.meta.get("dy")
        if (action.target or action.value or set(action.meta) != {"dy"}
                or type(dy) is not int or not 1 <= abs(dy) <= 2000):
            raise EgoSandboxRefused("sandbox_scroll_invalid")
        return expected, f"await page.mouse.wheel(0, {dy});\n"
    selector = _selector(action.target)
    if not selector.startswith("#") or selector[1:] not in FIELD_IDS or action.meta:
        raise EgoSandboxRefused("sandbox_known_draft_field_required")
    field = selector[1:]
    target = observation.get("target")
    if (type(target) is not dict or target.get("count") != 1 or target.get("selector") != selector
            or target.get("disabled") is not False or target.get("readonly") is not False
            or target.get("editable") is not True or edit_id is None
            or any(value is None for value in old_fields.values())):
        raise EgoSandboxRefused("sandbox_unique_editable_field_required")
    if action.kind == "fill":
        input_type = "number" if field in {"f_price", "f_orig", "f_stock"} else "text"
        if (field in SELECT_OPTIONS or target.get("tag") != "input" or target.get("input_type") != input_type
                or not _text(action.value, 2048)):
            raise EgoSandboxRefused("sandbox_plain_input_required")
        body = f"await page.fill({json.dumps('css:' + selector)}, {json.dumps(action.value, ensure_ascii=True)});\n"
        expected["target"] = {"selector": selector, "tag": "input", "input_type": input_type, "options": []}
    else:
        known = SELECT_OPTIONS.get(field)
        if (known is None or action.value not in known or target.get("tag") != "select"
                or target.get("options") != [{"label": value, "value": value, "disabled": False} for value in known]):
            raise EgoSandboxRefused("sandbox_known_select_options_required")
        expected["target"] = {"selector": selector, "tag": "select", "input_type": "",
                              "options": copy.deepcopy(target["options"])}
        # This fixed SPA has no explicit option values: DOM value == label.
        body = f"await page.selectOption({json.dumps('css:' + selector)}, {{label:{json.dumps(action.value, ensure_ascii=True)}}});\n"
    return expected, body


def _draft_body(expected: dict[str, Any], input_statement: str) -> str:
    data = json.dumps(expected, ensure_ascii=True)
    return (
        'let phase="before_effect";\ntry {\n'
        "const holds=await page.evaluate((expected)=>{\n"
        " if(location.href!==expected.url) return false; const here=new URL(location.href);\n"
        " if(here.searchParams.getAll('batch').length!==1 || here.searchParams.get('batch')!==expected.batch_id) return false;\n"
        " if((typeof editId==='number'?editId:null)!==expected.edit_id) return false;\n"
        " if(!Object.entries(expected.fields).every(([id,value])=>{const n=document.querySelectorAll('#'+id);return value===null?n.length===0:n.length===1 && String(n[0].value)===value;})) return false;\n"
        " if(!expected.target) return true; const t=expected.target, nodes=document.querySelectorAll(t.selector);\n"
        " if(nodes.length!==1) return false; const el=nodes[0];\n"
        " if(!el.getClientRects().length || getComputedStyle(el).display==='none' || getComputedStyle(el).visibility==='hidden' || el.disabled || el.readOnly || el.form) return false;\n"
        " if(['onclick','oninput','onchange','onkeydown','onkeyup','onkeypress'].some(name=>el.hasAttribute(name))) return false;\n"
        " if(el.tagName.toLowerCase()!==t.tag || (t.tag==='input' && String(el.type).toLowerCase()!==t.input_type)) return false;\n"
        " return t.tag!=='select' || JSON.stringify([...el.options].map(o=>({label:o.label,value:o.value,disabled:!!o.disabled})))===JSON.stringify(t.options);\n"
        f"}}, {data});\n"
        "if(!holds) throw new Error('sandbox_guard_changed');\n"
        'phase="effect_possible";\n' + input_statement + 'phase="after_effect";\n'
        'const snapshot=await page.snapshot({scope:"full_page",includeStableLocator:true});\n'
        'console.log(JSON.stringify({...ampulyEffect,ok:true,phase:"completed",snapshot}));\n'
        '} catch (_) { console.log(JSON.stringify({...ampulyEffect,ok:false,phase,error:phase==="before_effect"?"sandbox_guard_changed":"sandbox_draft_outcome_unknown"})); }\n'
    )


class EgoSandboxPage:
    def __init__(self, backend: EgoBackend, *, expected_origin: str = "http://127.0.0.1:8899", batch_id: str) -> None:
        if not isinstance(backend, EgoBackend):
            raise TypeError("existing_ego_backend_required")
        if type(batch_id) is not str or not _BATCH.fullmatch(batch_id):
            raise EgoSandboxRefused("sandbox_batch_invalid")
        self.backend = backend
        self.origin = _origin(expected_origin)
        self.batch_id = batch_id

    def _script(self, body: str, action_id: str, attempt_id: str) -> str:
        for value in (action_id, attempt_id):
            if not _text(value, 256, empty=False):
                raise EgoSandboxRefused("sandbox_action_identity_invalid")
        return _bound_script(space_name=self.backend._ego_space_name, page_label=self.backend.page_label,
            ego_profile_id=self.backend.ego_profile_id, space_id=self.backend._ego_space_id,
            action_id=action_id, attempt_id=attempt_id, body=body)

    def inspect(self, selector: str = "") -> dict[str, Any]:
        selector = _selector(selector, empty=True)
        with self.backend._operation_lock:
            recovery = self.backend.lifecycle_state == "unknown"
            if recovery:
                if type(self.backend._ego_space_id) is not int:
                    raise EgoActionFailed("ego_readonly_recovery_refused")
            else:
                self.backend._require_active_or_new()
            action_id, attempt_id = self.backend._mint_attempt()
            source = self._script(_inspect_body(selector=selector, origin=self.origin, batch_id=self.batch_id), action_id, attempt_id)
            result = self.backend._run_script(source, recovery_readonly=recovery)
            if result.get("ok") is not True or result.get("unknown") is True:
                return result
            inspection = result.get("inspection")
            if type(inspection) is not dict:
                return {"ok": False, "unknown": False, "error": "sandbox_inspection_missing"}
            _source_url(inspection.get("url"), self.origin, self.batch_id)
            return {**inspection, "schema": PAGE_SCHEMA, "ok": True, "unknown": False,
                    "snapshot": result.get("snapshot"), "resource_state": self.backend.lifecycle_state,
                    "read_only_recovery": recovery}

    def guarded_click(self, selector: str, expected: dict[str, Any], *, action_id: str, attempt_id: str) -> dict[str, Any]:
        selector = _selector(selector)
        clean = validate_guard(expected, selector=selector, origin=self.origin, batch_id=self.batch_id)
        with self.backend._operation_lock:
            self.backend._require_active_or_new()
            if self.backend.lifecycle_state != "active" or type(self.backend._ego_space_id) is not int:
                raise EgoSandboxRefused("sandbox_observed_space_required")
            # Receipt filenames use the existing monotonic local round; the
            # actual action/attempt identity comes from the original Effect.
            self.backend._mint_attempt()
            return self.backend._run_script(self._script(_click_body(clean), action_id, attempt_id))

    def guarded_draft(self, action: Action, expected_observation: dict[str, Any], *, action_id: str, attempt_id: str) -> dict[str, Any]:
        expected, statement = _draft_contract(action, expected_observation, origin=self.origin, batch_id=self.batch_id)
        with self.backend._operation_lock:
            self.backend._require_active_or_new()
            if self.backend.lifecycle_state != "active" or type(self.backend._ego_space_id) is not int:
                raise EgoSandboxRefused("sandbox_observed_space_required")
            self.backend._mint_attempt()
            return self.backend._run_script(self._script(_draft_body(expected, statement), action_id, attempt_id))
