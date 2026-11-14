"""The browser universal connector (浏览器万能连接器, 4.7 Q05, policy).

A component a flow step carries when no API or MCP component reaches the system it has to work in: the step does
its work in the built-in browser, on the sites this component names, towards a goal it states, and is finished only
when the page shows what its postconditions say. It is placed while a flow is built or generated -- never swapped in
at run time after another channel's outcome was unknown.

On a step it is one instance in ``nodes[].components``::

    components:
      - instance: browser
        kind: connector
        ref: browser_universal
        version: 1.0.0
        params:
          allowed_domains: [portal.example.com]
          goal: 在客户门户里查到订单的物流单号
          inputs: {order_no: {from: order.inputs.order_no}}
          postconditions: [{kind: text_contains, value: 物流单号}]
          output: {path: out/logistics.md, fields: [物流单号]}
          handoff: {captcha: human, upload: human, login: human, max_steps: 20}
          login: {mode: handoff}
          decider: {jev: true, mode: act}
          allowed_paths: [portal.example.com/orders] # P2 护栏一，可不填：填了才收窄到这一部分
          controls: [查询, 下一页] # P2 护栏一，可不填：填了就只能点这些按钮

What this module owns: the grant (the product browser tools the step's role gets), the parameters and their checks,
and the summary the runtime reads (``workflow_node.connector``). What it does not: the browser, the Gateway and the
approvals -- a connector step is a browser step like any other, only narrowed to its own sites.

Rules the checks keep:
- the sites are named, and are the only ones this step's browser may reach (the order's delegation still has to
  include them: a connector names, it never grants);
- inputs are references to the order's own inputs, never literal values stored in the package;
- at least one postcondition, checked by code on the page -- "the model says done" is not done;
- the output lands in a file the step promised (one of its deliverables);
- a login is handed to a person (keeping logins per customer and site is decided, not yet built: B7/B8).
"""
from __future__ import annotations

import re
from typing import Any

from ampuly.computer_use.download_safety import DEFAULT_SAFE_TYPES

CONNECTOR_KIND = "connector"
CONNECTOR_REF = "browser_universal"
CONNECTOR_VERSION = "1.0.0"
CONNECTOR_NAME_ZH = "浏览器万能连接器"
CONNECTOR_NAME_EN = "Browser universal connector"

# What a step carrying the connector may do in its browser: look, move, fill a draft, choose, ask a person, and
# propose a submit (which the Gateway still sends for approval). No screenshots, no vision, no file-writing snapshot.
CONNECTOR_TOOLS: tuple[str, ...] = (
    "browser_navigate", "browser_observe", "browser_click", "browser_type", "browser_select_option", "browser_scroll",
    "browser_wait_for", "browser_navigate_back", "browser_switch_tab", "browser_submit_enter", "browser_request_human",
    # B3: PULY (JEV) picks the next low-risk steps; the step's own model still types and proposes every submit.
    "browser_decide_act",
)

POSTCONDITION_KINDS = ("url_contains", "text_contains", "element", "file_exists")
# B5: what a file_exists condition may name -- a type a download may be saved as, or any.
FILE_TYPES = frozenset({*DEFAULT_SAFE_TYPES, "*", "any", "任意"})
HANDOFF_KEYS = ("captcha", "upload", "login")
LOGIN_MODES = ("handoff",)
RESERVED_LOGIN_MODES = ("customer_site",)
DECIDER_MODES = ("act", "suggest")
MAX_STEPS_DEFAULT = 20
MAX_STEPS_LIMIT = 40
_INPUT_FROM = re.compile(r"^order\.inputs\.[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Bulk P1 (owner ruling ): how the step is proven done. operation (default): something the step did made a
# condition true; lookup: the site shows the order's own key and the answer together (reading IS the work); batch
# (bulk P4): the step works a list from the order and each row is judged on its own. Only the owner sets a step to
# anything but operation -- never master control, never a generated draft: it decides what counts as done.
VERIFY_MODES = ("operation", "lookup", "batch")
VERIFY_KEYS = frozenset({"mode", "key", "no_result", "extract", "items", "item_key"})
# How a condition names the row it is about: 「已发布 {item.标题}」. Filled by code from the order's own row, never
# by the model, and only on a batch step.
_ITEM_FIELD = re.compile(r"\{item\.([^{}]{1,64})\}")
NO_RESULT_LIMIT = 10
LABEL_MAX = 40
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})*$")


def is_connector(item: Any) -> bool:
    return isinstance(item, dict) and item.get("kind") == CONNECTOR_KIND and item.get("ref") == CONNECTOR_REF


def connector_of(node: dict[str, Any]) -> dict[str, Any] | None:
    """The step's browser connector instance, or None."""
    for item in list(node.get("components", []) or []):
        if is_connector(item):
            return item
    return None


def clean_domain(raw: Any) -> str:
    """A host as the browser registry matches it: lower case, no scheme, port, path or leading dot; '' if not a host."""
    text = str(raw or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in text:
        return ""
    text = text.rsplit(":", 1)[0] if text.count(":") == 1 else text
    text = text.strip(".")
    return text if _DOMAIN.match(text) else ""


def clean_address(raw: Any) -> str:
    """An allow-list entry as the gates match it: ``host`` or ``host/path``; ``''`` if it is not a site.

    P2 护栏一 let an entry narrow to part of a site. :func:`clean_domain` drops the path, which is right for the
    rules that are still host-only and wrong for the entry itself -- writing the path and having it silently
    dropped would be worse than refusing it.
    """
    from ampuly.computer_use.web_scope import split_entry

    host, path = split_entry(raw)
    if not _DOMAIN.match(host):
        return ""
    return f"{host}{path}" if path else host


def host_allowed(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    """The registry's rule (browser_sessions.check_navigation): the domain itself or any subdomain of it."""
    host = str(host or "").strip().lower().rstrip(".")
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def _distribution_actions() -> tuple[str, ...]:
    """What a batch step may declare it does (D1): upload / publish / look_back -- the distribution authorization's own
    list (business_store.DISTRIBUTION_ACTIONS), so the step and the authorization can never name different moves."""
    from ampuly.plugins.office_ui.business_store import DISTRIBUTION_ACTIONS

    return DISTRIBUTION_ACTIONS


def connector_findings(node: dict[str, Any]) -> list[tuple[str, str, str]]:
    """What is wrong with a step's browser connector, as (field, code, message). Empty when there is none."""
    items = [item for item in list(node.get("components", []) or []) if isinstance(item, dict) and item.get("kind") == CONNECTOR_KIND]
    if not items:
        return []
    found: list[tuple[str, str, str]] = []
    if len(items) > 1:
        found.append(("components", "connector_twice", "a step carries one browser connector; put the second site's work in its own step"))
    item = items[0]
    if item.get("ref") != CONNECTOR_REF:
        return [*found, ("components", "connector_unknown", f"connector {item.get('ref')!r} is not one this console has")]
    version = str(item.get("version", "") or "")
    if version and version != CONNECTOR_VERSION:
        found.append(("components", "connector_version", f"browser connector version {version} is not available (this console has {CONNECTOR_VERSION})"))
    if str(node.get("type", "") or "") != "ai_do":
        found.append(("components", "connector_not_ai_step", "the browser connector runs on an AI step; a person's step uses their own browser"))
    params = dict(item.get("params", {}) or {})
    domains = params.get("allowed_domains")
    if not isinstance(domains, list) or not domains:
        found.append(("components", "connector_domains_missing", "name the sites this step works on (allowed_domains)"))
    else:
        bad = [str(d) for d in domains if not clean_domain(d)]
        if bad:
            found.append(("components", "connector_domain_invalid", f"not a site name: {', '.join(bad[:3])}"))
    # P2 护栏一: narrowing to part of a site is opt in. An address here that belongs to no named site would narrow
    # nothing and read as though it did, so it is named rather than ignored.
    paths = params.get("allowed_paths")
    if paths is not None:
        hosts = [h for h in (clean_domain(d) for d in list(domains or []) if isinstance(domains, list)) if h]
        if not isinstance(paths, list):
            found.append(("components", "connector_paths_invalid",
                          "allowed_paths narrows a site to part of it, as addresses: portal.example.com/creator"))
        else:
            unusable = [str(x) for x in paths if "/" not in clean_address(x)]
            if unusable:
                found.append(("components", "connector_paths_invalid",
                              f"an address with a path, not a site name: {', '.join(unusable[:3])}"))
            stray = [str(x) for x in paths if (h := clean_address(x).split("/", 1)[0])
                     and "/" in clean_address(x) and not any(h == d or h.endswith("." + d) for d in hosts)]
            if stray:
                found.append(("components", "connector_paths_unlisted",
                              f"narrows a site this step never named: {', '.join(stray[:3])}"))
    # P2 护栏一: a step may declare which buttons it is allowed to click. Optional -- leaving it out is what every
    # step does today and means the click rules are unchanged. Declaring it is a promise the runtime holds the
    # step to, so an empty or malformed declaration is a mistake to name, never a silent 「什么都不准点」.
    controls = params.get("controls")
    if controls is not None:
        if not isinstance(controls, list) or any(not str(c or "").strip() for c in controls):
            found.append(("components", "connector_controls_invalid",
                          "controls lists the buttons this step may click, as they read on the page"))
        elif not controls:
            found.append(("components", "connector_controls_empty",
                          "an empty controls list means the step may click nothing; name the buttons, or leave the field out"))
        elif long := [str(c) for c in controls if len(str(c).strip()) > 80]:
            found.append(("components", "connector_controls_long",
                          f"a button's name, not a sentence: {', '.join(c[:20] for c in long[:3])}"))
    if not str(params.get("goal", "") or "").strip():
        found.append(("components", "connector_goal_missing", "say what the step has to get done on the site (goal)"))
    inputs = params.get("inputs", {})
    if inputs not in (None, {}):
        if not isinstance(inputs, dict):
            found.append(("components", "connector_inputs_invalid", "inputs map a name to {from: order.inputs.<name>}"))
        else:
            for name, source in inputs.items():
                ref = source.get("from") if isinstance(source, dict) else None
                if not isinstance(ref, str) or not _INPUT_FROM.match(ref) or set(source) - {"from"}:
                    found.append(("components", "connector_input_not_reference",
                                  f"input {name!r} must be a reference to the order's inputs ({{from: order.inputs.<name>}}), never a value stored in the flow"))
    posts = params.get("postconditions")
    if not isinstance(posts, list) or not posts:
        found.append(("components", "connector_postconditions_missing",
                      "state at least one thing the page must show when the work is done (postconditions): the model saying done is not done"))
    else:
        for index, post in enumerate(posts, 1):
            if (not isinstance(post, dict) or post.get("kind") not in POSTCONDITION_KINDS or set(post) - {"kind", "value"}
                    or not str(post.get("value", "") or "").strip()):
                found.append(("components", "connector_postcondition_invalid",
                              f"postcondition {index} must be {{kind: {'|'.join(POSTCONDITION_KINDS)}, value}}"))
            elif post["kind"] == "file_exists" and str(post["value"]).strip().lower().lstrip(".") not in FILE_TYPES:
                # B5: a download is named by its type -- the product saves it as download-<digest>.<type>; a page's
                # file name never reaches the check or the evidence.
                found.append(("components", "connector_file_type_invalid",
                              f"postcondition {index}: a downloaded file is named by its type (pdf, xlsx, csv ... or * for any)"))
    found.extend(_verify_findings(params))
    # D1 (policy): a batch step says which of 上传 / 发布 / 回查 it does, so the order's distribution
    # authorization can be held to it. Leaving it out is allowed at save (such a step is refused at launch instead);
    # a value that is not one of the three is a mistake to name, never read as 「没选」.
    action = params.get("distribution_action")
    if action not in (None, "") and action not in _distribution_actions():
        found.append(("components", "connector_distribution_action_invalid",
                      "distribution_action is what a batch step does with each row: upload, publish or look_back"))
    output = params.get("output")
    deliverables = [str(x) for x in list(node.get("deliverables", []) or [])]
    if not isinstance(output, dict) or not str(output.get("path", "") or "").strip():
        found.append(("components", "connector_output_missing", "say which of the step's deliverables the result is written to (output.path)"))
    elif str(output["path"]).strip() not in deliverables:
        found.append(("components", "connector_output_not_deliverable", f"output.path {output['path']!r} is not one of this step's deliverables"))
    handoff = params.get("handoff", {}) or {}
    if not isinstance(handoff, dict):
        found.append(("components", "connector_handoff_invalid", "handoff is {captcha, upload, login: human, max_steps}"))
    else:
        for key in HANDOFF_KEYS:
            if key in handoff and handoff[key] != "human":
                found.append(("components", "connector_handoff_invalid", f"handoff.{key} can only be human: a captcha, an upload or a login is done by a person"))
        steps = handoff.get("max_steps", MAX_STEPS_DEFAULT)
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEPS_LIMIT:
            found.append(("components", "connector_max_steps_invalid", f"handoff.max_steps is a whole number from 1 to {MAX_STEPS_LIMIT}"))
    login = params.get("login", {}) or {}
    mode = login.get("mode", "handoff") if isinstance(login, dict) else None
    if mode in RESERVED_LOGIN_MODES:
        found.append(("components", "connector_login_not_enabled",
                      "keeping a customer's login per site is decided but not built yet (登录保留未启用); a person logs in (handoff)"))
    elif mode not in LOGIN_MODES:
        found.append(("components", "connector_login_invalid", "login.mode is handoff"))
    decider = params.get("decider", {}) or {}
    if not isinstance(decider, dict) or not isinstance(decider.get("jev", True), bool) or decider.get("mode", "act") not in DECIDER_MODES:
        found.append(("components", "connector_decider_invalid", f"decider is {{jev: true|false, mode: {'|'.join(DECIDER_MODES)}}}"))
    return found


def _verify_findings(params: dict[str, Any]) -> list[tuple[str, str, str]]:
    """What is wrong with a connector's verification settings (bulk P1)."""
    found: list[tuple[str, str, str]] = []
    verify = params.get("verify", {}) or {}
    posts = [p for p in list(params.get("postconditions", []) or []) if isinstance(p, dict)]
    templated = [p for p in posts if "{key}" in str(p.get("value", "") or "")]
    item_templated = [p for p in posts if _ITEM_FIELD.search(str(p.get("value", "") or ""))]
    if not isinstance(verify, dict) or set(verify) - VERIFY_KEYS or verify.get("mode", "operation") not in VERIFY_MODES:
        return [("components", "connector_verify_invalid",
                 "verify is {mode: operation|lookup|batch, key: {from: order.inputs.<name>}, no_result: [...], "
                 "extract: {field: label}, items: {from: order.inputs.<name>}, item_key: <field>}")]
    mode = str(verify.get("mode", "operation") or "operation")
    if mode == "batch":
        return [*found, *_batch_findings(verify, params, posts, item_templated)]
    if item_templated:
        found.append(("components", "connector_item_outside_batch",
                      "{item.…} in a condition is filled in only for a batch step"))
    if mode != "lookup":
        if templated:
            found.append(("components", "connector_key_outside_lookup", "{key} in a condition is filled in only for a lookup step"))
        return found
    key = verify.get("key")
    ref = key.get("from") if isinstance(key, dict) else None
    inputs = dict(params.get("inputs", {}) or {}) if isinstance(params.get("inputs"), dict) else {}
    declared = {str(dict(source).get("from", "")) for source in inputs.values() if isinstance(source, dict)}
    if not isinstance(ref, str) or not _INPUT_FROM.match(ref) or ref not in declared or set(key) - {"from"}:
        found.append(("components", "connector_lookup_key_missing",
                      "a lookup names its key: one of this step's order inputs ({from: order.inputs.<name>}), e.g. the customer's order number"))
    if not any(p.get("kind") in {"text_contains", "element"} and "{key}" in str(p.get("value", "") or "") for p in posts):
        found.append(("components", "connector_lookup_key_not_shown",
                      "a lookup states that the page shows the key: at least one page condition containing {key}"))
    if any(p.get("kind") == "file_exists" for p in posts):
        found.append(("components", "connector_lookup_no_downloads", "a download is not a lookup's evidence; use an operation step"))
    output = params.get("output") if isinstance(params.get("output"), dict) else {}
    fields = [str(f) for f in list(output.get("fields", []) or []) if str(f).strip()]
    if not fields:
        found.append(("components", "connector_lookup_answer_missing",
                      "a lookup names the answer it takes off the page (output.fields, e.g. 物流单号)"))
    no_result = verify.get("no_result", [])
    if not isinstance(no_result, list) or len(no_result) > NO_RESULT_LIMIT or any(
            not isinstance(p, str) or not p.strip() or len(p) > LABEL_MAX for p in no_result):
        found.append(("components", "connector_lookup_no_result_invalid",
                      f"no_result is at most {NO_RESULT_LIMIT} phrases of at most {LABEL_MAX} characters (e.g. 未找到)"))
    extract = verify.get("extract", {})
    if not isinstance(extract, dict) or any(str(f) not in fields or not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX
                                            for f, label in extract.items()):
        found.append(("components", "connector_lookup_extract_invalid",
                      "extract maps an answer field (one of output.fields) to the label the page writes before it"))
    return found


def _batch_findings(verify: dict[str, Any], params: dict[str, Any], posts: list[dict[str, Any]],
                    item_templated: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """What is wrong with a batch step's settings (bulk P4).

    A batch step's whole claim is 「这一条做完了」, so it must name where the list comes from, which field of a row
    is that row's identity, and at least one page condition that says the row's own words. Without the last one
    every row would be proved by the same evidence, which proves nothing about any of them.
    """
    from ampuly.computer_use.browser_bridge import ITEM_FIELD_MAX, ORDER_ITEMS_LIMIT

    found: list[tuple[str, str, str]] = []
    items = verify.get("items")
    ref = items.get("from") if isinstance(items, dict) else None
    inputs = dict(params.get("inputs", {}) or {}) if isinstance(params.get("inputs"), dict) else {}
    declared = {str(dict(source).get("from", "")) for source in inputs.values() if isinstance(source, dict)}
    if not isinstance(ref, str) or not _INPUT_FROM.match(ref) or ref not in declared or set(items) - {"from"}:
        found.append(("components", "connector_batch_items_missing",
                      "a batch step names its list: one of this step's order inputs ({from: order.inputs.<name>}), "
                      f"a list of at most {ORDER_ITEMS_LIMIT} rows"))
    item_key = verify.get("item_key")
    if not isinstance(item_key, str) or not item_key.strip() or len(item_key) > ITEM_FIELD_MAX:
        found.append(("components", "connector_batch_item_key_missing",
                      "a batch step names which field of a row is that row's identity (item_key), e.g. 剧集编号"))
    if not item_templated:
        found.append(("components", "connector_batch_item_not_shown",
                      "a batch step states what the page shows for the row it is on: at least one page condition "
                      "containing {item.<field>}"))
    if any("{key}" in str(p.get("value", "") or "") for p in posts):
        found.append(("components", "connector_key_outside_lookup", "{key} in a condition is filled in only for a lookup step"))
    if set(verify) & {"key", "no_result", "extract"}:
        found.append(("components", "connector_batch_lookup_keys",
                      "key / no_result / extract belong to a lookup step; a batch step names items and item_key"))
    return found


def connector_summary(node: dict[str, Any]) -> dict[str, Any] | None:
    """What the running step reads (``workflow_node.connector``): the connector's parameters, normalised. None without one."""
    item = connector_of(node)
    if item is None:
        return None
    params = dict(item.get("params", {}) or {})
    handoff = dict(params.get("handoff", {}) or {})
    decider = dict(params.get("decider", {}) or {})
    output = dict(params.get("output", {}) or {})
    verify = _verify_summary(params)
    action = params.get("distribution_action")
    return {
        "ref": CONNECTOR_REF,
        "version": str(item.get("version", "") or CONNECTOR_VERSION),
        "allowed_domains": sorted({d for d in (clean_domain(x) for x in list(params.get("allowed_domains", []) or [])) if d}),
        # P2 护栏一: the addresses this step narrows to, when it narrows at all. Absent or empty means the whole
        # site, which is what every step said before this field existed.
        "allowed_paths": sorted({a for a in (clean_address(x) for x in list(params.get("allowed_paths", []) or [])) if "/" in a}),
        "goal": str(params.get("goal", "") or "").strip(),
        "inputs": {str(name): {"from": str(dict(source).get("from", ""))} for name, source in dict(params.get("inputs", {}) or {}).items()
                   if isinstance(source, dict)},
        "postconditions": [{"kind": str(p.get("kind", "")), "value": str(p.get("value", ""))}
                           for p in list(params.get("postconditions", []) or []) if isinstance(p, dict)],
        "output": {"path": str(output.get("path", "") or ""), "fields": [str(f) for f in list(output.get("fields", []) or [])]},
        "handoff": {"captcha": "human", "upload": "human", "login": "human",
                    "max_steps": int(handoff.get("max_steps", MAX_STEPS_DEFAULT) or MAX_STEPS_DEFAULT)},
        "login": {"mode": "handoff"},
        # policy: a step with this connector may ask JEV by default.
        "decider": {"jev": decider.get("jev", True) is not False, "mode": str(decider.get("mode", "act") or "act")},
        "verify": verify,
        # D1 (policy): what a batch step does with each row, read by the distribution authorization
        # check at launch and on every read of the list. Only on a batch step that declared one of the three: absent
        # is 「没选」, which that check refuses.
        **({"distribution_action": action}
           if verify["mode"] == "batch" and isinstance(action, str) and action in _distribution_actions() else {}),
        # P2 护栏一: only present when the step actually declared buttons. Absent and empty mean different things
        # to the runtime -- absent is 「没声明」, empty is 「一个都不准点」 -- so an absent field must stay absent.
        **({"controls": [str(c).strip() for c in list(params.get("controls") or []) if str(c).strip()]}
           if isinstance(params.get("controls"), list) else {}),
    }


def _verify_summary(params: dict[str, Any]) -> dict[str, Any]:
    verify = params.get("verify", {}) if isinstance(params.get("verify"), dict) else {}
    mode = str(verify.get("mode", "operation") or "operation")
    if mode == "batch":
        items = verify.get("items") if isinstance(verify.get("items"), dict) else {}
        return {"mode": "batch", "items": {"from": str(items.get("from", "") or "")},
                "item_key": str(verify.get("item_key", "") or "")}
    if mode != "lookup":
        return {"mode": "operation"}
    key = verify.get("key") if isinstance(verify.get("key"), dict) else {}
    return {
        "mode": "lookup",
        "key": {"from": str(key.get("from", "") or "")},
        "no_result": [str(p) for p in list(verify.get("no_result", []) or []) if str(p).strip()][:NO_RESULT_LIMIT],
        "extract": {str(f): str(label) for f, label in dict(verify.get("extract", {}) or {}).items()},
    }


def bind_order_inputs(ws_handler: Any) -> None:
    """Bulk P1: the kernel asks for an order input's value (a lookup's key); this reads it from the business record --
    the order the task works for, in its project -- never from the prompt or a page."""
    from ampuly.computer_use import browser_bridge

    def resolve(task: Any, name: str) -> str | None:
        metadata = dict(getattr(task, "metadata", {}) or {})
        order_id = str(metadata.get("order_id", "") or "").strip()
        if not order_id:
            return None
        project = str(getattr(task, "project_id", "") or "").strip() or "default"
        order = ws_handler._console_api().business.get_order(order_id, project_id=project)
        if order is None:
            return None
        value = dict(getattr(order, "fields", {}) or {}).get(name)
        if value is None or isinstance(value, (dict, list, bool)) or not str(value).strip():
            return None
        return str(value)

    browser_bridge.bind_order_input_resolver(resolve)

    def resolve_items(task: Any, name: str) -> list[Any] | None:
        """Bulk P4: the rows of a batch step's list, from the same business record and nowhere else.

        A list is refused where a single value is (``resolve`` above rejects a list on purpose); this is its
        sibling, and it accepts ONLY a list. The kernel bounds and flattens what comes back.

        LP2-A: and only the rows the order's live distribution authorization names (:func:`authorized_rows`),
        checked again on every read -- not only at launch. D1: against this very step's sites and declared action,
        as the running step carries them (:func:`batch_step_of_task`).
        """
        metadata = dict(getattr(task, "metadata", {}) or {})
        order_id = str(metadata.get("order_id", "") or "").strip()
        if not order_id:
            return None
        project = str(getattr(task, "project_id", "") or "").strip() or "default"
        business = ws_handler._console_api().business
        order = business.get_order(order_id, project_id=project)
        if order is None:
            return None
        value = dict(getattr(order, "fields", {}) or {}).get(name)
        if not isinstance(value, list):
            return None
        return authorized_rows(business, order, project, value, browser_bridge.batch_item_key(task),
                               step=batch_step_of_task(task))

    browser_bridge.bind_order_items_resolver(resolve_items)


def batch_step_of_task(task: Any) -> dict[str, Any]:
    """D1: the batch step a task runs, as the distribution check reads it -- its name, the sites it works on and the
    action it declared ('' when none) -- from the connector the running step carries (``workflow_node.connector``,
    :func:`connector_summary` as projected), the same record the kernel reads its list and item key from."""
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node") if task is not None else None
    node = node if isinstance(node, dict) else {}
    connector = node.get("connector") if isinstance(node.get("connector"), dict) else {}
    action = connector.get("distribution_action")
    # The step's name only, never its id: the name ends up in the owner's sentence ('' reads 「这一步」).
    return {"name": str(node.get("name", "") or "").strip(),
            "sites": [host for host in (clean_domain(x) for x in list(connector.get("allowed_domains") or [])) if host],
            "action": action if isinstance(action, str) else ""}


# LP2-A: what the item resolver last put on each order's timeline, so a stop is said once, not on every read of the
# list (a batch step reads it on every browser call). Keyed by (project, order); cleared when the list is whole again.
_BATCH_STOPS_SAID: dict[tuple[str, str], tuple[str, str, int]] = {}
_BATCH_STOPS_SAID_MAX = 1000


def authorized_rows(business: Any, order: Any, project_id: str, rows: list[Any], key: str, *,
                    step: dict[str, Any] | None = None) -> list[Any] | None:
    """LP2-A: the rows of a batch list that the order's live distribution authorization names -- or None, and the
    step stops.

    D1 (policy): ``step`` is the batch step reading its list (:func:`batch_step_of_task`), and the
    authorization must cover it -- every site it works on among the channels, its declared action among the actions.
    A step that declared no action, or none given at all, is refused (fail closed): nothing, and the step stops.

    The kernel reads its list again at every row it starts (browser_decide_act(item=...), and each browser call on a
    row), so this runs each time: one local read (distribution_authorization_gate), no model, no network. What it
    means for the owner:

    * revoked or expired (no live authorization), or one that does not cover this step's sites or action -> nothing,
      so the step stops at its next row: the kernel refuses
      the next browser_decide_act(item=...), the closing check of the row under way included. What that row already
      did is not recalled, and any submit of it still goes to the owner's approval inbox as always;
    * a list that grew after the approval -> only the rows the authorization names; the others are not worked;
    * two rows whose names differ only past the kernel's cut -> nothing (they would be one row to the step).

    Each stop is logged and put on the order's timeline once (``_BATCH_STOPS_SAID``).
    """
    from ampuly.computer_use.browser_bridge import ITEM_VALUE_MAX, ORDER_ITEMS_LIMIT
    from ampuly.plugins.office_ui.api import batch_identity_collision, batch_item_identity

    if not key or len(rows) > ORDER_ITEMS_LIMIT or any(not isinstance(row, dict) for row in rows):
        # The kernel refuses such a list whole. Narrowing an over-long list to the rows an authorization names
        # would get round that refusal (「悄悄砍掉分发清单」), so it is refused here before any narrowing.
        return None
    shared = batch_identity_collision(rows, key)
    if shared:
        _say_batch_stop(business, order, project_id, "collision",
                        f"批量发送停在下一条之前，没有再往外发：工单清单里有几条的编号前 {ITEM_VALUE_MAX} 个字一模一样"
                        f"（「{shared[:20]}」开头），批量步骤分不清它们。")
        return None
    identities = [batch_item_identity(row, key) for row in rows]
    gate = business.distribution_authorization_gate(order.order_id, project_id=project_id,
                                                    targets=[identity for identity in identities if identity],
                                                    steps=[step or {}])
    current = gate.get("authorization") if gate.get("ok") or gate.get("reason") == "targets_changed" else None
    if not isinstance(current, dict):
        _say_batch_stop(business, order, project_id, str(gate.get("reason", "") or "refused"),
                        f"批量发送停在下一条之前，没有再往外发（正在做的那一条不撤回）：{gate.get('why_zh', '')}")
        return None
    named = set(current.get("targets") or [])
    kept = [row for row, identity in zip(rows, identities) if identity and identity in named]
    left_out = len(rows) - len(kept)
    if left_out:
        _say_batch_stop(business, order, project_id, "targets_changed",
                        f"工单清单里有 {left_out} 条不在生效的分发授权单里，批量步骤只做授权单里有的那些，这 {left_out} 条没有做。"
                        "要发它们，请撤销旧的授权单，按现在的清单重新起草并批准。",
                        authorization_id=str(current.get("authorization_id", "") or ""), rows_left_out=left_out)
    else:
        _BATCH_STOPS_SAID.pop((project_id, order.order_id), None)
    return kept or None


def _say_batch_stop(business: Any, order: Any, project_id: str, reason: str, why_zh: str, *,
                    authorization_id: str = "", rows_left_out: int = 0) -> None:
    """Log the stop and put it on the order's timeline, once per change. Never raises: the stop does not wait on it."""
    from loguru import logger

    slot = (project_id, str(order.order_id))
    said = (reason, authorization_id, rows_left_out)
    if _BATCH_STOPS_SAID.get(slot) == said:
        return
    if len(_BATCH_STOPS_SAID) >= _BATCH_STOPS_SAID_MAX:
        _BATCH_STOPS_SAID.clear()
    _BATCH_STOPS_SAID[slot] = said
    logger.warning("工单 {} 的批量步骤：{}", order.order_id, why_zh)
    note = getattr(business, "note_distribution_batch_stop", None)
    if not callable(note):
        return
    try:
        note(order.order_id, reason=reason, why_zh=why_zh, authorization_id=authorization_id, rows_left_out=rows_left_out)
    except Exception:  # noqa: BLE001 - the timeline line is a record of the stop, not the stop itself
        logger.opt(exception=True).warning("could not put the batch stop on order {}'s timeline", order.order_id)


# Data phase 1, C6: a connector step reads what its browser downloaded (../steps/<step>/downloads/, read-only to it).
CONNECTOR_FILE_TOOLS: tuple[str, ...] = ("file_read", "list_dir")


def connector_tools(node: dict[str, Any]) -> list[str]:
    """The product tools this step's role gets for it (merged into the role at projection, never node.tools): the
    browser tools, and the two file tools that read its downloads."""
    return [*CONNECTOR_TOOLS, *CONNECTOR_FILE_TOOLS] if connector_of(node) is not None else []


def connector_params(node: dict[str, Any]) -> dict[str, Any] | None:
    """What the builder shows and edits of a step's connector: its parameters, or None without one (Q05 B4)."""
    item = connector_of(node)
    return dict(item.get("params", {}) or {}) if item is not None else None


def apply_connector_draft(draft: dict[str, Any], merged: dict[str, Any]) -> None:
    """Turn the builder's `connector` field into the step's connector instance, after the bare lists are settled.

    The canvas edits one field -- the parameters, or null to take the connector off the step -- and never the whole
    list of instances; a draft that does not carry the field leaves the stored connector as it is. The instance keeps
    its name and version; the parameters are checked by the save (connector_findings) like any other.
    """
    if "connector" not in draft:
        return
    from ampuly.plugins.office_ui.node_components import derived_components, normalize_components

    value = draft.get("connector")
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"node {merged.get('id', '')}: connector must be the connector's parameters, or null")
    current = merged.get("components")
    base = list(current) if isinstance(current, list) else derived_components(merged)
    before = next((item for item in base if isinstance(item, dict) and item.get("kind") == CONNECTOR_KIND), None)
    kept = [item for item in base if not (isinstance(item, dict) and item.get("kind") == CONNECTOR_KIND)]
    if value:
        kept.append({"instance": str((before or {}).get("instance", "") or "browser"), "kind": CONNECTOR_KIND, "ref": CONNECTOR_REF,
                     "version": str((before or {}).get("version", "") or ""), "params": dict(value)})
    if value or isinstance(current, list):
        merged["components"] = normalize_components(str(merged.get("id", "") or ""), kept)
    else:
        merged.pop("components", None)


def library_entry() -> dict[str, Any]:
    """The component library's card for the connector (a built-in: nothing to install, no key of its own)."""
    return {
        "id": CONNECTOR_REF,
        "kind": CONNECTOR_KIND,
        "name_zh": CONNECTOR_NAME_ZH,
        "name_en": CONNECTOR_NAME_EN,
        "version": CONNECTOR_VERSION,
        "access_mode": "browser",
        "built_in": True,
        "summary_zh": "API、MCP 组件够不着的系统，用内置浏览器去做：只在指定网站上操作，目标写明，完成与否由页面上的条件核验；"
                      "提交、发送、支付、删除仍需审批，验证码、上传、登录交给人。默认由 PULY（JEV）决定下一步点哪里。",
        "summary_en": "For systems no API or MCP component reaches: the built-in browser works on the named sites only, towards a "
                      "stated goal, and is done only when the page shows the stated conditions. Submits, sends, payments and "
                      "deletions still need approval; captchas, uploads and logins go to a person. PULY (JEV) picks the next step by default.",
        "tools": list(CONNECTOR_TOOLS),
    }
