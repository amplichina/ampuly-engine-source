import type { WorkflowNode } from '../components/WorkflowsPage'
import { nextNodeId } from './workflowGraph'

/**
 * A copy of one step, placed right after the original in the table. It keeps what the step *is* (type, role,
 * responsibility, skills, tools, gate, what it depends on) and gets its own id and name. Three things are not copied:
 * the hand-placed position (the copy would sit exactly on top of the original; the canvas places it a row under),
 * the 分支条件 (branch_when: a copy of a branch would be a second branch on the same condition at the same decision,
 * and the run would take both -- the copy is no branch until someone makes it one), and nothing downstream is rewired
 * -- a copy nobody depends on is the safe default, and the person wires it where they want it.
 *
 * Deliverables are copied as written. Two steps promising the same file is something a person should see and
 * change, not something to guess a new path for; the name says it is a copy.
 */
export function duplicateNode(nodes: readonly WorkflowNode[], nodeId: string, zh: boolean): { nodes: WorkflowNode[]; created: WorkflowNode } | null {
  const index = nodes.findIndex(node => node.id === nodeId)
  if (index < 0) return null
  const original = nodes[index]
  const base = original.id.replace(/_copy(_\d+)?$/, '').replace(/_\d+$/, '') || 'step'
  const copy = structuredClone(original) as WorkflowNode & { canvas?: unknown }
  copy.id = nextNodeId(nodes, `${base}_copy`)
  copy.name = `${original.name || original.id}${zh ? '（副本）' : ' (copy)'}`
  delete copy.canvas
  delete copy.branch_when
  return { nodes: [...nodes.slice(0, index + 1), copy, ...nodes.slice(index + 1)], created: copy }
}
