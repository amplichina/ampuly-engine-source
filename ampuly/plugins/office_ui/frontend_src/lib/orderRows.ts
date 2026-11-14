// 业务工单's rows (frontend refactor U3). Pure.
//
// A row is one line until asked: its tools (review, cancel, budget, template, savings) unfold when the person asks,
// when the drawer sends them to the table to act -- and on their own when the order's allowance has stopped, because
// that is a thing waiting for a person and must not sit folded away.

type BudgetLike = { order_id: string; state: string }

export function toggled(open: readonly string[], orderId: string): string[] {
  return open.includes(orderId) ? open.filter(id => id !== orderId) : [...open, orderId]
}

/** An allowance that is neither open nor closed (stopped, exhausted, whatever the server calls it) wants a person. */
export function allowanceWantsPerson(orderId: string, budgets: readonly BudgetLike[] | null | undefined): boolean {
  return (budgets ?? []).some(budget => budget.order_id === orderId && budget.state !== 'open' && budget.state !== 'closed')
}

export function rowShowsTools(orderId: string, open: readonly string[], budgets: readonly BudgetLike[] | null | undefined): boolean {
  return open.includes(orderId) || allowanceWantsPerson(orderId, budgets)
}

/** The orders whose allowance is waiting for a person, for the queues to count. The same rule as above, once over. */
export function stoppedAllowanceOrders(budgets: readonly BudgetLike[] | null | undefined): Set<string> {
  const ids = new Set<string>()
  for (const budget of budgets ?? []) {
    if (budget.state !== 'open' && budget.state !== 'closed') ids.add(budget.order_id)
  }
  return ids
}
