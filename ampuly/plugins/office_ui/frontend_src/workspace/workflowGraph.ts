import type { WorkflowNode } from '../components/WorkflowsPage'
import {
  GROUP_GAP,
  GROUP_PADDING,
  GROUP_TITLE_HEIGHT,
  GROUP_WIDTH,
  NODE_GAP,
  computeRunGraphLayout,
  type LayoutItem,
  type RunGraphLayout,
} from './runGraphLayout'

// Pure graph/patch logic behind the 搭建台 canvas (4.4 §2.3/§2.4). It is kept out of WorkflowCanvas
// (which imports React Flow + its CSS and so cannot run in the node contract tests) so the drag,
// connect, cycle-guard and disconnect rules can be unit-tested directly. A node is a component
// container: dropping a component patches one of its slots; a wire is a depends_on relation.

/** The MIME the 组件库 chips carry; the drop text is a JSON ComponentPayload. */
export const COMPONENT_MIME = 'application/x-superones-component'

/** The node kinds a person can add from the 组件库 (invoke_workflow needs a target and is not offered). */
export type NodeKind = 'ai_do' | 'human_do' | 'human_review'
export const ADDABLE_NODE_KINDS: readonly NodeKind[] = ['ai_do', 'human_do', 'human_review']

export type ComponentPayload =
  | { kind: 'employee'; id: string; name: string }
  | { kind: 'node'; id: NodeKind; name: string }
  // one of the package's declared skills (installed, version-locked, contract-checked); nothing else is offered
  | { kind: 'skill'; id: string; name: string }
  // another live workflow, added as an invoke_workflow step: the larger component (业务流)
  | { kind: 'flow'; id: string; name: string }

/** dataTransfer.types is the only thing readable during dragover, so each kind also sets its own type. */
export const componentKindMime = (kind: ComponentPayload['kind']) => `${COMPONENT_MIME}-${kind}`

/**
 * The card rows a component can be dropped onto. `gate` is the 审核 row: the identity that signs the
 * node's gate off (gate_to). Anything else on the node -- its body or the 负责 row -- is `assignee`.
 */
export type NodeSlot = 'assignee' | 'gate'

/**
 * The WorkflowNode patch a dropped component produces, or null if it changes nothing (a kind that fills
 * no slot yet, or a skill the node already uses). A skill is added wherever on the node it lands.
 */
export function patchForComponent(
  payload: ComponentPayload,
  slot: NodeSlot | '' = 'assignee',
  node?: Pick<WorkflowNode, 'skills'>,
): Partial<WorkflowNode> | null {
  switch (payload.kind) {
    case 'employee':
      // gate_to is who signs the gate off: the engine hands it to the pause request as
      // assignee_employee_id, so that employee (and the owner) may answer the checkpoint.
      if (slot === 'gate') return { gate_to: payload.id }
      // assignee is the employee_id: company_mode routes the human node by assignee == employee_id.
      return { assignee: payload.id }
    case 'skill': {
      const current = node?.skills ?? []
      return current.includes(payload.id) ? null : { skills: [...current, payload.id] }
    }
    // a node chip adds a step (newNode); it fills no slot of an existing node
    default: return null
  }
}

/** A node's skills with `id` switched on or off, keeping the order they were added in. */
export function toggleSkill(skills: readonly string[] | undefined, id: string): string[] {
  const current = [...(skills ?? [])]
  return current.includes(id) ? current.filter(item => item !== id) : [...current, id]
}

/**
 * Skills a node names that the package does not declare. The save accepts them (the process factory
 * names skills it drafts later), but only declared skills are installed and version-locked, so the
 * builder says so instead of letting one look like it will run.
 */
export function undeclaredSkills(node: Pick<WorkflowNode, 'skills'>, packageSkills: readonly string[] | undefined, librarySkills: readonly string[] = []): string[] {
  if (!packageSkills) return []
  return (node.skills ?? []).filter(skill => !packageSkills.includes(skill) && !librarySkills.includes(skill))
}

/**
 * Where a skill on a step stands with the package: declared already, declared by the save (an internal-library
 * skill this package can lock), or neither -- the save keeps the name but nothing installs it.
 */
export function skillDeclaration(
  skill: string, packageSkills: readonly string[] | undefined, librarySkills: readonly string[] = [], libraryUnlockable: readonly string[] = [],
  trialSkills: readonly string[] = [],
  candidates: readonly string[] = [],
): 'declared' | 'on_save' | 'refused' | 'trial' | 'candidate' | 'undeclared' {
  if (!packageSkills || packageSkills.includes(skill)) return 'declared'
  if (librarySkills.includes(skill)) return 'on_save'
  // In the library, but this package cannot lock it (incomplete release contract): the save would refuse the table.
  if (libraryUnlockable.includes(skill)) return 'refused'
  // On trial (调用测试): fine on a draft to see how it fits; not saved until it is admitted to the internal library.
  if (trialSkills.includes(skill)) return 'trial'
  // Suggested by master control from outside the library, not downloaded yet: 调用测试 is the next step.
  return candidates.includes(skill) ? 'candidate' : 'undeclared'
}

/** Below this canvas width the side areas stop taking room from the canvas (4.4 §2.4). */
export const NARROW_CANVAS_WIDTH = 720

/**
 * How the builder lays out at a given canvas width (4.4 §2.4: on a narrow screen the shelf and the node editor fold
 * into drawers and the canvas keeps the main space). An unmeasured width (0) is treated as wide: the layout the
 * canvas had before anything was measured.
 */
export function canvasLayoutFor(width: number): { narrow: boolean; shelf: 'side' | 'drawer'; editor: 'popover' | 'sheet' } {
  const narrow = width > 0 && width < NARROW_CANVAS_WIDTH
  return { narrow, shelf: narrow ? 'drawer' : 'side', editor: narrow ? 'sheet' : 'popover' }
}

/**
 * Skill names a draft adds to its steps that the save would not declare: not the package's, not the internal
 * library's (a skill on trial, a component master control suggested from outside, a typo). While any is there the
 * draft is not saved -- it would carry a name nothing installs. Names the saved steps already carry are left alone.
 */
export function unadmittedNewSkills(
  draft: ReadonlyArray<Pick<WorkflowNode, 'skills'>>, saved: ReadonlyArray<Pick<WorkflowNode, 'skills'>>,
  packageSkills: readonly string[] | undefined, librarySkills: readonly string[],
): string[] {
  const before = new Set(saved.flatMap(node => node.skills ?? []))
  const found = new Set<string>()
  for (const node of draft) {
    for (const skill of node.skills ?? []) {
      if (before.has(skill) || (packageSkills ?? []).includes(skill) || librarySkills.includes(skill)) continue
      found.add(skill)
    }
  }
  return [...found]
}

/** The skills on trial a draft's steps name: while any is there, the draft is not saved (see skillDeclaration). */
export function trialSkillsOnSteps(
  nodes: ReadonlyArray<Pick<WorkflowNode, 'skills'>>, packageSkills: readonly string[] | undefined, librarySkills: readonly string[], trialSkills: readonly string[],
): string[] {
  const found = new Set<string>()
  for (const node of nodes) {
    for (const skill of node.skills ?? []) {
      if (skillDeclaration(skill, packageSkills ?? [], librarySkills, [], trialSkills) === 'trial') found.add(skill)
    }
  }
  return [...found]
}

/**
 * The employees a workflow's nodes may name (as assignee or signer). An employee belongs to one org's
 * roster, and an order runs on the workflow's own org (prepare_invocation installs and runs
 * package.org_id), so the console org's employees are valid only when that org is the workflow's org.
 * Offering them otherwise would write a person the workflow's runtime org does not have.
 */
export function rosterFor<T>(workflowOrgId: string | null | undefined, employeesOrgId: string | null | undefined, employees: readonly T[]): { roster: readonly T[]; sameOrg: boolean } {
  const workflowOrg = String(workflowOrgId ?? '').trim()
  const sameOrg = Boolean(workflowOrg) && workflowOrg === String(employeesOrgId ?? '').trim()
  return { roster: sameOrg ? employees : [], sameOrg }
}

/** The signing identity a gate resolves to: an empty gate_to is the owner (the backend's default). */
export function effectiveSigner(gateTo: string | null | undefined): string {
  return String(gateTo ?? '').trim() || 'owner'
}

export interface StepInput {
  from: string
  name: string
  deliverables: string[]
}

/**
 * What a step needs: the deliverables of the steps it depends on, in depends_on order. A wire carries a
 * step's output to the next (4.4 §2.1), so this is derived from the wires rather than stored separately.
 */
export function inputsFor(
  nodes: readonly Pick<WorkflowNode, 'id' | 'name' | 'deliverables' | 'depends_on'>[],
  node: Pick<WorkflowNode, 'depends_on'>,
): StepInput[] {
  const byId = new Map(nodes.map(item => [item.id, item]))
  const inputs: StepInput[] = []
  for (const dep of node.depends_on ?? []) {
    const upstream = byId.get(dep)
    if (upstream) inputs.push({ from: upstream.id, name: upstream.name || upstream.id, deliverables: [...(upstream.deliverables ?? [])] })
  }
  return inputs
}

/**
 * A step's saved canvas position (relative to its lane), or null when the layout should place it. The
 * position lives on the node in the 流程包, so every viewer sees the same arrangement.
 */
export function placedPosition(node: Pick<WorkflowNode, 'canvas'>): { x: number; y: number } | null {
  const { x, y } = node.canvas ?? {}
  if (typeof x !== 'number' || typeof y !== 'number' || !Number.isFinite(x) || !Number.isFinite(y)) return null
  return { x, y }
}

/** A node id not used yet -- step_1, step_2, ... Ids are referenced by depends_on and by runtime scope keys. */
export function nextNodeId(nodes: readonly Pick<WorkflowNode, 'id'>[], base = 'step'): string {
  const used = new Set(nodes.map(node => node.id))
  for (let n = 1; ; n++) {
    const id = `${base}_${n}`
    if (!used.has(id)) return id
  }
}

/**
 * A new draft step of this kind, chained after `after` when that node exists. It starts incomplete on
 * purpose -- no role, responsibility or deliverables -- because only a person can say what the step is;
 * the backend refuses to save it until they do, and missingForSave shows what is left on its card.
 */
export function newNode(nodes: readonly WorkflowNode[], kind: NodeKind, name: string, after = ''): WorkflowNode {
  return {
    id: nextNodeId(nodes),
    name,
    type: kind,
    role: '',
    // a person node must name who does it; 'owner' is the value the backend accepts by default
    assignee: kind === 'ai_do' ? '' : 'owner',
    gate: 'none',
    gate_to: '',
    depends_on: after && nodes.some(node => node.id === after) ? [after] : [],
    deliverables: [],
    skills: [],
    tools: [],
    responsibility: '',
    invoke: {},
  }
}

/**
 * A step that hands the order on to another workflow (runtime: an order is placed on `workflowId` and
 * launched). Like any new step it waits for a person to give it a role, the only way the runtime sees it.
 */
export function newInvokeNode(nodes: readonly WorkflowNode[], workflowId: string, name: string, after = ''): WorkflowNode {
  return {
    id: nextNodeId(nodes, 'call'),
    name,
    type: 'invoke_workflow',
    role: '',
    assignee: '',
    gate: 'none',
    gate_to: '',
    depends_on: after && nodes.some(node => node.id === after) ? [after] : [],
    deliverables: [],
    skills: [],
    tools: [],
    responsibility: '',
    invoke: { workflow: workflowId },
  }
}

/**
 * A draft was opened on `baseVersion`, and the package is now at `liveVersion`: someone saved in between.
 * Saving it would be refused as a version conflict (and must be -- it would drop their change), so the
 * builder says so and offers no save until the draft is reopened on what is saved now.
 */
export function draftIsStale(baseVersion: string | undefined, liveVersion: string | undefined): boolean {
  const base = String(baseVersion ?? '').trim()
  return Boolean(base) && base !== String(liveVersion ?? '').trim()
}

/** The node fields a change is reported by; `canvas` is only a position and is reported apart. */
export const DIFF_FIELDS = ['name', 'type', 'role', 'assignee', 'gate', 'gate_to', 'depends_on', 'deliverables', 'inputs', 'skills', 'tools', 'responsibility', 'invoke', 'model', 'tier', 'connector', 'employee_binding'] as const

/** How a changed step field is named, on a proposal's cards and in 验收准入's version differences. A version compares
 *  every key a step carries, so this also names the ones the canvas never shows but the runtime reads; a key not
 *  listed here is shown by its own name. */
export const DIFF_FIELD_LABELS: Record<string, [string, string]> = {
  name: ['名称', 'name'],
  type: ['类型', 'type'],
  role: ['角色', 'role'],
  assignee: ['负责', 'who'],
  gate: ['审核', 'gate'],
  gate_to: ['签字人', 'signer'],
  depends_on: ['依赖', 'depends on'],
  deliverables: ['交付', 'deliverables'],
  inputs: ['需要', 'needs'],
  skills: ['技能', 'skills'],
  tools: ['工具', 'tools'],
  responsibility: ['职责', 'responsibility'],
  invoke: ['调用', 'call'],
  employee_binding: ['AI 员工绑定', 'AI employee binding'],
  standard: ['验收标准', 'standard'],
  writes: ['可写位置', 'writes'],
  components: ['组件设置', 'component settings'],
  revision_path: ['打回单位置', 'revision sheet'],
  branch_when: ['分支条件', 'branch'],
  rework_to: ['打回后回到', 'rework to'],
  model: ['模型', 'model'],
  tier: ['档位', 'tier'],
  connector: ['浏览器连接器', 'browser connector'],
}

export interface NodeTableDiff {
  added: string[]
  removed: string[]
  changed: Array<{ id: string; fields: string[] }>
  moved: string[]
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

/**
 * What a proposed node table changes against the one on the canvas: steps added, removed, and changed (with
 * the fields), plus steps only moved. This is what a person reads before applying a proposal to their draft.
 */
export function diffNodeTables(base: readonly WorkflowNode[], proposed: readonly WorkflowNode[]): NodeTableDiff {
  const before = new Map(base.map(node => [node.id, node]))
  const after = new Map(proposed.map(node => [node.id, node]))
  const diff: NodeTableDiff = { added: [], removed: [], changed: [], moved: [] }
  for (const node of proposed) {
    const old = before.get(node.id)
    if (!old) { diff.added.push(node.id); continue }
    const fields = DIFF_FIELDS.filter(field => {
      const empty = field === 'depends_on' || field === 'deliverables' || field === 'inputs' || field === 'skills' || field === 'tools' ? [] : field === 'invoke' ? {} : ''
      return !sameValue(old[field] ?? empty, node[field] ?? empty)
    })
    if (fields.length) diff.changed.push({ id: node.id, fields: [...fields] })
    else if (!sameValue(placedPosition(old), placedPosition(node))) diff.moved.push(node.id)
  }
  for (const node of base) if (!after.has(node.id)) diff.removed.push(node.id)
  return diff
}

/**
 * A master-control proposal no longer matches what is saved: the version moved on, or the table changed at the
 * same version (a save may keep its version number). Applying it then would quietly undo that later save.
 */
export function proposalIsStale(
  proposal: { base_version: string; base_nodes: readonly WorkflowNode[] },
  saved: { version: string; nodes: readonly WorkflowNode[] },
): boolean {
  if (draftIsStale(proposal.base_version, saved.version)) return true
  const diff = diffNodeTables(proposal.base_nodes, saved.nodes)
  // A move counts too: applying would put the steps back where the proposal found them.
  return diff.added.length > 0 || diff.removed.length > 0 || diff.changed.length > 0 || diff.moved.length > 0
}

/** One step the table on screen changed away from the table a proposal was checked against (4.7 A04). `fields` are
 *  DIFF_FIELDS keys, plus 'canvas' (only moved), 'added' (a step that is not in that table) and 'removed'. */
export interface ProposalConflict { id: string; name: string; fields: string[] }

/**
 * What applying a proposal would overwrite: every step the table on screen (an open draft, or a save made after the
 * proposal) differs in from the table the proposal was checked against. Applying replaces the whole table, so a step
 * the proposal never touched is overwritten as well -- each one is named with its fields, and the person confirms
 * before it goes. Empty = nothing of theirs is lost.
 */
export function proposalConflicts(base: readonly WorkflowNode[], current: readonly WorkflowNode[]): ProposalConflict[] {
  const diff = diffNodeTables(base, current)
  const name = (id: string) => current.find(node => node.id === id)?.name || base.find(node => node.id === id)?.name || id
  return [
    ...diff.changed.map(item => ({ id: item.id, name: name(item.id), fields: item.fields })),
    ...diff.moved.map(id => ({ id, name: name(id), fields: ['canvas'] })),
    ...diff.added.map(id => ({ id, name: name(id), fields: ['added'] })),
    ...diff.removed.map(id => ({ id, name: name(id), fields: ['removed'] })),
  ]
}

/** A conflict's fields as a person reads them: 「名称、职责」. */
export function conflictFieldsText(fields: readonly string[], zh: boolean): string {
  const extra: Record<string, [string, string]> = { canvas: ['位置', 'position'], added: ['新加了这一步', 'this step was added'], removed: ['删掉了这一步', 'this step was removed'] }
  return fields.map(field => (extra[field] ?? DIFF_FIELD_LABELS[field] ?? [field, field])[zh ? 0 : 1]).join(zh ? '、' : ', ')
}

/** What a proposal lets one step do that it could not do before: tools, websites, places it writes. */
export interface ProposalWidening { id: string; name: string; tools: string[]; sites: string[]; writes: string[] }

const connectorSites = (connector: WorkflowNode['connector']): string[] =>
  connector && Array.isArray(connector.allowed_domains) ? connector.allowed_domains.filter((site): site is string => typeof site === 'string') : []

/**
 * Where a proposal widens what a step may do (4.7 A04 权限扩大), against the table it was checked against: a tool the
 * step did not have, a website its browser connector could not reach, a place it could not write. A new step counts
 * everything it carries. Narrowing is not listed -- it takes nothing away that needs a yes.
 */
export function proposalWidens(base: readonly WorkflowNode[], proposed: readonly WorkflowNode[]): ProposalWidening[] {
  const before = new Map(base.map(node => [node.id, node]))
  const fresh = (now: readonly string[] | undefined, was: readonly string[] | undefined) =>
    [...new Set((now ?? []).filter(value => value.trim() && !(was ?? []).includes(value)))]
  const out: ProposalWidening[] = []
  for (const node of proposed) {
    const old = before.get(node.id)
    const tools = fresh(node.tools, old?.tools)
    const sites = fresh(connectorSites(node.connector), connectorSites(old?.connector))
    const writes = fresh(node.writes, old?.writes)
    if (tools.length || sites.length || writes.length) out.push({ id: node.id, name: node.name || node.id, tools, sites, writes })
  }
  return out
}

/**
 * What a tool lets a step do, in the owner's words: the tools a step can be given (RoleInspector's list) and the
 * ones the engine names in its own prompts (restart_unknown_wait). A tool not listed here is shown by its id.
 */
const TOOL_WORDS: Record<string, readonly [string, string]> = {
  file_read: ['读文件', 'read files'], read_file: ['读文件', 'read files'],
  file_write: ['写文件', 'write files'], write_file: ['写文件', 'write files'],
  file_edit: ['改文件', 'edit files'], edit_file: ['改文件', 'edit files'], apply_patch: ['改文件', 'edit files'],
  file_search: ['在文件里搜索', 'search files'], grep: ['在文件里搜索', 'search files'], glob: ['查找文件', 'find files'],
  list_dir: ['查看文件夹', 'list folders'],
  shell_exec: ['运行命令', 'run commands'], shell: ['运行命令', 'run commands'], bash: ['运行命令', 'run commands'],
  powershell: ['运行命令', 'run commands'], run_command: ['运行命令', 'run commands'], python_exec: ['运行代码', 'run code'],
  web_search: ['搜索网页', 'search the web'], web_fetch: ['抓取网页', 'fetch web pages'],
  todo_read: ['查看待办清单', 'read the to-do list'], todo_write: ['记待办清单', 'write the to-do list'],
  browser_navigate: ['打开网页', 'open web pages'], browser_navigate_back: ['网页后退', 'go back a page'],
  browser_snapshot: ['读取网页', 'read web pages'], browser_observe: ['查看网页', 'look at web pages'],
  browser_wait_for: ['等待网页', 'wait for a page'], browser_scroll: ['滚动网页', 'scroll pages'],
  browser_click: ['点击网页', 'click on pages'], browser_type: ['在网页里输入', 'type into pages'],
  browser_select_option: ['在网页里选择', 'pick options on pages'], browser_submit_enter: ['在网页里提交', 'submit on pages'],
  browser_take_screenshot: ['网页截图', 'take screenshots'], browser_evaluate: ['在网页里运行脚本', 'run scripts on pages'],
  browser_close: ['关闭网页', 'close pages'], browser_switch_tab: ['切换网页标签', 'switch tabs'],
  browser_request_human: ['请人接手网页', 'hand a page to a person'],
  desktop_observe: ['查看本机桌面', 'look at this computer’s desktop'], desktop_act: ['在本机桌面应用里操作', 'operate desktop apps'],
  desktop_finish: ['核对桌面步骤是否做完', 'check a desktop step is done'],
  image_generate: ['生成图片', 'generate images'], send_email: ['发邮件', 'send email'], send_dm: ['发消息', 'send messages'],
}

/** A tool's name for a person: the catalogue's name when one was given, else the words above, else its id. */
export function toolWords(id: string, zh: boolean, names?: ReadonlyMap<string, string>): string {
  const named = names?.get(id)?.trim()
  if (named) return named
  const words = TOOL_WORDS[id]
  return words ? words[zh ? 0 : 1] : id
}

/** A widening as a person reads it: 「工具 搜索网页；网站 shop.example」. `toolNames` is the tool catalogue's names. */
export function wideningText(item: ProposalWidening, zh: boolean, toolNames?: ReadonlyMap<string, string>): string {
  const join = (values: string[]) => values.join(zh ? '、' : ', ')
  const tools = [...new Set(item.tools.map(tool => toolWords(tool, zh, toolNames)))]
  return [
    tools.length ? `${zh ? '工具' : 'tools'} ${join(tools)}` : '',
    item.sites.length ? `${zh ? '网站' : 'websites'} ${join(item.sites)}` : '',
    item.writes.length ? `${zh ? '写入目录' : 'write to'} ${join(item.writes)}` : '',
  ].filter(Boolean).join(zh ? '；' : '; ')
}

export type FlowCallRejection = 'self' | 'cycle'
export type FlowCompositionRejection = FlowCallRejection | 'duplicate'

/**
 * Why `currentFlowId` may not call `targetFlowId`: itself, or a flow that already (transitively) calls it back.
 * `flows` gives each live flow's calls. The engine places a new order per call, so a loop would not stop.
 */
export function flowCallRejection(
  currentFlowId: string,
  targetFlowId: string,
  flows: ReadonlyArray<{ id: string; calls: readonly string[] }>,
): FlowCallRejection | null {
  if (!targetFlowId || targetFlowId === currentFlowId) return 'self'
  const callsOf = new Map(flows.map(flow => [flow.id, flow.calls]))
  const seen = new Set<string>()
  const reaches = (from: string): boolean => {
    if (from === currentFlowId) return true
    if (seen.has(from)) return false
    seen.add(from)
    return (callsOf.get(from) ?? []).some(reaches)
  }
  return reaches(targetFlowId) ? 'cycle' : null
}

/** Suggest the next patch for a semantic canvas edit; unfamiliar version shapes stay explicit for a person. */
export function nextPatchVersion(version: string | undefined): string {
  const current = String(version ?? '').trim()
  const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(current)
  return match ? `${match[1]}.${match[2]}.${Number(match[3]) + 1}` : current
}

/**
 * Stage one workflow as a component inside another workflow's node table.
 * This never saves: callers must still assign the staged node to a role and
 * use the builder's normal versioned save/test/release path.
 */
export function composeWorkflowDraft(
  currentFlowId: string,
  targetFlow: { id: string; name: string },
  nodes: readonly WorkflowNode[],
  flows: ReadonlyArray<{ id: string; calls: readonly string[] }>,
): { nodes: WorkflowNode[]; addedNodeId: string } | { rejection: FlowCompositionRejection } {
  const structural = flowCallRejection(currentFlowId, targetFlow.id, flows)
  if (structural) return { rejection: structural }
  if (nodes.some(node => node.type === 'invoke_workflow' && String(node.invoke?.workflow ?? node.invoke?.workflow_id ?? '') === targetFlow.id)) {
    return { rejection: 'duplicate' }
  }
  const added = newInvokeNode(nodes, targetFlow.id, targetFlow.name)
  return { nodes: [...nodes, added], addedNodeId: added.id }
}

/** The table without `id`, with `id` also dropped from every depends_on (else the save names a missing node). */
export function removeNode(nodes: readonly WorkflowNode[], id: string): WorkflowNode[] {
  return nodes
    .filter(node => node.id !== id)
    .map(node => ((node.depends_on ?? []).includes(id) ? { ...node, depends_on: node.depends_on.filter(dep => dep !== id) } : node))
}

export const STAGING_GROUP_KEY = '__new_steps'

/**
 * A step that belongs in the staging lane: it has no role yet. That includes an invoke_workflow node --
 * the runtime reads nodes only through the role they are projected onto, so a role-less call never runs.
 */
export function isStagedStep(node: Pick<WorkflowNode, 'role' | 'type'>): boolean {
  return !String(node.role ?? '').trim()
}

/**
 * Lay out a node table with role-less steps kept out of the role lanes. computeRunGraphLayout orders lanes by
 * rank and colours them by position, so a new dependency-free step's own lane would be inserted second and
 * shift -- and recolour -- every lane after it: adding one step would move the whole graph. Staged steps go
 * into one lane appended after all role lanes instead, computed without them, so existing lanes stay put.
 */
export function layoutWithStaging(items: readonly LayoutItem[], stagedIds: ReadonlySet<string>, stagingName: string): RunGraphLayout {
  const placed = items.filter(item => !stagedIds.has(item.id))
  const placedIds = new Set(placed.map(item => item.id))
  const layout = computeRunGraphLayout(placed.map(item => ({
    ...item,
    deps: item.deps.filter(dep => placedIds.has(dep)),
    parent: item.parent && placedIds.has(item.parent) ? item.parent : undefined,
  })))
  const staged = items.filter(item => stagedIds.has(item.id)).sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0))
  if (!staged.length) return layout
  const x = layout.groups.reduce((right, group) => Math.max(right, group.x + group.width + GROUP_GAP), 0)
  const positions = new Map(layout.positions)
  let y = GROUP_TITLE_HEIGHT + GROUP_PADDING
  for (const item of staged) {
    positions.set(item.id, { x: GROUP_PADDING, y, groupKey: STAGING_GROUP_KEY })
    y += item.height + NODE_GAP
  }
  return {
    groups: [...layout.groups, { key: STAGING_GROUP_KEY, name: stagingName, color: '#8c8c96', x, y: 0, width: GROUP_WIDTH, height: y - NODE_GAP + GROUP_PADDING, count: staged.length }],
    positions,
    ranks: layout.ranks,
  }
}

export type MissingField = 'role' | 'roleNotInOrg' | 'responsibility' | 'deliverables' | 'assignee' | 'invokeTarget'

/**
 * What the backend's per-node checks (validate_nodes) would still refuse, so the card can say so before a
 * save fails. Pass the org's role ids to also mirror "role is in the org config" (skipped when the list is
 * empty, as the backend skips it).
 */
export function missingForSave(node: WorkflowNode, orgRoleIds?: ReadonlySet<string>): MissingField[] {
  const missing: MissingField[] = []
  // Every node needs a role, a call included: the runtime only sees nodes projected onto a role.
  const role = String(node.role ?? '').trim()
  if (!role) missing.push('role')
  else if (orgRoleIds && orgRoleIds.size > 0 && !orgRoleIds.has(role)) missing.push('roleNotInOrg')
  if (node.type === 'invoke_workflow') {
    if (!String(node.invoke?.workflow ?? '').trim()) missing.push('invokeTarget')
  } else {
    if (!String(node.responsibility ?? '').trim()) missing.push('responsibility')
    if (!(node.deliverables ?? []).length) missing.push('deliverables')
  }
  if ((node.type === 'human_do' || node.type === 'human_review') && !String(node.assignee ?? '').trim()) missing.push('assignee')
  return missing
}

/** Only the fields the graph rules read, so callers can pass WorkflowNode[] or a lighter shape. */
type GraphNode = Pick<WorkflowNode, 'id' | 'depends_on'>

/** True if `from` reaches `to` by following depends_on edges (so an edge to->from would cycle). */
export function reachesViaDeps(nodes: readonly GraphNode[], from: string, to: string): boolean {
  const adj = new Map(nodes.map(node => [node.id, node.depends_on ?? []]))
  const seen = new Set<string>()
  const walk = (id: string): boolean => {
    if (id === to) return true
    if (seen.has(id)) return false
    seen.add(id)
    return (adj.get(id) ?? []).some(walk)
  }
  return walk(from)
}

export interface NodePatch {
  nodeId: string
  patch: Partial<WorkflowNode>
}

export type ConnectRejection = 'missing' | 'self' | 'duplicate' | 'cycle'

/** Why a source->target wire is refused, or null when it is a valid new dependency. */
export function connectRejection(nodes: readonly GraphNode[], source: string | null | undefined, target: string | null | undefined): ConnectRejection | null {
  if (!source || !target) return 'missing'
  if (source === target) return 'self'
  const node = nodes.find(item => item.id === target)
  if (!node || !nodes.some(item => item.id === source)) return 'missing'
  if ((node.depends_on ?? []).includes(source)) return 'duplicate'
  if (reachesViaDeps(nodes, source, target)) return 'cycle' // source already reaches target
  return null
}

/**
 * The depends_on patch that adds a source->target wire (target depends on source), or null when the
 * edge is invalid (see connectRejection): a self-loop, a duplicate, a cycle, or an unknown end.
 */
export function connectPatch(nodes: readonly GraphNode[], source: string | null | undefined, target: string | null | undefined): NodePatch | null {
  if (connectRejection(nodes, source, target)) return null
  const node = nodes.find(item => item.id === target)!
  return { nodeId: target as string, patch: { depends_on: [...(node.depends_on ?? []), source as string] } }
}

/**
 * The depends_on patches that remove a set of edges, aggregated per target so several edges deleted
 * against one node collapse into a single patch (separate patches would each drop from the original
 * list and clobber one another).
 */
export function disconnectPatches(nodes: readonly GraphNode[], edges: readonly { source?: string | null; target?: string | null }[]): NodePatch[] {
  const removeByTarget = new Map<string, Set<string>>()
  for (const edge of edges) {
    if (!edge.target || !edge.source) continue
    if (!removeByTarget.has(edge.target)) removeByTarget.set(edge.target, new Set())
    removeByTarget.get(edge.target)!.add(edge.source)
  }
  const patches: NodePatch[] = []
  for (const [target, sources] of removeByTarget) {
    const node = nodes.find(item => item.id === target)
    if (!node) continue
    patches.push({ nodeId: target, patch: { depends_on: (node.depends_on ?? []).filter(dep => !sources.has(dep)) } })
  }
  return patches
}

/**
 * Changing what kind of step this is (AI 做 / 人做 / 人审), from the step's own editor -- the shelf no longer offers
 * three kinds of empty step (policy: the choice belongs in the step's popup). Returns the patch, or null
 * when the kind cannot or need not change: a step that calls another flow is not one of these three.
 *
 * A person's step must name who does it (`owner` is what the server accepts by default); an AI step names nobody, so
 * a default `owner` that nobody chose is cleared -- but a real assignee someone picked is left alone.
 */
export function retypePatch(node: Pick<WorkflowNode, 'type' | 'assignee'>, kind: NodeKind): Partial<WorkflowNode> | null {
  if (!ADDABLE_NODE_KINDS.includes(kind) || !ADDABLE_NODE_KINDS.includes(node.type as NodeKind) || node.type === kind) return null
  const assignee = (node.assignee ?? '').trim()
  if (kind === 'ai_do') return assignee === 'owner' ? { type: kind, assignee: '' } : { type: kind }
  // A person's step runs on no model, no tier and no browser connector: what it carried as an AI step goes (the save
  // refuses any of the three on a person's step, and a person's step has no field left to clear them).
  return assignee ? { type: kind, model: '', tier: '', connector: null } : { type: kind, assignee: 'owner', model: '', tier: '', connector: null }
}
