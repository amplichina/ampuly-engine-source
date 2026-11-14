// 业务工单's queues and search (frontend refactor U3, first cut). Pure, over the orders the page already has: no new
// request, no new status. The queues are made of the server's own statuses -- the owner's prototype has queues
// (待分派 / 已取消 …) the engine has no status for, and those are not invented here.

import { stoppedAllowanceOrders } from './orderRows'
import { ordersWaitingOnPeople } from './orderWaiting'

export const ORDER_QUEUES = ['all', 'needs_person', 'in_progress', 'ended'] as const
export type OrderQueue = typeof ORDER_QUEUES[number]

export const ORDER_QUEUE_LABELS: Record<OrderQueue, readonly [string, string]> = {
  all: ['全部', 'All'],
  needs_person: ['等人处理', 'Needs a person'],
  in_progress: ['进行中', 'In progress'],
  ended: ['已结束', 'Ended'],
}

type OrderLike = {
  order_id: string
  status: string
  workflow_id?: string
  customer_name?: string
  brand_name?: string
  customer_id?: string
  acceptance?: { status?: string } | null
}

const ENDED = new Set(['closed', 'rejected'])

/** Orders whose allowance has stopped and is waiting for a person (neither open nor closed -- the server's third
 *  state is a spend the run could not verify). Passing this in is optional: without it the queues say what the order
 *  itself says, which is what pages that do not hold the allowances can honestly claim. */
export type StoppedAllowances = ReadonlySet<string>

/** Everything inside an order that waits on a person beyond what the order's own status says: an allowance that
 *  stopped (neither open nor closed), and a pause its run raised at a person's step. Every page that counts the queues
 *  of one project -- 业务工单 and 项目中心 -- builds its set here from the same three things the console holds, so the
 *  same orders land in the same queue on both. Missing allowances or pauses (not read yet) count as none. */
export function personWaits(
  orders: Parameters<typeof ordersWaitingOnPeople>[0],
  budgets: Parameters<typeof stoppedAllowanceOrders>[0],
  blockers: Parameters<typeof ordersWaitingOnPeople>[1],
): Set<string> {
  return new Set([...stoppedAllowanceOrders(budgets), ...ordersWaitingOnPeople(orders, blockers)])
}

/** An order waits on a person when its intake has not been reviewed yet, its acceptance was submitted and nobody has
 *  decided it, or the caller's set says so (`personWaits`: its allowance stopped, or its run paused at a person's
 *  step). Without the set, only what the order itself says is counted. */
export function needsPerson(order: OrderLike, stopped?: StoppedAllowances): boolean {
  if (ENDED.has(order.status)) return false
  if (stopped?.has(order.order_id)) return true
  return order.status === 'pending_review' || order.acceptance?.status === 'submitted'
}

export function queueOf(order: OrderLike, stopped?: StoppedAllowances): Exclude<OrderQueue, 'all'> {
  if (ENDED.has(order.status)) return 'ended'
  return needsPerson(order, stopped) ? 'needs_person' : 'in_progress'
}

export function queueCounts(orders: readonly OrderLike[], stopped?: StoppedAllowances): Record<OrderQueue, number> {
  const counts: Record<OrderQueue, number> = { all: orders.length, needs_person: 0, in_progress: 0, ended: 0 }
  for (const order of orders) counts[queueOf(order, stopped)] += 1
  return counts
}

/** How many of these orders have not ended, by the queue function: a count that sits beside the queues (项目中心's
 *  「工单 N/M 未完」) ends an order exactly where 已结束 does. (It used to end one on four statuses no order ever has, so
 *  a closed or rejected order was still counted as not finished.) */
export function openOrderCount(orders: readonly OrderLike[] | null | undefined): number {
  return (orders ?? []).filter(order => queueOf(order) !== 'ended').length
}

/** Whether an `order_list` reply listed only the latest orders (its `truncated`), so the queues above count only
 *  those. Only a list that was given and says so: a reply without the field, a refusal, or a flag that is not
 *  plainly true is a list taken as whole. */
export function listedOnlyLatest(reply: { ok?: unknown; truncated?: unknown }): boolean {
  return reply.ok === true && reply.truncated === true
}

/** 4.7-A08 「查看更早的工单」: where the page's order list stands between replies. `nextCursor` is where the next
 *  older page starts ('' = nothing older, or the server did not say); `olderLoaded` is whether older pages were
 *  added below the first; `scope` names the project and filters the rows belong to; `loading` a request in flight. */
export interface OrderPaging {
  nextCursor: string
  olderLoaded: boolean
  scope: string
  loading: boolean
}

export const NO_ORDER_PAGING: OrderPaging = { nextCursor: '', olderLoaded: false, scope: '', loading: false }

type OrderListReply = { ok?: unknown; orders?: unknown; before?: unknown; next_cursor?: unknown; truncated?: unknown; project_id?: unknown; customer_id?: unknown; environment_id?: unknown }

function replyScope(reply: OrderListReply): string {
  return [reply.project_id, reply.customer_id, reply.environment_id].map(value => String(value ?? '')).join('\n')
}

/**
 * Fold one `order_list` reply into the orders the page holds. Returns null for a reply to drop: an older page that
 * answers a cursor the page no longer waits on (a double click, or the filters changed since).
 *
 * - An older page (`before`) is added below, skipping any order already listed, and moves the cursor on.
 * - A first page replaces the list -- except that the older pages already loaded (same project and filters) stay
 *   below it, so a refresh after 「查看更早的工单」 neither drops them nor opens a gap between the two.
 * - `truncated` is what the page says under the list: more orders exist than it holds.
 */
export function foldOrderPage<T extends { order_id: string }>(
  held: readonly T[] | null | undefined,
  paging: OrderPaging,
  reply: OrderListReply,
): { orders: T[]; paging: OrderPaging; truncated: boolean } | null {
  const before = typeof reply.before === 'string' ? reply.before : ''
  const incoming = reply.ok === true && Array.isArray(reply.orders) ? (reply.orders as T[]) : []
  const cursor = typeof reply.next_cursor === 'string' ? reply.next_cursor : ''
  const scope = replyScope(reply)
  if (before) {
    if (before !== paging.nextCursor || scope !== paging.scope) return null
    if (reply.ok !== true) {
      // The older page failed: keep what is listed and the cursor, so the button can be pressed again.
      return { orders: [...(held ?? [])], paging: { ...paging, loading: false }, truncated: paging.nextCursor !== '' }
    }
    const known = new Set((held ?? []).map(order => order.order_id))
    const added = incoming.filter(order => !known.has(order.order_id))
    const next = { nextCursor: cursor, olderLoaded: true, scope, loading: false }
    return { orders: [...(held ?? []), ...added], paging: next, truncated: cursor !== '' }
  }
  // Kept only while the older rows are really still held: a cursor whose rows are gone would skip everything between.
  if (reply.ok === true && paging.olderLoaded && scope === paging.scope && (held?.length ?? 0) > 0) {
    const fresh = new Set(incoming.map(order => order.order_id))
    const kept = (held ?? []).filter(order => !fresh.has(order.order_id))
    return { orders: [...incoming, ...kept], paging: { ...paging, loading: false }, truncated: paging.nextCursor !== '' }
  }
  const next = { nextCursor: reply.ok === true ? cursor : '', olderLoaded: false, scope, loading: false }
  return { orders: incoming, paging: next, truncated: listedOnlyLatest(reply) }
}

/** The flows that have an order here, most orders first then by id, for a filter to offer. */
export function flowsWithOrders(orders: readonly OrderLike[] | null | undefined): string[] {
  const counts = new Map<string, number>()
  for (const order of orders ?? []) {
    const id = (order.workflow_id ?? '').trim()
    if (id) counts.set(id, (counts.get(id) ?? 0) + 1)
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).map(([id]) => id)
}

/** The orders a queue and a search show, in the order they came. Every word typed must be found in the order's id,
 *  customer, brand or flow. */
export function shownOrders<T extends OrderLike>(orders: readonly T[], queue: OrderQueue, query: string, stopped?: StoppedAllowances, flowId = ''): T[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  return orders.filter(order => {
    if (queue !== 'all' && queueOf(order, stopped) !== queue) return false
    // Match the exact flow id so a prefix shared with another id cannot broaden a project drill-down.
    if (flowId && (order.workflow_id ?? '') !== flowId) return false
    if (words.length === 0) return true
    const text = [order.order_id, order.customer_name, order.brand_name, order.workflow_id, order.customer_id].filter(Boolean).join('\n').toLowerCase()
    return words.every(word => text.includes(word))
  })
}
