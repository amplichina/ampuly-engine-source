// 配置 → 数据与存储: which project, flow and order every piece of business data belongs to, as a tree (the prototype
// draws one). Pure, over two things the console already reads: the project index and the all-projects overview
// (`project_order_overview` -- order id, status, flow, acceptance status; nothing about customers or content).
//
// It states only what is known. A project the overview did not return (not asked yet, refused for a customer-bound
// account) has `known: false` and no children -- "not read" and "no orders" are different things.

import { queueOf, type StoppedAllowances } from './orderQueues'
import type { ProjectOverview } from './projectOverview'

type ProjectLike = { id: string; name?: string; customer_id?: string; lifecycle_status?: string }

export type DataOrder = { orderId: string; queue: 'needs_person' | 'in_progress' | 'ended' }
export type DataFlow = { flowId: string; orders: DataOrder[] }
export type DataProject = {
  id: string
  name: string
  customer: string
  lifecycle: string
  /** Whether the overview returned this project at all. */
  known: boolean
  truncated: boolean
  flows: DataFlow[]
  /** Where the project's files live, relative to the project data area (资料区): <project>/. Its own place on disk
   *  depends on the deployment (the local launcher: the system directory's workplace/; Docker: /data/workplace). */
  workspace: string
}

/** Projects in the index's own order; inside each, flows by id and orders by id, so two consoles draw the same tree.
 *
 *  `waitsByProject` holds the "waiting on a person" set (`personWaits`) of each project the console has one for -- the
 *  open project only: the overview carries neither pauses nor allowances. That project's orders are then in the queue
 *  业务工单 shows them in; any other project's are placed by what the order itself says. */
export function dataTree(
  projects: readonly ProjectLike[] | null | undefined,
  overview: ProjectOverview | null | undefined,
  waitsByProject?: ReadonlyMap<string, StoppedAllowances>,
): DataProject[] {
  return (projects ?? []).map(project => {
    const entry = overview?.[project.id]
    const waits = waitsByProject?.get(project.id)
    const byFlow = new Map<string, DataOrder[]>()
    for (const order of entry?.orders ?? []) {
      const flowId = (order.workflow_id ?? '').trim() || '—'
      const list = byFlow.get(flowId) ?? []
      list.push({ orderId: order.order_id, queue: queueOf(order, waits) })
      byFlow.set(flowId, list)
    }
    return {
      id: project.id,
      name: project.name?.trim() || project.id,
      customer: project.customer_id?.trim() ?? '',
      lifecycle: project.lifecycle_status?.trim() || 'active',
      known: Boolean(entry),
      truncated: entry?.truncated === true,
      flows: [...byFlow.entries()]
        .sort((a, b) => (a[0] < b[0] ? -1 : 1))
        .map(([flowId, orders]) => ({ flowId, orders: orders.sort((a, b) => (a.orderId < b.orderId ? -1 : 1)) })),
      workspace: `${project.id}/`,
    }
  })
}

export function treeTotals(tree: readonly DataProject[]): { projects: number; flows: number; orders: number } {
  return {
    projects: tree.length,
    flows: tree.reduce((sum, project) => sum + project.flows.length, 0),
    orders: tree.reduce((sum, project) => sum + project.flows.reduce((inner, flow) => inner + flow.orders.length, 0), 0),
  }
}
