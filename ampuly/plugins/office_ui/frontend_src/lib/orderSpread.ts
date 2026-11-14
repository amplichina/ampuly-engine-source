// What one page hands 业务工单 when a count or a row on it is a way into the inbox (frontend refactor U3; redrawn
// S3-2B, 2026-09-27). Pure. 项目中心's own counting (业务分布 and its bars) is gone: every number on that page is the
// server's `standing` now (lib/projectOverview), and this module keeps only the link.

import { INBOX_STATES } from './ordersInbox'

export interface OrdersTarget {
  /** One of the old queue names (`queue:` form); 'all' otherwise. The inbox maps it onto its states. */
  queue: string
  /** An inbox state named directly (`state:` form: mine / risk / live / customer / done …); '' for none. */
  state: string
  query: string
  open: string
  flow: string
}

/** What another page hands 业务工单: a queue to stand in, an inbox state, a flow to narrow to, or one order to open.
 *  Several parts may be joined with `;` (`state:risk;flow:document-review`). Opening one order stands in 全部 with no search,
 *  so the order is there to be opened whatever group it is in. Anything unknown changes nothing (null). */
export function ordersTarget(target: string | undefined, queues: readonly string[]): OrdersTarget | null {
  if (!target) return null
  const out: OrdersTarget = { queue: 'all', state: '', query: '', open: '', flow: '' }
  let known = 0
  for (const part of target.split(';')) {
    const colon = part.indexOf(':')
    if (colon < 0) return null
    const key = part.slice(0, colon)
    const value = part.slice(colon + 1).trim()
    if (!value) return null
    if (key === 'queue' && queues.includes(value)) out.queue = value
    else if (key === 'state' && (INBOX_STATES as readonly string[]).includes(value)) out.state = value
    // A flow is named exactly, not typed into the search box: one flow's id can be a prefix of another's.
    else if (key === 'flow') out.flow = value
    else if (key === 'order') out.open = value
    else return null
    known += 1
  }
  return known > 0 ? out : null
}
