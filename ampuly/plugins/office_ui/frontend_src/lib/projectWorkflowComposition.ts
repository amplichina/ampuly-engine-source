import type { WorkflowSummary, WorkOrderSummary } from '../components/WorkflowsPage'
import { queueOf, type StoppedAllowances } from './orderQueues'

// Which workflow_list replies the console shows -- the project on screen's, a "switch" to the project already on
// screen holding nothing back, a refusal that names no project still reaching the page -- is lib/currentProject's
// acceptsOrBareRefusal now, the same rule every list handler asks; its cases are tested there.

export interface ProjectWorkflowCard {
  id: string
  name: string
  version: string
  status: string
  orderCount: number
  openOrderCount: number
  /** Of the open ones, how many wait on a person (the same rule as 业务工单's queue). */
  waitingOrderCount: number
  calls: string[]
  missingCalls: string[]
  /** The project this flow was derived for (派生为本项目草稿); '' for a global flow. */
  project: string
}

function invokedWorkflowId(node: WorkflowSummary['nodes'][number]): string {
  if (node.type !== 'invoke_workflow') return ''
  const target = node.invoke?.workflow ?? node.invoke?.workflow_id
  return typeof target === 'string' ? target.trim() : ''
}

/** A read-only project projection. Workflow packages and work orders remain the facts.
 *
 *  `waits` is the project's "waiting on a person" set (`personWaits`: allowances that stopped, runs paused at a
 *  person's step), so a card's 等人处理 is the number 业务工单 shows for that flow. Without it, only what each order
 *  itself says is counted. */
export function projectWorkflowComposition(
  workflows: WorkflowSummary[],
  orders: WorkOrderSummary[],
  waits?: StoppedAllowances,
): ProjectWorkflowCard[] {
  const known = new Set(workflows.map(workflow => workflow.id))
  const counts = new Map<string, { total: number; open: number; waiting: number }>()
  for (const order of orders) {
    const current = counts.get(order.workflow_id) ?? { total: 0, open: 0, waiting: 0 }
    current.total += 1
    // One rule for where an order stands: the queue function 业务工单 sorts by, with the same set. (This used to test
    // for four statuses an order never has, so a closed order counted as open.)
    const queue = queueOf(order, waits)
    if (queue !== 'ended') current.open += 1
    if (queue === 'needs_person') current.waiting += 1
    counts.set(order.workflow_id, current)
  }

  return workflows.map(workflow => {
    const targets = Array.from(new Set(workflow.nodes.map(invokedWorkflowId).filter(Boolean)))
    const orderCount = counts.get(workflow.id) ?? { total: 0, open: 0, waiting: 0 }
    return {
      id: workflow.id,
      name: workflow.name || workflow.id,
      version: workflow.version,
      status: workflow.status,
      orderCount: orderCount.total,
      openOrderCount: orderCount.open,
      waitingOrderCount: orderCount.waiting,
      calls: targets.filter(target => known.has(target)),
      missingCalls: targets.filter(target => !known.has(target)),
      project: String(workflow.project_id ?? '').trim(),
    }
  })
}
