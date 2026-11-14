/** A scoped server observation, not an inferred approval decision. */
export type PendingBadgeView = { count: number; visible: boolean; text: string; stale: boolean }

export function stalePendingBadge(previous: PendingBadgeView | null): PendingBadgeView {
  return {
    count: previous?.count ?? 0,
    visible: true,
    text: previous && previous.count > 0 ? `待审批 ${previous.count}（状态待核对）` : '待审批状态待核对',
    stale: true,
  }
}

export function pendingBadgeServerObservation(
  raw: unknown, projectId: string, actorLogin: string, previous: PendingBadgeView | null,
): PendingBadgeView {
  if (!raw || typeof raw !== 'object') return stalePendingBadge(previous)
  const row = raw as Record<string, unknown>
  if (row.project_id !== projectId || row.actor_login !== actorLogin
    || row.count_exact !== true || row.limited === true
    || typeof row.count !== 'number' || !Number.isSafeInteger(row.count) || row.count < 0) {
    return stalePendingBadge(previous)
  }
  return { count: row.count, visible: row.count > 0, text: `待审批 ${row.count}`, stale: false }
}
