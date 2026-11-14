"""4.9 opt-in real-model evidence helpers (DeepSeek through the product LLM path).

Nothing here runs unless the caller opts in (``AMPULY_REAL_MODEL=1``) and the
product provider has credentials. The key is resolved only by ``LLMProvider``
from the product ``llm_config.yaml`` (``api_key_env``) or the process / Windows
user environment; this module never reads, prints, logs or returns it.

Three bounded uses, each one model call with a small prompt and a hard
``max_tokens`` cap:

- ``compose_no_flow_body``: Q2 no-flow direct goal -> body text that then goes
  through ``run_no_flow_direct_chain`` (real workspace file + governed record);
- ``judge_no_flow_intents``: one batched JSON judgment of whether each master
  chat message is a no-flow direct-delivery instruction (compared with the
  heuristic ``parse_no_flow_master_candidate``);
- ``model_goal_canvas_plan``: Q5 goal -> ``GoalDraftPlan`` (``planner`` is
  ``deepseek_model``, ``paid_api`` True) validated before any package write.

Round 2 (A02/A14/A21) adds, still opt-in and bounded:

- ``install_call_cap``: a hard per-run ceiling on provider calls (counts every
  completion the provider sends, including the ones a node trial makes);
- ``plan_multistep_delivery`` / ``compose_step_body``: a no-flow multi-step
  delivery over files read by the product attachment reader;
- ``model_goal_canvas_plan(rich=True)``: nodes with dependencies, capability
  (tool) references, inputs, outputs and acceptance standards, refused when
  any of them is missing or inconsistent instead of being filled in.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REAL_MODEL_ENV = "AMPULY_REAL_MODEL"
REAL_MODEL_PLANNER = "deepseek_model"
DEFAULT_REAL_MODEL = "deepseek/deepseek-chat"
MAX_CALL_TOKENS = 900

_NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_JSON_BLOCK_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


class RealModelRefused(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "refused")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


def real_model_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(REAL_MODEL_ENV, "") or "").strip() == "1"


@dataclass
class UsageMeter:
    """Counts calls and tokens only (never content or credentials)."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    models: list[str] = field(default_factory=list)

    def add(self, result: dict[str, Any]) -> None:
        usage = dict(result.get("usage") or {})
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        model = str(result.get("model") or "")
        if model and model not in self.models:
            self.models.append(model)

    def to_payload(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "models": list(self.models),
        }


def load_product_llm_config(ampuly_home: str | Path) -> Any:
    """Read ``<ampuly_home>/config/llm_config.yaml`` into ``LLMConfig`` (key fields untouched)."""
    from ampuly.core.config import LLMConfig

    path = Path(ampuly_home) / "config" / "llm_config.yaml"
    if not path.is_file():
        raise RealModelRefused("llm_config_missing", str(path))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RealModelRefused("llm_config_unreadable", type(exc).__name__) from exc
    return LLMConfig(**dict(data.get("llm") or {}))


def product_llm_provider(ampuly_home: str | Path, *, model: str = "") -> Any:
    """Build the product ``LLMProvider`` from the product config; refuse without credentials."""
    from ampuly.llm.provider import LLMProvider

    config = load_product_llm_config(ampuly_home)
    if model:
        config = config.model_copy(update={"default_model": model})
    provider = LLMProvider(config, ampuly_home=Path(ampuly_home))
    if not provider.has_credentials():
        raise RealModelRefused("key_not_set", str(config.api_key_env or ""))
    return provider


async def _ask(llm: Any, system: str, user: str, *, meter: UsageMeter | None, max_tokens: int) -> str:
    result = await llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        max_tokens=min(int(max_tokens), MAX_CALL_TOKENS),
    )
    if meter is not None:
        meter.add(result)
    return str(result.get("content") or "")


def _extract_json(text: str) -> Any:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        return json.loads(raw)
    except ValueError:
        match = _JSON_BLOCK_RE.search(raw)
        if not match:
            raise RealModelRefused("model_json_missing")
        try:
            return json.loads(match.group(0))
        except ValueError as exc:
            raise RealModelRefused("model_json_invalid") from exc


# --------------------------------------------------------------------- Q2 body

async def compose_no_flow_body(
    llm: Any, goal: str, *, meter: UsageMeter | None = None, max_tokens: int = 400,
) -> str:
    clean = " ".join(str(goal or "").split())
    if not clean:
        raise RealModelRefused("goal_empty")
    body = await _ask(
        llm,
        "You write short, plain deliverable files for a small business owner. "
        "Return only the file body, no preamble, at most 200 words.",
        f"Goal: {clean[:400]}",
        meter=meter,
        max_tokens=max_tokens,
    )
    if not body.strip():
        raise RealModelRefused("model_body_empty")
    return body.strip() + "\n"


# ------------------------------------------------------------------- Q2 intent

async def judge_no_flow_intents(
    llm: Any, messages: list[str], *, meter: UsageMeter | None = None, max_tokens: int = 300,
) -> list[bool]:
    """One batched call: is each message an instruction to write/deliver given content to a named file now?"""
    items = [str(m or "")[:300] for m in messages]
    if not items or len(items) > 30:
        raise RealModelRefused("intent_batch_size_invalid", str(len(items)))
    numbered = "\n".join(f"{i}. {json.dumps(text, ensure_ascii=False)}" for i, text in enumerate(items))
    reply = await _ask(
        llm,
        "Classify owner chat messages. A message is DELIVERY only if it instructs the assistant to "
        "write/save/deliver content that the message itself supplies into a named file right now. "
        "Questions, plans, requests to think, or messages without a target file name are NOT delivery. "
        f'Answer with JSON only: {{"labels": [true|false, ...]}} with exactly {len(items)} booleans '
        "in input order (index 0 first).",
        numbered,
        meter=meter,
        max_tokens=max_tokens,
    )
    data = _extract_json(reply)
    labels = data.get("labels") if isinstance(data, dict) else data
    if isinstance(labels, dict):  # {"0": true, ...} or {"1": true, ...}
        try:
            keyed = {int(k): v for k, v in labels.items()}
        except (TypeError, ValueError):
            keyed = {}
        base = 0 if 0 in keyed else 1
        labels = [keyed.get(base + i) for i in range(len(items))] if keyed else labels
    if not isinstance(labels, list) or len(labels) != len(items):
        shape = f"{type(labels).__name__}:{len(labels) if hasattr(labels, '__len__') else '-'}"
        raise RealModelRefused("intent_labels_shape_invalid", f"{shape} expected={len(items)} reply={str(reply)[:160]!r}")
    return [_label_true(x) for x in labels]


def _label_true(value: Any) -> bool:
    if isinstance(value, dict):
        value = next((value[k] for k in ("label", "delivery", "is_delivery", "value") if k in value), False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().lower() in {"true", "yes", "delivery", "1"}


def precision_recall(predicted: list[bool], gold: list[bool]) -> dict[str, Any]:
    tp = sum(1 for p, g in zip(predicted, gold) if p and g)
    fp = sum(1 for p, g in zip(predicted, gold) if p and not g)
    fn = sum(1 for p, g in zip(predicted, gold) if not p and g)
    tn = sum(1 for p, g in zip(predicted, gold) if not p and not g)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall}


# ------------------------------------------------------------------ Q5 planner

TRIAL_TOOLS = ("file_read", "file_write")


def plan_from_model_payload(
    data: Any, *, role_id: str, goal: str, rich: bool = False, allowed_tools: tuple[str, ...] = TRIAL_TOOLS,
) -> Any:
    """Validate a model JSON plan into ``GoalDraftPlan`` (refuses instead of guessing)."""
    from ampuly.plugins.office_ui.goal_canvas_draft import GoalDraftPlan

    if not isinstance(data, dict):
        raise RealModelRefused("plan_not_object")
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not (2 <= len(raw_nodes) <= 6):
        raise RealModelRefused("plan_nodes_count_invalid")
    nodes: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise RealModelRefused("plan_node_not_object")
        node_id = str(raw.get("id") or "").strip().lower()
        if not _NODE_ID_RE.match(node_id) or node_id in seen:
            raise RealModelRefused("plan_node_id_invalid", node_id)
        seen.add(node_id)
        name = str(raw.get("name") or "").strip()[:40]
        duty = str(raw.get("responsibility") or "").strip()[:200]
        if not name or not duty:
            raise RealModelRefused("plan_node_text_missing", node_id)
        deliverables = [
            str(d).strip() for d in list(raw.get("deliverables") or [])
            if str(d).strip() and not str(d).strip().startswith(("/", "\\")) and ".." not in str(d)
            and ":" not in str(d)
        ][:3]
        if not deliverables:
            if rich:
                raise RealModelRefused("plan_node_outputs_missing", node_id)
            deliverables = [f"drafts/{node_id}.md"]
        node: dict[str, Any] = {
            "id": node_id,
            "name": name,
            "type": "ai_do",
            "role": role_id,
            "responsibility": duty,
            "deliverables": deliverables,
        }
        if rich:
            node.update(_rich_node_fields(raw, node_id, nodes, allowed_tools, notes))
        nodes.append(node)
    clean_goal = " ".join(str(goal or "").split())
    preview = clean_goal if len(clean_goal) <= 48 else clean_goal[:47] + "…"
    name = str(data.get("name") or "").strip()[:60] or f"目标：{preview}"
    summary = str(data.get("summary") or "").strip()[:200] or "Q5 model draft from goal"
    plan = GoalDraftPlan(
        name=name,
        summary=summary,
        intake_fields=[
            {"id": "goal", "label": "目标", "required": True},
            {"id": "materials", "label": "材料摘要", "required": False},
        ],
        nodes=nodes,
        planner=REAL_MODEL_PLANNER,
        paid_api=True,
    )
    plan.model_notes = notes  # what validation dropped (never what it invented: it invents nothing)
    return plan


def _rich_node_fields(
    raw: dict[str, Any], node_id: str, earlier: list[dict[str, Any]], allowed_tools: tuple[str, ...], notes: list[str],
) -> dict[str, Any]:
    """Dependencies, capability references, inputs and acceptance as the model gave them, checked, never filled in."""
    known = {str(n["id"]): n for n in earlier}
    depends = []
    for dep in list(raw.get("depends_on") or []):
        dep_id = str(dep or "").strip().lower()
        if dep_id not in known:
            raise RealModelRefused("plan_node_depends_unknown", f"{node_id}->{dep_id}")
        if dep_id not in depends:
            depends.append(dep_id)
    if earlier and not depends:
        raise RealModelRefused("plan_node_disconnected", node_id)
    tools = []
    for tool in list(raw.get("tools") or []):
        name = str(tool or "").strip()
        if name in allowed_tools and name not in tools:
            tools.append(name)
        elif name and name not in allowed_tools:
            notes.append(f"{node_id}: tool {name!r} not in {list(allowed_tools)}")
    if "file_write" not in tools:
        raise RealModelRefused("plan_node_tools_missing", node_id)
    upstream = {d for dep in depends for d in known[dep].get("deliverables", [])}
    inputs = []
    for item in list(raw.get("inputs") or []):
        path = str(item or "").strip()
        if path in upstream and path not in inputs:
            inputs.append(path)
        elif path:
            notes.append(f"{node_id}: input {path!r} is no upstream output, dropped")
    if depends and not inputs:
        raise RealModelRefused("plan_node_inputs_missing", node_id)
    if inputs and "file_read" not in tools:
        raise RealModelRefused("plan_node_reads_without_file_read", node_id)
    standard = [str(s).strip()[:160] for s in list(raw.get("standard") or raw.get("acceptance") or []) if str(s).strip()][:4]
    if not standard:
        raise RealModelRefused("plan_node_standard_missing", node_id)
    return {
        "depends_on": depends,
        "tools": tools,
        "skills": [],
        "writes": [],
        "inputs": inputs,
        "standard": standard,
        "gate": "none",
    }


async def model_goal_canvas_plan(
    llm: Any, goal: str, materials_digest: str = "", role_id: str = "worker", *,
    meter: UsageMeter | None = None, max_tokens: int = 700, rich: bool = False, materials_text: str = "",
    allowed_tools: tuple[str, ...] = TRIAL_TOOLS,
) -> Any:
    clean = " ".join(str(goal or "").split())
    if not clean:
        raise RealModelRefused("goal_empty")
    if rich:
        system = (
            "You draft a small workflow canvas for an AI employee from the owner's goal and materials. "
            "Return JSON only: {\"name\": str, \"summary\": str, \"nodes\": [{\"id\": snake_case, \"name\": str, "
            "\"responsibility\": str, \"depends_on\": [earlier node ids], \"tools\": [tool names], "
            "\"inputs\": [files produced by its depends_on nodes], \"deliverables\": [relative .md file paths it writes], "
            "\"standard\": [1-3 checkable acceptance criteria]}]} with 2 to 4 nodes in order. "
            f"Tools available: {', '.join(allowed_tools)} (file_write is needed to write deliverables, file_read to read inputs). "
            "Every node after the first depends on an earlier node and lists, as inputs, the exact deliverable paths it reads. "
            "The first node works from the goal and materials quoted in its responsibility (it has no input files). "
            "Use the owner's language for names, responsibilities and criteria. No prose outside JSON."
        )
        material = str(materials_text or "").strip()[:1200]
        user = f"Goal: {clean[:500]}\nMaterials:\n{material or 'none'}"
    else:
        system = (
            "You draft a small workflow canvas for an AI employee. Return JSON only: "
            '{"name": str, "summary": str, "nodes": [{"id": snake_case, "name": str, '
            '"responsibility": str, "deliverables": [relative file paths]}]} with 2 to 5 nodes in order. '
            "Use the owner's language for names and responsibilities. No prose outside JSON."
        )
        user = f"Goal: {clean[:400]}\nMaterials digest: {str(materials_digest or 'none')[:64]}"
    reply = await _ask(llm, system, user, meter=meter, max_tokens=max_tokens)
    return plan_from_model_payload(
        _extract_json(reply), role_id=role_id, goal=clean, rich=rich, allowed_tools=allowed_tools,
    )


async def generate_canvas_draft_with_model(
    llm: Any, *, ampuly_home: str | Path, org_source: str | Path, goal: str,
    actor: str = "owner", draft_id: str = "", materials_digest: str = "", message_id: str = "",
    meter: UsageMeter | None = None, rich: bool = False, materials_text: str = "", plan_sink: list | None = None,
) -> Any:
    """Opt-in Q5 path: model plan first, then the same package APIs as the fixture planner."""
    from ampuly.plugins.office_ui.goal_canvas_draft import _first_role_id, generate_canvas_draft_from_goal

    role_id = _first_role_id(Path(org_source))
    plan = await model_goal_canvas_plan(
        llm, goal, materials_digest, role_id, meter=meter, rich=rich, materials_text=materials_text,
        max_tokens=900 if rich else 700,
    )
    if plan_sink is not None:
        plan_sink.append(plan)
    return generate_canvas_draft_from_goal(
        ampuly_home=ampuly_home,
        org_source=org_source,
        goal=goal,
        actor=actor,
        draft_id=draft_id,
        materials_digest=materials_digest,
        message_id=message_id,
        planner=lambda _g, _d, _r: plan,
        allow_paid_planner=True,
    )


# ------------------------------------------------------------ call ceiling

def install_call_cap(llm: Any, cap: int, counter: dict[str, int]) -> None:
    """Refuse any provider completion past ``cap`` (shared ``counter``); counts node-trial calls too.

    Wraps the provider instance's own ``_acompletion`` so every chat / tool turn is counted where it is sent;
    nothing about the request (and never the credential) is looked at.
    """
    original = getattr(llm, "_acompletion", None)
    if original is None:
        raise RealModelRefused("call_cap_unsupported", type(llm).__name__)
    counter.setdefault("calls", 0)

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        if counter["calls"] >= int(cap):
            raise RealModelRefused("call_cap_reached", str(cap))
        counter["calls"] += 1
        return await original(*args, **kwargs)

    llm._acompletion = guarded


# ------------------------------------------------- A02 no-flow multi-step

_STEP_FILE_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff/]{0,60}\.md$")


async def plan_multistep_delivery(
    llm: Any, goal: str, attachments_block: str, *, meter: UsageMeter | None = None, max_tokens: int = 400,
) -> list[dict[str, str]]:
    """The model splits one owner goal into 2-3 delivery steps, each one markdown file (no flow, no nodes)."""
    clean = " ".join(str(goal or "").split())
    if not clean:
        raise RealModelRefused("goal_empty")
    reply = await _ask(
        llm,
        "Plan a short multi-step delivery for a small business owner, done directly without any workflow. "
        'Return JSON only: {"steps": [{"file": "relative/name.md", "purpose": str}]} with 2 or 3 steps in order; '
        "each step writes one markdown file and later steps may build on earlier files. Use only what the attached "
        "files really say; a file marked cut (已截断) was only partly read and a file that could not be read must not "
        "be treated as read. Use the owner's language.",
        f"Goal: {clean[:500]}\n\n{str(attachments_block or '')[:3000]}",
        meter=meter,
        max_tokens=max_tokens,
    )
    data = _extract_json(reply)
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list) or not (2 <= len(steps) <= 3):
        raise RealModelRefused("steps_count_invalid")
    out: list[dict[str, str]] = []
    for raw in steps:
        if not isinstance(raw, dict):
            raise RealModelRefused("step_not_object")
        file = str(raw.get("file") or "").strip().replace("\\", "/")
        purpose = str(raw.get("purpose") or "").strip()[:200]
        if not _STEP_FILE_RE.match(file) or ".." in file or any(s["file"] == file for s in out):
            raise RealModelRefused("step_file_invalid", file)
        if not purpose:
            raise RealModelRefused("step_purpose_missing", file)
        out.append({"file": file, "purpose": purpose})
    return out


async def compose_step_body(
    llm: Any, goal: str, step: dict[str, str], attachments_block: str, previous: list[tuple[str, str]], *,
    meter: UsageMeter | None = None, max_tokens: int = 650,
) -> str:
    """One step's file body from the read files and the earlier steps' files; cites what it uses."""
    earlier = "\n\n".join(f"【已交付：{name}】\n{body[:1200]}" for name, body in previous)
    body = await _ask(
        llm,
        "You write one deliverable markdown file for a small business owner. Return only the file body, at most "
        "300 words. After every fact taken from an attached file, cite it exactly as instructed in the attachment "
        "block (（来自：文件名）). If a file was cut (已截断), say the conclusion covers only the part that was read. "
        "If a file could not be read (a picture without local OCR, a damaged file), say plainly it was not read and "
        "do not guess its content or cite it.",
        f"Goal: {' '.join(str(goal or '').split())[:400]}\nThis step writes {step['file']}: {step['purpose']}\n\n"
        f"{str(attachments_block or '')[:3000]}\n\n{earlier}",
        meter=meter,
        max_tokens=max_tokens,
    )
    if not body.strip():
        raise RealModelRefused("model_body_empty", step["file"])
    return body.strip() + "\n"


def false_citations(body: str, unread_filenames: list[str]) -> list[str]:
    """Unread files the body nevertheless cites as a source (（来自：<name>）)."""
    text = str(body or "")
    hits = []
    for name in unread_filenames:
        stem = name.rsplit(".", 1)[0]
        if re.search(r"(来自|from)\s*[:：]\s*[「『《\"']?" + re.escape(stem), text, re.IGNORECASE):
            hits.append(name)
    return hits


# ---------------------------------------------------------- A21 variants

def plan_signature(plan: Any) -> dict[str, Any]:
    """What a plan does, for comparing variants: nodes, edges, outputs, criteria (text, no ids invented)."""
    nodes = list(getattr(plan, "nodes", []) or [])
    return {
        "node_ids": [str(n.get("id")) for n in nodes],
        "edges": sorted(f"{d}->{n.get('id')}" for n in nodes for d in n.get("depends_on", []) or []),
        "outputs": sorted(str(d) for n in nodes for d in n.get("deliverables", []) or []),
        "text": " ".join(
            [str(n.get("name", "")) + " " + str(n.get("responsibility", "")) + " " + " ".join(n.get("standard", []) or [])
             for n in nodes]
        ),
    }


def plan_difference(before: Any, after: Any) -> dict[str, Any]:
    a, b = plan_signature(before), plan_signature(after)
    ids_a, ids_b = set(a["node_ids"]), set(b["node_ids"])
    union = ids_a | ids_b
    return {
        "added_nodes": sorted(ids_b - ids_a),
        "removed_nodes": sorted(ids_a - ids_b),
        "node_jaccard": round(len(ids_a & ids_b) / len(union), 3) if union else 1.0,
        "edges_changed": a["edges"] != b["edges"],
        "outputs_changed": a["outputs"] != b["outputs"],
        "text_changed": a["text"] != b["text"],
    }


def terms_covered(plan: Any, terms: list[str]) -> dict[str, bool]:
    text = plan_signature(plan)["text"].lower() + " " + " ".join(plan_signature(plan)["outputs"]).lower()
    return {term: term.lower() in text for term in terms}
