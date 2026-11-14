// A node-table change master control proposed (policy: master control only proposes). It rides on
// the reply's metadata as `master_node_proposal`, survives a reload with the message, and is only ever
// previewed on the builder canvas until a person applies it to their draft and saves it themselves.

import type { WorkflowNode } from '../components/WorkflowsPage'
import { nextPatchVersion } from '../workspace/workflowGraph'
import { copyEmployeeBinding, employeeBindingFrom, employeeBindingsUnchanged } from './workflowEmployeeBinding'

export interface NodeProposal {
  package_id: string
  package_name: string
  base_version: string
  new_version: string
  change_note: string
  /** The table the proposal was checked against; a same-version save since then makes the proposal stale. */
  base_nodes: WorkflowNode[]
  nodes: WorkflowNode[]
  counts: { added: number; removed: number; changed: number }
  /**
   * Components the proposal puts on steps that are not in the internal library yet (policy: master
   * control calls every component that may help; people test and admit). Each is still to be tried: 调用测试
   * downloads it, and a draft naming it is not saved until it is admitted or taken off the steps.
   */
  candidates: ProposalCandidate[]
}

export interface ProposalCandidate {
  id: string
  /** skillhub: the online market (can be downloaded to the trial area); catalog: the local resource library catalog. */
  source: 'skillhub' | 'catalog'
  name: string
  summary: string
  category: string
  /** The proposed steps that name it. */
  node_ids: string[]
  /** False when it cannot be tried on this machine (a local catalog skill with no library named): no import is offered. */
  can_try: boolean
}

const MAX_CANDIDATES = 50

function candidatesFrom(raw: unknown): ProposalCandidate[] {
  if (!Array.isArray(raw)) return []
  const out: ProposalCandidate[] = []
  for (const entry of raw.slice(0, MAX_CANDIDATES)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const item = entry as Record<string, unknown>
    const id = typeof item.id === 'string' ? item.id : ''
    const source = item.source === 'skillhub' || item.source === 'catalog' ? item.source : null
    if (!/^[a-z0-9][a-z0-9._-]{0,127}$/.test(id) || !source) continue
    const text = (value: unknown, limit: number) => (typeof value === 'string' ? value.slice(0, limit) : '')
    out.push({ id, source, name: text(item.name, 200) || id, summary: text(item.summary, 300), category: text(item.category, 80), node_ids: strings(item.node_ids).slice(0, 50), can_try: item.can_try !== false })
  }
  return out
}

const PACKAGE_ID_RE = /^[a-z0-9][a-z0-9_-]{1,63}$/
const MAX_NODES = 200
// A value past this is not something a person reviews on a card; the whole proposal is refused, never cut.
const MAX_TEXT = 20000

const string = (value: unknown): string | null => (typeof value === 'string' ? (value.length <= MAX_TEXT ? value : null) : '')
const strings = (value: unknown) => (Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [])
const count = (value: unknown) => (typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : 0)

// Values pass through as the backend normalised them (node_payload): trimming or truncating here would make an
// untouched step differ from the list payload it is compared with, and read as a change the proposal never made.
function nodeFrom(raw: unknown): WorkflowNode | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const item = raw as Record<string, unknown>
  const fields = ['id', 'name', 'type', 'role', 'assignee', 'gate', 'gate_to', 'responsibility'] as const
  const values: Record<string, string> = {}
  for (const field of fields) {
    const value = string(item[field])
    if (value === null) return null
    values[field] = value
  }
  if (!values.id.trim()) return null
  const hasBinding = Object.prototype.hasOwnProperty.call(item, 'employee_binding')
  const binding = hasBinding ? employeeBindingFrom(item.employee_binding) : null
  if (hasBinding && !binding) return null
  const canvas = item.canvas && typeof item.canvas === 'object' ? item.canvas as { x?: number; y?: number } : null
  return {
    id: values.id,
    name: values.name,
    type: values.type,
    role: values.role,
    assignee: values.assignee,
    gate: values.gate,
    gate_to: values.gate_to,
    depends_on: strings(item.depends_on),
    deliverables: strings(item.deliverables),
    inputs: strings(item.inputs),
    skills: strings(item.skills),
    tools: strings(item.tools),
    responsibility: values.responsibility,
    invoke: item.invoke && typeof item.invoke === 'object' && !Array.isArray(item.invoke) ? { ...(item.invoke as Record<string, unknown>) } : {},
    ...(binding ? { employee_binding: binding } : {}),
    // As node_payload sent it, always present: '' is "back to the default", and the save only clears what a draft
    // carries -- dropping an empty value would keep the stored model the proposal meant to reset.
    model: typeof item.model === 'string' ? item.model : '',
    // The step's tier (轻/标准/强) the same way: '' is "back to the step's own default", and it must survive here or
    // a proposed tier would be counted as a change, never shown on the card, and dropped on 应用 without a word.
    tier: typeof item.tier === 'string' ? item.tier : '',
    // 打回时回到哪一步 travels with an applied proposal like the rest of the step (review).
    rework_to: typeof item.rework_to === 'string' ? item.rework_to : '',
    // A branch's 分支条件 (read-only, 4.7 A02) as node_payload sent it, '' = not a branch: the previewed card shows it,
    // and it survives 应用. A proposal cannot change it (the server refuses that by name), so it is never a change.
    branch_when: typeof item.branch_when === 'string' ? item.branch_when : '',
    // The step's browser connector as node_payload sent it (null = none, and null takes one off).
    connector: item.connector && typeof item.connector === 'object' && !Array.isArray(item.connector)
      ? { ...(item.connector as Record<string, unknown>) } as WorkflowNode['connector']
      : null,
    canvas,
  }
}

function tableFrom(raw: unknown): WorkflowNode[] | null {
  if (!Array.isArray(raw) || raw.length > MAX_NODES) return null
  const nodes = raw.map(nodeFrom)
  return nodes.some(node => node === null) ? null : nodes as WorkflowNode[]
}

/** 整体撤销 (4.7 A04): an applied proposal, remembered with the table it was checked against until its draft is
 *  saved (then offered back) or discarded (then forgotten). */
export interface ProposalRevert { workflowId: string; baseVersion: string; baseNodes: WorkflowNode[]; note: string }

const copyNode = (node: WorkflowNode): WorkflowNode => ({
  ...node,
  depends_on: [...node.depends_on],
  deliverables: [...node.deliverables],
  ...(node.inputs ? { inputs: [...node.inputs] } : {}),
  skills: [...(node.skills ?? [])],
  tools: [...(node.tools ?? [])],
  invoke: { ...node.invoke },
  ...(node.employee_binding ? { employee_binding: copyEmployeeBinding(node.employee_binding) } : {}),
})

/** The table 应用到草稿 puts on the canvas: the proposal's own, copied so an edit to the draft never reaches back
 *  into the proposal (or into what 整体撤销 later puts back). */
export function appliedNodes(nodes: readonly WorkflowNode[]): WorkflowNode[] {
  return nodes.map(copyNode)
}

/** What 应用到草稿 remembers for 整体撤销. */
export function appliedProposal(workflowId: string, proposal: Pick<NodeProposal, 'base_version' | 'base_nodes' | 'change_note'>): ProposalRevert {
  return { workflowId, baseVersion: proposal.base_version, baseNodes: appliedNodes(proposal.base_nodes), note: proposal.change_note }
}

/**
 * 回到 v… 的步骤表: the draft that undoes a saved proposal. It is the table the proposal was checked against, opened
 * against the version and revision saved NOW (so its save is not refused as stale) and saved as the next version --
 * nothing is rewritten in place: the change and its undoing are both versions on record.
 */
export function revertDraft(offer: ProposalRevert, saved: { version: string; draft_revision?: string }, zh: boolean): {
  nodes: WorkflowNode[]; baseVersion: string; baseRevision: string; version: string; note: string
} {
  return {
    nodes: appliedNodes(offer.baseNodes),
    baseVersion: saved.version,
    baseRevision: saved.draft_revision ?? '',
    version: nextPatchVersion(saved.version),
    note: zh ? `撤销安安的修改，回到 v${offer.baseVersion} 的步骤表` : `Undo 安安's change: back to the steps of v${offer.baseVersion}`,
  }
}

/** The proposal a reply carries, or null when there is none or it is not well-formed (then no card is shown). */
export function nodeProposalFrom(metadata: unknown): NodeProposal | null {
  if (!metadata || typeof metadata !== 'object') return null
  const raw = (metadata as Record<string, unknown>).master_node_proposal
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const item = raw as Record<string, unknown>
  const packageId = typeof item.package_id === 'string' ? item.package_id : ''
  const baseVersion = typeof item.base_version === 'string' ? item.base_version.trim() : ''
  const newVersion = typeof item.new_version === 'string' ? item.new_version.trim() : ''
  if (!PACKAGE_ID_RE.test(packageId) || !baseVersion || !newVersion || baseVersion.length > 40 || newVersion.length > 40) return null
  const nodes = tableFrom(item.nodes)
  // A proposal without the table it was checked against cannot be told apart from a stale one: no card.
  const baseNodes = tableFrom(item.base_nodes)
  if (!nodes || !baseNodes) return null
  // Transporting an existing binding is not permission for master control to
  // add/change/unbind it, including by deleting the bound node itself.
  if (!employeeBindingsUnchanged(baseNodes, nodes)) return null
  const name = string(item.package_name)
  const note = string(item.change_note)
  if (name === null || note === null) return null
  const counts = item.counts && typeof item.counts === 'object' ? item.counts as Record<string, unknown> : {}
  return {
    package_id: packageId,
    package_name: name || packageId,
    base_version: baseVersion,
    new_version: newVersion,
    change_note: note,
    base_nodes: baseNodes,
    nodes,
    counts: { added: count(counts.added), removed: count(counts.removed), changed: count(counts.changed) },
    candidates: candidatesFrom(item.candidates),
  }
}
