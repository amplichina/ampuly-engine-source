// The top bar's 执行区 entry (frontend refactor U1, second cut; Computer Use addendum §1). Pure.
//
// The prototype's execution workspace is a demo; what exists here is real: controlled browser sessions that a running
// step opened, each of which can be watched and, when it asks, taken over by a person. The entry lists those -- it
// creates nothing, and picking one grants nothing: the server checks the owner on every browser action.

type SessionLike = { binding_key: string; state: string; live?: boolean; task_id?: string; order_id?: string; anchor_session_id?: string; note?: string; node_name?: string }

export type ExecutionEntry<T extends SessionLike> = {
  /** Live sessions, the ones waiting on a person first. */
  sessions: T[]
  /** How many of them wait on a person: the number the entry shows. */
  waiting: number
}

export function executionEntry<T extends SessionLike>(sessions: readonly T[] | null | undefined): ExecutionEntry<T> {
  const live = (sessions ?? []).filter(session => session.live === true)
  const waitsOnPerson = (session: T) => session.state === 'awaiting_human'
  const ordered = [...live.filter(waitsOnPerson), ...live.filter(session => !waitsOnPerson(session))]
  return { sessions: ordered, waiting: live.filter(waitsOnPerson).length }
}

/** The page 执行工具 → 浏览器 opens (Q05 B5): one of the order being looked at -- a step waiting on a person first --
 *  and only when that order has none, the first live page. `currentTaskId` is the open session's task (an order's root
 *  task, which a work item's session carries as its order_id). */
export function browserForOrder<T extends SessionLike>(sessions: readonly T[] | null | undefined, currentTaskId: string | null | undefined): T | undefined {
  const live = executionEntry(sessions).sessions
  const current = String(currentTaskId ?? '').trim()
  if (current) {
    // the order's root task, the work item's own task, or the order's run session (as the side pane reads them)
    const mine = live.find(session => session.order_id === current || session.task_id === current || session.anchor_session_id === current)
    if (mine) return mine
  }
  return live[0]
}

export const SESSION_STATE_LABELS: Record<string, readonly [string, string]> = {
  awaiting_human: ['等人接管', 'Waiting for a person'],
  agent_operating: ['AI 操作中', 'The agent is operating'],
  identity_verified: ['已核对身份', 'Identity checked'],
  ready: ['就绪', 'Ready'],
}

/** A state this page has not heard of is shown as it is, never translated into something it may not mean. */
export function sessionStateLabel(state: string, zh: boolean): string {
  const known = SESSION_STATE_LABELS[state]
  return known ? known[zh ? 0 : 1] : state
}
