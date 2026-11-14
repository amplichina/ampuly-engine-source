// 打回时回到 (4.7-A07): where a person's review sends its 打回. The save allows only a step the review comes after --
// its depends_on, followed back (workflow_packages._steps_before) -- and only on a human_review step; the popup offers
// exactly those, so a choice it shows is one the save accepts. Pure.
import type { WorkflowNode } from '../components/WorkflowsPage'

type Step = Pick<WorkflowNode, 'id' | 'name' | 'depends_on'>

/** The steps `node` comes after, in table order (how the canvas lists steps everywhere else). */
export function reworkTargets(nodes: readonly Step[], node: Pick<WorkflowNode, 'id'>): Array<{ id: string; name: string }> {
  const deps = new Map(nodes.map(step => [step.id, (step.depends_on ?? []).map(dep => String(dep).trim())]))
  const before = new Set<string>()
  const queue = [...(deps.get(node.id) ?? [])]
  while (queue.length) {
    const current = queue.shift() as string
    if (current === node.id || before.has(current) || !deps.has(current)) continue
    before.add(current)
    queue.push(...(deps.get(current) ?? []))
  }
  return nodes.filter(step => before.has(step.id)).map(step => ({ id: step.id, name: step.name || step.id }))
}

/** Shown on a person's review; on any other step only while it still carries a value, so the save's refusal can be
 *  fixed where it points (a step retyped away from 人审 keeps what it had until someone clears it). */
export function showsReworkTo(node: Pick<WorkflowNode, 'type' | 'rework_to'>): boolean {
  return node.type === 'human_review' || Boolean(String(node.rework_to ?? '').trim())
}
