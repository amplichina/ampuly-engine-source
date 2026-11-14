// One tab edits a workflow at a time -- in this browser. The editing tab writes a small record to browser storage and
// renews it; another tab that sees a live record shows the canvas read-only, says why, and may take over. Nothing
// here reaches the server: two machines editing the same flow are still caught by the server's version check when
// the second one saves. Storage may be missing or blocked; then every tab is simply free to edit, as before.

export interface EditLock {
  tabId: string
  /** Epoch milliseconds of the last renewal. */
  at: number
}

export type LockState = 'free' | 'mine' | 'other'

const PREFIX = 'superones.builderEditLock.v1'
/** A record not renewed for this long is dead: a closed tab or a crashed browser never locks a flow for good. */
export const LOCK_TTL_MS = 30_000
export const LOCK_RENEW_MS = 10_000

export function lockKey(workflowId: string): string {
  return `${PREFIX}:${workflowId}`
}

/** The workflow a storage key is the lock of, or null for any other key. */
export function workflowOfLockKey(key: string | null): string | null {
  return key && key.startsWith(`${PREFIX}:`) ? key.slice(PREFIX.length + 1) : null
}

type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function storage(): Store | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null
  } catch {
    return null
  }
}

export function newTabId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  } catch { /* fall through */ }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function readLock(workflowId: string, store: Store | null = storage()): EditLock | null {
  if (!store || !workflowId) return null
  try {
    const value = JSON.parse(store.getItem(lockKey(workflowId)) ?? 'null') as Partial<EditLock> | null
    if (!value || typeof value.tabId !== 'string' || !value.tabId || typeof value.at !== 'number' || !Number.isFinite(value.at)) return null
    return { tabId: value.tabId, at: value.at }
  } catch {
    return null
  }
}

/** Whose the flow is right now. A record from the future (a clock that jumped) is treated as just renewed, not as
 *  valid forever. */
export function lockState(lock: EditLock | null, tabId: string, now: number): LockState {
  if (!lock) return 'free'
  if (lock.tabId === tabId) return 'mine'
  return now - Math.min(lock.at, now) < LOCK_TTL_MS ? 'other' : 'free'
}

/** Take or renew the lock. `force` is a person pressing "take over"; without it a live lock of another tab stands.
 *  Returns whether this tab holds it afterwards. */
export function claimLock(workflowId: string, tabId: string, now: number, options: { force?: boolean } = {}, store: Store | null = storage()): boolean {
  if (!store || !workflowId) return true
  if (!options.force && lockState(readLock(workflowId, store), tabId, now) === 'other') return false
  try {
    store.setItem(lockKey(workflowId), JSON.stringify({ tabId, at: now } satisfies EditLock))
    return true
  } catch {
    // Storage that cannot be written cannot coordinate tabs; editing goes on as it did before there was a lock.
    return true
  }
}

/** Give the lock up -- only if it is this tab's: releasing must never delete the record of the tab that took over. */
export function releaseLock(workflowId: string, tabId: string, store: Store | null = storage()): void {
  if (!store || !workflowId) return
  try {
    if (readLock(workflowId, store)?.tabId === tabId) store.removeItem(lockKey(workflowId))
  } catch { /* nothing to release */ }
}
