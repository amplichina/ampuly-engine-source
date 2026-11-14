// 客户页「我的单」 (frontend redesign S3-6B, policy; FRONTEND-IA- 「员工页与客户页」, 「第二轮定稿」 row
// 员工页、客户页). Pure: which account sees the page, what one order looks like on it, and which of its five states an
// order is in. The page reads the customer projection (customer_view.build_customer_view) as customer_orders_view
// answers it (S3-6A, built in parallel): tolerant of the list's key and of a field that is not there, strict about the
// types of what is there, and it never carries an id onto the page -- the order id is kept only as the key
// customer_accept names the order by.

import type { AuthAccount } from '../components/LoginScreen'

export interface CustomerOrder {
  /** The key customer_accept names the order by; never shown. */
  orderId: string
  /** What the order is called to its customer (the brand name, else the server's title); '' when neither came. */
  name: string
  /** The order's status word (orders.status.*), as the server names it. */
  status: string
  /** One sentence of where the order stands: the server's own when it sent one, else the last stage the customer is shown. */
  stage: string
  deliverables: { name: string; at: string }[]
  criteria: string[]
  round: number
  /** The acceptance record's status (orders.acceptance.*): not_started, criteria_defined, submitted, accepted ... */
  acceptance: string
  decidedAt: string
  note: string
  /** Whether it is the customer's turn: acceptance submitted for their decision. */
  canAccept: boolean
}

export type CustomerOrderState = 'waiting_you' | 'accepted_waiting_owner' | 'closed' | 'sent_back' | 'in_progress'

/** Whether an account sees 我的单 instead of the console: the customer role (S3-6A) and nothing else. The role table
 *  (api.role_allows) answers customer_orders_view / customer_accept to that role alone, so an employee row with
 *  customers bound and no employee behind it (an imported accounts.json row from before the role) keeps the employee's
 *  page: read as a customer it would only ever see a refusal. */
export function isCustomerAccount(account: Pick<AuthAccount, 'role'> | null | undefined): boolean {
  return account?.role === 'customer'
}

const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

function parseOrder(value: unknown): CustomerOrder | null {
  if (!isRecord(value)) return null
  const orderId = text(value.order_id)
  if (!orderId) return null
  const acceptance = isRecord(value.acceptance) ? value.acceptance : {}
  const stages = Array.isArray(value.stages) ? value.stages.filter(isRecord) : []
  const lastStage = stages.length ? text(stages[stages.length - 1].stage) : ''
  const criteriaRaw = Array.isArray(value.criteria) ? value.criteria : Array.isArray(acceptance.criteria) ? acceptance.criteria : []
  const roundRaw = value.round ?? acceptance.round_no
  const round = typeof roundRaw === 'number' && Number.isFinite(roundRaw) && roundRaw >= 1 ? Math.floor(roundRaw) : 1
  const deliverables = Array.isArray(value.deliverables)
    ? value.deliverables.filter(isRecord).map(item => ({ name: text(item.name), at: text(item.at) })).filter(item => item.name)
    : []
  return {
    orderId,
    name: text(value.brand_name) || text(value.title) || text(value.order_title),
    status: text(value.status),
    stage: text(value.stage) || text(value.stage_sentence) || lastStage,
    deliverables,
    criteria: criteriaRaw.filter((item): item is string => typeof item === 'string').map(item => item.trim()).filter(Boolean),
    round,
    acceptance: text(acceptance.status) || 'not_started',
    decidedAt: text(acceptance.decided_at),
    note: text(acceptance.note),
    canAccept: value.can_accept === true,
  }
}

/** The orders a customer_orders_view reply carries, under whichever key the server put them (orders / views, or one
 *  view). An entry that is not an order is left out; an entry that is one is read with its missing fields empty. */
export function parseCustomerOrders(payload: Record<string, unknown>): CustomerOrder[] {
  const list = Array.isArray(payload.orders) ? payload.orders : Array.isArray(payload.views) ? payload.views : isRecord(payload.view) ? [payload.view] : []
  return list.map(parseOrder).filter((item): item is CustomerOrder => item !== null)
}

/** Which of the page's five sentences an order gets. A closed order is closed whatever its acceptance says; the
 *  customer's turn comes only from can_accept (the server's word on it); an accepted round waits on the owner's close
 *  (the customer's confirmation only records, 老板确认收货后才结案). */
export function customerOrderState(order: Pick<CustomerOrder, 'status' | 'acceptance' | 'canAccept'>): CustomerOrderState {
  if (order.status === 'closed') return 'closed'
  if (order.canAccept) return 'waiting_you'
  if (order.acceptance === 'accepted' || order.acceptance === 'accepted_with_exceptions') return 'accepted_waiting_owner'
  if (order.acceptance === 'rejected') return 'sent_back'
  return 'in_progress'
}
