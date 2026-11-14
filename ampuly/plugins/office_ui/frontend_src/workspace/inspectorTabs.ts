// The step inspector's three pages (frontend refactor U2-3): what comes in, how the step is set up, what it hands
// over. Pure: which page a field lives on is decided here, so "go fix it" from the blocker strip can open the right
// page before it puts the cursor in the field -- a field on a hidden page cannot take focus.

import type { WorkflowNode } from '../components/WorkflowsPage'

export const INSPECTOR_TABS = ['inputs', 'config', 'output'] as const
export type InspectorTab = typeof INSPECTOR_TABS[number]

/** [zh, en] -- the owner's names for the three pages. */
export const INSPECTOR_TAB_LABELS: Record<InspectorTab, readonly [string, string]> = {
  inputs: ['输入', 'Inputs'],
  config: ['配置', 'Set-up'],
  output: ['交付', 'Output'],
}

/** Opening a step shows how it is set up; the other two pages are one click away. */
export const DEFAULT_INSPECTOR_TAB: InspectorTab = 'config'

const FIELD_TABS: Record<string, InspectorTab> = {
  inputs: 'inputs',
  deliverables: 'output',
}

/** The page a field is on. Anything not listed is part of the step's set-up. */
export function tabForField(field: string): InspectorTab {
  return FIELD_TABS[field] ?? 'config'
}

export type UpstreamStep = { id: string; name: string; deliverables: string[]; missing: boolean }

/** The steps this one waits for, in the order it names them, with what each promises to hand over. A dependency on a
 *  step that is not in the draft is kept and marked: hiding it would hide exactly the thing that needs fixing. */
export function upstreamSteps(
  nodes: ReadonlyArray<Pick<WorkflowNode, 'id' | 'name' | 'deliverables'>>,
  node: Pick<WorkflowNode, 'depends_on'>,
): UpstreamStep[] {
  const byId = new Map(nodes.map(item => [item.id, item]))
  const seen = new Set<string>()
  const result: UpstreamStep[] = []
  for (const raw of node.depends_on ?? []) {
    const id = String(raw ?? '').trim()
    if (!id || seen.has(id)) continue
    seen.add(id)
    const found = byId.get(id)
    result.push({
      id,
      name: found ? (found.name || found.id) : id,
      deliverables: found ? (found.deliverables ?? []).map(item => String(item)).filter(Boolean) : [],
      missing: !found,
    })
  }
  return result
}
