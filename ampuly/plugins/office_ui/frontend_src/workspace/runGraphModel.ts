import type { RoleWorkItemRow, Session } from '../types/kanban'

const HUMAN_REVIEW_STATUSES: ReadonlySet<string> = new Set([
  'awaiting_review',
  'awaiting_manager_review',
  'awaiting_human',
])

/** What a person can do to the session behind a node right now. Mirrors
 *  the task header's rules so the graph never offers a button the header
 *  would refuse. */
export interface NodeSessionActions {
  session?: Session
  /** Checkpoints waiting on a person in this session (approvals, questions). */
  pendingCount: number
  /** True while the session is actually executing. */
  live: boolean
  canStop: boolean
  canResume: boolean
}

/** A work item's session is the one whose task is its execution turn; a
 *  never-dispatched item falls back to its role's resident session. */
export function sessionForRow(
  row: RoleWorkItemRow,
  roleSessionId: string | undefined,
  sessions: Session[],
): Session | undefined {
  const turnId = String(row.executionTurnId ?? '').trim()
  if (turnId) {
    const byTurn = sessions.find(session => session.taskId === turnId)
    if (byTurn) return byTurn
  }
  const roleKey = String(roleSessionId ?? '').trim()
  if (roleKey) {
    return sessions.find(session => session.sessionId === roleKey || session.taskId === roleKey)
  }
  return undefined
}

export function actionsForSession(session: Session | undefined): NodeSessionActions {
  if (!session) return { pendingCount: 0, live: false, canStop: false, canResume: false }
  const control = session.runtimeControlState ?? (session.status === 'running' ? 'running' : 'idle')
  const suspending = control === 'suspending'
  const resuming = control === 'resuming'
  const suspended = control === 'suspended'
  const running = session.status === 'running' && !suspending && !suspended && !resuming
  const awaitingReview = HUMAN_REVIEW_STATUSES.has(session.status)
  const canStop = (session.canStop ?? session.status === 'running') && !suspending && !suspended && !resuming
  const canResume = (
    session.canResume
    ?? (suspended || (!awaitingReview && !running && session.status !== 'done' && session.status !== 'pending'))
  ) && !suspending && !resuming
  const pendingCount = Math.max(
    Number(session.pendingPermissionCount ?? 0) || 0,
    Array.isArray(session.permissionRequests) ? session.permissionRequests.length : 0,
  )
  return { session, pendingCount, live: running, canStop, canResume }
}
