import type { WorkOrderSummary } from '../components/WorkflowsPage'

type Acceptance = NonNullable<WorkOrderSummary['acceptance']>

/** True when this acceptance came from a verifying read (acceptance_get / decide / rework): it carries the verified
 *  files and byte comparisons that the order list never does. */
export function hasVerifiedDetail(acceptance: Acceptance | null | undefined): boolean {
  if (!acceptance) return false
  if (acceptance.artifact_snapshot?.verified === true) return true
  return (acceptance.history ?? []).some(round => round.artifact_snapshot?.verified === true)
}

/** Same round, same decision, same evidence: nothing the verified detail describes has moved. */
export function sameAcceptanceState(a: Acceptance | null | undefined, b: Acceptance | null | undefined): boolean {
  if (!a || !b) return false
  return a.status === b.status
    && (a.round_no ?? 1) === (b.round_no ?? 1)
    && a.evidence_hash === b.evidence_hash
    && a.criteria_hash === b.criteria_hash
    && (a.history?.length ?? 0) === (b.history?.length ?? 0)
}

/**
 * Take a fresh order list without throwing away what a verifying read already showed.
 *
 * The list is the authority on every order, but its acceptance is the unverified summary: replacing state with it
 * made the history files, the byte comparison, the round export and the delivery bundle vanish after every decision
 * until the panel was closed and opened again. An order whose acceptance has not moved keeps its verified detail; one
 * that has moved takes the list's summary and is named in `reverify`, so the caller can ask for a verified read again.
 */
export function mergeOrderList(
  previous: WorkOrderSummary[] | null | undefined,
  incoming: WorkOrderSummary[],
): { orders: WorkOrderSummary[]; reverify: string[] } {
  const before = new Map((previous ?? []).map(order => [order.order_id, order.acceptance] as const))
  const reverify: string[] = []
  const orders = incoming.map(order => {
    const known = before.get(order.order_id)
    if (!hasVerifiedDetail(known) || !order.acceptance) return order
    if (sameAcceptanceState(known, order.acceptance)) return { ...order, acceptance: known }
    reverify.push(order.order_id)
    return order
  })
  return { orders, reverify }
}
