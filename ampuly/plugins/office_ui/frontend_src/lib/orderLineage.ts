// An order's parent and children (frontend refactor U3). Pure, over the orders the page already holds.
//
// A flow with an "invoke another flow" step places child orders. The console was handed the whole lineage on every
// row -- the invoke node writes `_workflow_parent` into the child order's own fields -- and showed one number:
// "N child orders running", with nothing to click. When a child goes wrong, the first two questions are "who sent
// this out" and "which version of the upstream material did it get"; both answers were already in the payload.

/** What the invoke node records on a child order. Only what it actually writes is read here. */
export type ParentContract = {
  parent_order_id?: unknown
  node_id?: unknown
  target_workflow_id?: unknown
  artifact_refs?: unknown
}

type OrderLike = {
  order_id: string
  status: string
  workflow_id?: string
  customer_name?: string
  brand_name?: string
  fields?: Record<string, unknown> | null
}

export const PARENT_FIELD = '_workflow_parent'

export type Parent = { orderId: string; nodeId: string; assets: string[] }

/** The order that sent this one out, when this one is a child. */
export function parentOf(order: OrderLike): Parent | null {
  const raw = order.fields?.[PARENT_FIELD]
  if (!raw || typeof raw !== 'object') return null
  const contract = raw as ParentContract
  const orderId = typeof contract.parent_order_id === 'string' ? contract.parent_order_id.trim() : ''
  if (!orderId) return null
  const assets = Array.isArray(contract.artifact_refs)
    ? contract.artifact_refs.filter((item): item is string => typeof item === 'string' && !!item.trim()).map(item => item.trim())
    : []
  return { orderId, nodeId: typeof contract.node_id === 'string' ? contract.node_id.trim() : '', assets }
}

/** The orders this one sent out, in the order they arrived. */
export function childrenOf<T extends OrderLike>(order: OrderLike, orders: readonly T[] | null | undefined): T[] {
  return (orders ?? []).filter(item => item.order_id !== order.order_id && parentOf(item)?.orderId === order.order_id)
}

/** A short way to say which order this is, for a link: the customer if there is one, else the number. */
export function orderLabel(order: OrderLike | null | undefined): string {
  if (!order) return ''
  return [order.customer_name, order.brand_name].filter(Boolean).join(' · ') || order.order_id
}
