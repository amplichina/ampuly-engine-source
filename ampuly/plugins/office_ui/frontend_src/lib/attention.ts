// 总控's landing: 需要关注 -- the orders that are waiting on a person, as the prototype's landing lists them. Pure,
// over what the console already holds (the orders, the pauses and the allowances); nothing is asked for.
//
// An order is here exactly when 业务工单 would sort it into 等人处理 -- its own step (lib/orderDrawer), a pause inside
// its run (lib/orderWaiting), or an allowance that stopped (lib/orderRows): the three things lib/orderQueues'
// personWaits is built from, not a fourth rule -- or when it is due. "Mine" is not claimed: the engine records no owner
// for an order, so the line says whose turn it is as far as the data names anyone (lib/orderTurn).

import { orderStep } from './orderDrawer'
import { dueState, type OrderHandling } from './orderHandling'
import { orderLabel } from './orderLineage'
import { stoppedAllowanceOrders } from './orderRows'
import { whoseTurn } from './orderTurn'
import { waitingOn } from './orderWaiting'

type OrderLike = Parameters<typeof orderStep>[0] & { customer_name?: string; brand_name?: string; reviewer_employee_id?: string; updated_at?: string; handling?: OrderHandling | null }
type BlockerLike = NonNullable<Parameters<typeof waitingOn>[1]>[number]
type BudgetLike = NonNullable<Parameters<typeof stoppedAllowanceOrders>[0]>[number]

export type AttentionItem = {
  orderId: string
  title: string
  /** What it is waiting for, in words. */
  why: string
  /** Whose turn, as far as the data names anyone. */
  who: string
  /** A pause inside the run (answered in the approval centre) rather than a step of the order itself. */
  paused: boolean
  /** The order is past, or on, its due date (someone recorded one). */
  overdue: boolean
}

const WHY = {
  intake_review: ['待审单', 'Intake review'],
  acceptance_decision: ['等验收决定', 'Acceptance decision'],
  paused: ['运行中暂停，等人决定', 'Paused inside the run'],
  // The allowance's third state: a paid call nobody can say went out or not. Nothing more is spent until a person
  // answers, on the order's row in 业务工单. The reason only: whose turn it is goes in `who`, not said twice.
  allowance: ['预算停了：有一次付费调用没确认', 'Allowance stopped: a paid call is not confirmed'],
  due: ['到期未结', 'Due and still open'],
} as const

// Only the owner's console is given the answer to an allowance (业务工单's 发出去了 / 没有发出去 buttons), so that is whose
// turn it is -- not the order's assignee, who cannot answer it, and not a role the product has nowhere else.
const ALLOWANCE_WHO = ['老板', 'The owner'] as const

export function attentionItems(
  orders: readonly OrderLike[] | null | undefined,
  blockers: readonly BlockerLike[] | null | undefined,
  budgets: readonly BudgetLike[] | null | undefined,
  nameOf: (employeeId: string) => string,
  zh: boolean,
  limit = 5,
  now: Date = new Date(),
): { items: AttentionItem[]; total: number } {
  const say = (words: readonly [string, string]) => words[zh ? 0 : 1]
  // Not read yet counts as none, as it does in 业务工单's queues.
  const stopped = stoppedAllowanceOrders(budgets)
  const found: Array<AttentionItem & { since: string }> = []
  for (const order of orders ?? []) {
    const step = orderStep(order)
    if (step.key === 'closed' || step.key === 'rejected') continue
    const waiting = waitingOn(order, blockers, nameOf)
    const allowance = stopped.has(order.order_id)
    // An order past (or on) the date someone set for it needs a look even while the run has it.
    const due = dueState(order.handling?.due_at, now, zh)
    const late = due.kind === 'overdue' || due.kind === 'today'
    if (!waiting && !step.person && !allowance && !late) continue
    const turn = whoseTurn(step, waiting, order.reviewer_employee_id ? nameOf(order.reviewer_employee_id) : '', zh)
    const reason = waiting ? WHY.paused : step.person ? WHY[step.key as 'intake_review' | 'acceptance_decision'] : allowance ? WHY.allowance : WHY.due
    let why = say(reason) + (waiting?.step ? `：${waiting.step}` : '')
    // A stopped allowance behind another reason is still said: answering the first leaves the order stuck on it.
    if (allowance && reason !== WHY.allowance) why += ` · ${say(WHY.allowance)}`
    if (late) why += reason === WHY.due ? `：${due.text}` : ` · ${due.text}`
    found.push({
      orderId: order.order_id,
      title: orderLabel(order),
      why,
      // While the run has it, the person to look is the assignee, when there is one.
      who: waiting || step.person ? turn.who
        : allowance ? say(ALLOWANCE_WHO)
        : order.handling?.assignee_employee_id ? nameOf(order.handling.assignee_employee_id) : turn.who,
      paused: Boolean(waiting),
      overdue: late,
      since: waiting?.since || String(order.updated_at ?? ''),
    })
  }
  // Late orders first, then the one that has waited longest; an item with no time sorts last rather than first.
  found.sort((a, b) => Number(b.overdue) - Number(a.overdue) || (a.since || '\uffff').localeCompare(b.since || '\uffff') || (a.orderId < b.orderId ? -1 : 1))
  return { items: found.slice(0, Math.max(0, limit)).map(({ since: _since, ...item }) => item), total: found.length }
}
