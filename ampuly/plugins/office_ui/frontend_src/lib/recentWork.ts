// 总控's "继续上次的工作" (frontend refactor U4). Pure, over what the console already holds: the flows, the orders, and
// which flow this browser has an unsaved draft of. No new request and no "recently opened" record kept anywhere:
// what is offered is what is really unfinished or really latest.

type FlowLike = { id: string; name?: string; version: string; status: string }
type OrderLike = { order_id: string; status: string; customer_name?: string; brand_name?: string; workflow_id?: string; updated_at?: string }

export type RecentItem =
  | { kind: 'draft'; flowId: string; title: string; version: string }
  | { kind: 'flow'; flowId: string; title: string; version: string; status: string }
  | { kind: 'order'; orderId: string; title: string; status: string; flowId: string }

const ENDED = new Set(['closed', 'rejected'])

export function recentWork(
  flows: readonly FlowLike[] | null | undefined,
  orders: readonly OrderLike[] | null | undefined,
  state: { draftFlowIds: readonly string[]; lastBuilderFlowId: string | null },
  limit = 4,
): RecentItem[] {
  const items: RecentItem[] = []
  const byId = new Map((flows ?? []).map(flow => [flow.id, flow]))
  // 1. What would be lost: flows this browser holds unsaved changes to.
  for (const id of state.draftFlowIds) {
    const flow = byId.get(id)
    if (flow) items.push({ kind: 'draft', flowId: id, title: flow.name || id, version: flow.version })
  }
  // 2. The flow last worked on in the builder, unless it is already there as a draft. A retired flow is not work.
  const last = state.lastBuilderFlowId ? byId.get(state.lastBuilderFlowId) : undefined
  if (last && last.status !== 'retired' && !items.some(item => item.kind === 'draft' && item.flowId === last.id)) {
    items.push({ kind: 'flow', flowId: last.id, title: last.name || last.id, version: last.version, status: last.status })
  }
  // 3. The orders still going, latest first. An order with no timestamp sorts last rather than first.
  const open = (orders ?? []).filter(order => !ENDED.has(order.status))
    .slice().sort((a, b) => String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')))
  for (const order of open) {
    items.push({
      kind: 'order', orderId: order.order_id, status: order.status, flowId: order.workflow_id ?? '',
      title: [order.customer_name, order.brand_name].filter(Boolean).join(' · ') || order.order_id,
    })
  }
  return items.slice(0, Math.max(0, limit))
}
