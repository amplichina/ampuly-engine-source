/** Per-account, per-browser presentation preference. Never changes channel delivery or task facts. */
type Store = Pick<Storage, 'getItem' | 'setItem'>
export function quietPreferenceKey(principal: string): string | null {
  return principal.trim() ? `superones.quietCompletion.v1:${encodeURIComponent(principal.trim())}` : null
}
export function loadQuietCompletion(storage: Pick<Storage, 'getItem'> | null | undefined, principal: string): boolean {
  const key = quietPreferenceKey(principal)
  try { return key !== null && storage?.getItem(key) === 'true' } catch { return false }
}
export function saveQuietCompletion(storage: Store | null | undefined, principal: string, value: boolean): boolean {
  const key = quietPreferenceKey(principal)
  if (!key || !storage || typeof value !== 'boolean') return false
  try { storage.setItem(key, String(value)); return storage.getItem(key) === String(value) } catch { return false }
}
/** Only explicit, matching-project completion of another known task is a non-safety background reminder. */
export function shouldNotifyBackgroundCompletion(input: {
  payload: Record<string, unknown>; projectId: string; activeTaskId: string; quiet: boolean
}): boolean {
  const { payload, projectId, activeTaskId, quiet } = input
  return !quiet && !!projectId.trim() && payload.project_id === projectId
    && payload.notification_kind === 'task_complete'
    && typeof payload.task_id === 'string' && !!payload.task_id.trim()
    && payload.task_id !== activeTaskId
    && !payload.error && !payload.blocked_reason
    && (!payload.status || ['completed', 'done', 'succeeded', 'success'].includes(String(payload.status)))
}
