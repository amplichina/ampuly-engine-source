/** Where a project's numbers actually take you.
 *
 * The project context card counts what is inside a project -- runs, people
 * being waited on, orders, deliverables -- and each number is a way in. Until
 * now every number landed on a page and left the person to find the thing
 * again in a list, which is the same "understand session/task first" the card
 * exists to remove.
 *
 * The rule is deliberately narrow: when a number means exactly one thing, go
 * to that thing; when it means several, go to the list, because picking one
 * for somebody is guessing. */

export interface RunLike {
  taskId?: string
  status?: string
}

// Whether a reply is the project on screen's is lib/currentProject's question now (accepts), one gate for every
// reply handler; the per-reply rule that lived here moved there with its tests.

export function acceptsInterruptReceipt(reply: Record<string, unknown>, pending: {requestId: string; sessionId: string} | null): boolean {
  return !!pending && !!pending.requestId && reply.request_id === pending.requestId
    && reply.session_id === pending.sessionId
}

export interface BlockerLike {
  checkpoint_id?: string
}

const RUNNING_STATUSES = new Set(['running', 'in_progress'])

/** The runs a person would call "in flight" right now. */
export function runningRuns<T extends RunLike>(sessions: readonly T[] | undefined): T[] {
  return (sessions ?? []).filter(session => RUNNING_STATUSES.has(String(session.status ?? '')))
}

/** The one run to open, or '' when there is nothing to open or a choice to make. */
export function soleRunningTaskId(sessions: readonly RunLike[] | undefined): string {
  const running = runningRuns(sessions)
  if (running.length !== 1) return ''
  return String(running[0]?.taskId ?? '').trim()
}

/** The one thing waiting on a person, or '' when there is a choice to make. */
export function soleBlockerCheckpointId(blockers: readonly BlockerLike[] | undefined): string {
  const list = blockers ?? []
  if (list.length !== 1) return ''
  return String(list[0]?.checkpoint_id ?? '').trim()
}

/** What the button promises, so the click holds no surprise. */
export function entryHint(count: number, one: string, many: string, none: string): string {
  if (count <= 0) return none
  return count === 1 ? one : many
}
