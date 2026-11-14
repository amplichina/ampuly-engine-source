Ampuly Engine — 协作图谱（原「推理与执行」观测页）
Implemented: 2026-10-22

Entry
Owner console > top bar > 协作图谱
Direct view: existing engine address + ?view=observatory
Optional task filter: &observe_task=<task id>
The five main navigation sections remain as they were.

What it shows
- Organization: human Owner, AI manager, AI and human employees, reporting lines.
- Projects: sessions/tasks in the active project; participants and recent observed items.
- Business: work orders with status, customer/brand, amount, due date, assignee and acceptance.
- Inspector: selected person's current work, collaboration relations and recent public events.
- Execution drawer: task/phase/search, event replay slider, event detail, references and the
  existing order_timeline history reader.
- An empty role is labelled 未配置; an empty project/order list is an explicit empty state.
  The page never invents a person, an online state, a metric or a relation.

Data
- Organization/roles/employees/runtime teams/seats/work items: existing org_info payload.
- Live AI employee status/tool/task: existing swarmAgents projection (agent_runtime_update).
- Projects/tasks: existing session list. Business: existing work-order summaries + handling.
- Execution: existing ExecutionTraceStore (bounded 600 display events).
- On open, the host requests org_info and list_agents once so a direct ?view=observatory
  load is not blank; it sends no execution, approval or external action.

Rendering and policy
- React Flow + @dagrejs/dagre for the organization graph; Edict-style duty cards for
  projects/business. Both are already product dependencies.
  The 关系 view (collaboration/RelationsGraph.tsx, lazy chunk) adds one runtime
  dependency, react-force-graph-2d (MIT) over force-graph (MIT): force layout, drag,
  zoom and particles flowing along active relations. See RELATIONS_SOURCE_NOTICE.txt
  and THIRD_PARTY_NOTICES.md. Above 40 people, idle branches fold (relations.ts);
  relations past the 40-link cap are counted and shown as 「还有 N 条关系未显示」.
  The 关系 view has three selectable themes (collaboration/RelationsStage.tsx, one lazy
  chunk each): 视界熔炉 FORGE (three.js + bloom), 天工开物 WORKS and 数据飞轮 OPS (Canvas 2D).
  Switch in the view's top-right control or press T (FORGE -> WORKS -> OPS); the choice
  persists in localStorage (ampuly.collab.relationTheme); a stored retired value such as
  'simple' (the removed 简洁 option) is rewritten to FORGE. The plain force graph above is
  no longer selectable: it renders only as an automatic fallback (data-theme="basic") when
  FORGE has no WebGL, when reduced motion is on (static layout), or when a theme crashes or
  its chunk fails to load; a chip says why. Counts, names, tasks, states and feed rows are
  real data; particle motion is decorative and each themed view says so in its footer.
  Members are capped at 64 per theme scene; FORGE lowers pixel ratio, then drops bloom,
  when frames stay slow.
- Theme follows the shell's VI tokens: paper/superones in light, midnight in dark.
  No hardcoded teal/pink/violet; status uses the shell green/yellow/red.
- Reduced motion stops edge flow and transitions; the page also exposes a static class.
- The execution drawer keeps the existing privacy contract: only sanitized public titles,
  summaries and references. Raw model thinking, tool arguments/results, paths and arbitrary
  payloads never enter this display projection. This is not an audit journal.
- Order history uses the existing order_timeline read over the authenticated socket, exact
  project/order scope, a 15-second timeout, cancellation and queue:false. The legacy reply
  has no request_id; a late reply for the same project/order cannot be distinguished from a
  newer read of that scope. Cross-project/order responses are rejected.
- Existing owner authentication and authorization remain authoritative.

Upstream patterns (see THIRD_PARTY_NOTICES.md)
- AutoGen Studio (microsoft/autogen, LICENSE-CODE MIT): React Flow node/edge and dagre layout.
- Edict (cft0808/edict, MIT): duty card and official detail information hierarchy.
- OpenClaw Office / OpenClaw Dashboard (MIT): employee card and command-center visual language.
- Agent-Management-System (dvelton, MIT): reporting/matrix/permission domain vocabulary.
- AgentScope Studio (Apache-2.0, archived): trace list/detail reference only.
No upstream source tree is vendored; the new files live under observatory/collaboration/.

Legacy
- ReasoningObservatoryPage.tsx, OrbCanvas.tsx and their tests are retained for reference but
  are no longer mounted by ExecutionObservatoryHost. The new page is the only entry.

Verification
- collaboration/model.test.ts, layout.test.ts, CollaborationPage.test.tsx (SSR/empty states).
- Existing observatory store/trace/history/transport/integration tests remain green.
- TypeScript build and Vite production build; browser check at 127.0.0.1:8766.
