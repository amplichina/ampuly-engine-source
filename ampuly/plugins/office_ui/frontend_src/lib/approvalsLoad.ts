// 待审批 -- what the page shows while it waits for its list, and when it stops waiting.
//
// The page sat on 正在加载审批状态… for up to twenty seconds after every open. The first blockers_list goes out the
// moment the socket connects; the project index then arrives and starts the initial project switch; and the reply
// to that first request is dropped by the scope guard (a reply that lands while a switch is pending is never
// applied -- lib/projectNavigation.ts acceptsProjectResponse). Nothing asked again until the 20 s poll, so the
// cards appeared only on 刷新 or a good while later. Two rules, pure here: ask again the moment a switch settles
// (App.tsx counts those), and after APPROVALS_WAIT_MS with nothing back say so and offer 刷新 instead of spinning.
//
// S2-B (2026-09-27): the wait is the request tracker's list timeout (lib/requestTracker, 列表读取 5 s). The page's ask
// goes through the tracker; a read that timed out, or that never left because the console was not connected, is
// 「没收到回执」 with a 刷新 button -- never a 正在加载 that stays.

import { REQUEST_TIMEOUT_MS, type RequestState } from './requestTracker'

/** How long the page waits for the list before it says "nothing back yet" (ms): the tracker's list read timeout. */
export const APPROVALS_WAIT_MS = REQUEST_TIMEOUT_MS.list

export type ApprovalsView = 'error' | 'list' | 'loading' | 'stale' | 'empty'

/**
 * Which body the page shows. A list already on screen is never replaced by a spinner or a "nothing back yet": the
 * poll refreshes it in place. An error outranks everything, as it did.
 */
export function approvalsView(input: { loading: boolean; stale?: boolean; error?: string | null; count: number }): ApprovalsView {
  if (input.error) return 'error'
  if (input.count > 0) return 'list'
  if (input.loading) return input.stale ? 'stale' : 'loading'
  return 'empty'
}

/** True once a request sent at `askedAt` has waited `waitMs` with nothing back. */
export function waitedTooLong(askedAt: number, now: number, waitMs: number = APPROVALS_WAIT_MS): boolean {
  return askedAt > 0 && now - askedAt >= waitMs
}

/** The tracker's word on the list read, as the page's `stale`: nothing came back in time, or it never went out. */
export function listWaitedOut(state: RequestState): boolean {
  return state === 'timeout' || state === 'unknown'
}
