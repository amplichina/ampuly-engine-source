// Whose turn an order is on (frontend refactor U3). Pure, over the pauses the console already holds.
//
// An order stopped at a human gate looked exactly like one that was running: both said 执行中, and the only hint was
// a count in the top bar's 待审批. The pauses are already in the console -- the approval centre and the browser pane
// both read them -- so 业务工单 can say which step is waiting and on whom, without asking for anything new.
//
// It never invents an owner for an order: the engine records no such person. The name shown is the signer the gate
// itself names, and when the gate names nobody, the order says it is waiting and leaves it at that.

type BlockerLike = {
  checkpoint_id?: string
  task_id?: string
  session_id?: string
  waiting_task_id?: string
  subject?: string
  created_at?: string
  snoozed_until?: string
  gate?: { node_id?: string; node_name?: string; version?: number; assignee?: string } | null
}

type OrderLike = { order_id: string; status: string; launched_task_id?: string }

// An ended order waits on nobody, whatever pauses still name it: a cancel closes its run's pauses, but one closed
// before that (or a pause the close could not reach) must not read 等人：browser_navigate on a closed row.
const ENDED = new Set(['closed', 'rejected'])

export type Waiting = {
  /** The step the run stopped at, when the pause names one. */
  step: string
  /** The signer the gate names, already turned into a person's name by the caller; '' when it names nobody. */
  who: string
  /** When the pause was raised, as the server wrote it; '' when it did not say. */
  since: string
  /** How many pauses this order has, when more than one waits. */
  count: number
  checkpointId: string
}

/** The pauses that belong to this order. The same match the browser pane makes: an approval may be anchored on the
 *  order's own runtime session rather than on the work item's task. */
export function blockersOfOrder<T extends BlockerLike>(order: OrderLike, blockers: readonly T[] | null | undefined): T[] {
  const ids = new Set([order.launched_task_id, order.order_id].filter(Boolean) as string[])
  if (!ids.size) return []
  return (blockers ?? []).filter(blocker =>
    ids.has(blocker.task_id ?? '') || ids.has(blocker.session_id ?? '') || ids.has(blocker.waiting_task_id ?? ''))
}

/** What to say about an order that is waiting on a person, or null when it is not. */
export function waitingOn(
  order: OrderLike,
  blockers: readonly BlockerLike[] | null | undefined,
  nameOf: (employeeId: string) => string = id => id,
): Waiting | null {
  if (ENDED.has(order.status)) return null
  const mine = blockersOfOrder(order, blockers)
  if (mine.length === 0) return null
  // The oldest first: it is the one that has been waiting longest, and the one to answer first.
  const sorted = [...mine].sort((a, b) => String(a.created_at ?? '').localeCompare(String(b.created_at ?? '')))
  const first = sorted[0]
  const assignee = String(first.gate?.assignee ?? '').trim()
  return {
    step: String(first.gate?.node_name ?? first.subject ?? '').trim(),
    who: assignee ? nameOf(assignee) : '',
    since: String(first.created_at ?? '').trim(),
    count: mine.length,
    checkpointId: String(first.checkpoint_id ?? '').trim(),
  }
}

/** The orders a person has to answer something on, for the queues to count. */
export function ordersWaitingOnPeople(orders: readonly OrderLike[] | null | undefined, blockers: readonly BlockerLike[] | null | undefined): Set<string> {
  const ids = new Set<string>()
  for (const order of orders ?? []) {
    if (!ENDED.has(order.status) && blockersOfOrder(order, blockers).length > 0) ids.add(order.order_id)
  }
  return ids
}
