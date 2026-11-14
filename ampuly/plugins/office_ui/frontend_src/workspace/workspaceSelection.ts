import type { Session } from '../types/kanban'

// The workspace page's pure decisions about which sessions and which board to show. They live apart from the page so
// they can be tested without loading it: the page pulls in the run-graph canvas, whose stylesheet import a plain Node
// test run cannot load -- which is how their test stopped running without anybody noticing.

export const MULTI_SESSION_PREVIEW_LIMIT = 4

export function selectMultiViewSessions(
  openSessions: Session[],
  activeSessionId: string | null,
  limit = MULTI_SESSION_PREVIEW_LIMIT,
): Session[] {
  const boundedLimit = Math.max(0, Math.floor(limit))
  if (boundedLimit === 0 || openSessions.length === 0) return []
  if (openSessions.length <= boundedLimit) return openSessions

  const activeSession = activeSessionId
    ? openSessions.find(session => session.taskId === activeSessionId)
    : undefined
  const inactiveSlots = boundedLimit - (activeSession ? 1 : 0)
  const recentInactive = inactiveSlots > 0
    ? openSessions
      .filter(session => session.taskId !== activeSession?.taskId)
      .slice(-inactiveSlots)
    : []
  return activeSession ? [...recentInactive, activeSession] : recentInactive
}

/** The flow package an order launch pinned on its own run (``workflow_contract.workflow_id``), or ''. */
function launchedFlowOf(session: Session | undefined): string {
  return String(session?.workflowContract?.workflow_id ?? '').trim()
}

/**
 * Whether a session belongs to an order (policy, R1 option B: work starts from 业务工单, so only an order's
 * session keeps its composer; any other session is listed and readable, never written to). The marker is the one the
 * order launch writes: ``workflow_contract`` on the order's own task (ws_handler._launch_workflow_package ->
 * _apply_workflow_contract), which reaches the console on every session row of collab_sync and on the session_updated
 * the launch sends right after it. The runtime tasks under an order do not copy it: they name the order's task as
 * ``origin_task_id`` and the order's session as ``parent_session_id`` -- the same two roads the server reads
 * (collaboration._order_flow_package_id). A free session (create_session), a run 安安 dispatched from 总控 and a
 * card of the retired board carry none.
 */
export function sessionBelongsToOrder(session: Session | null | undefined, sessions: Session[]): boolean {
  if (!session) return false
  if (launchedFlowOf(session)) return true
  const origin = String(session.originTaskId ?? '').trim()
  if (origin && origin !== session.taskId && launchedFlowOf(sessions.find(item => item.taskId === origin))) return true
  const parent = String(session.parentSessionId ?? '').trim()
  return !!parent && sessions.some(item => item.sessionId === parent && launchedFlowOf(item) !== '')
}

export function resolveWorkspaceBoardId({
  boardIds,
  sessionScopedBoardIds,
  activeSessionBoardId,
  activeTaskBoardId,
  currentActiveBoardId,
}: {
  boardIds: string[]
  sessionScopedBoardIds: string[]
  activeSessionBoardId: string | null
  activeTaskBoardId: string | null
  currentActiveBoardId: string | null
}): string | null {
  const available = new Set(boardIds)
  if (activeTaskBoardId && available.has(activeTaskBoardId)) return activeTaskBoardId
  if (activeSessionBoardId && available.has(activeSessionBoardId)) return activeSessionBoardId

  // Once the backend exposes session-scoped boards, an absent session board
  // means that session's projection has not arrived yet.  Showing any other
  // session's board during that gap leaks work items across chats.
  if (sessionScopedBoardIds.length > 0) return null

  if (currentActiveBoardId && available.has(currentActiveBoardId)) return currentActiveBoardId
  return boardIds[0] ?? null
}
