import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { LOCK_RENEW_MS, claimLock, lockState, newTabId, readLock, releaseLock, workflowOfLockKey, type LockState } from '../lib/editLock'
import { loadDraft } from '../lib/draftRecovery'
import { forgetDraft, type NodeDraftState } from './useNodeDraftState'

export interface EditLockApi {
  /** 'other' = another tab of this browser is editing that workflow right now: show it read-only. */
  stateOf: (workflowId: string) => LockState
  /** A person pressed "take over": this tab holds the lock from now on, and the other tab goes read-only. */
  takeOver: (workflowId: string) => void
}

/** A flow this tab took over but has no draft of yet: keep it only while there is something to decide (a stored
 *  draft waiting to be recovered or discarded). With nothing to decide, holding it would lock out every other tab
 *  for a flow nobody is editing. */
export function takenOverDuty(state: LockState, draftOpen: boolean, storedDraftWaiting: boolean): 'editing' | 'hold' | 'release' | 'lost' {
  if (state === 'other') return 'lost'
  if (draftOpen) return 'editing'
  return storedDraftWaiting ? 'hold' : 'release'
}

/** What this tab does about one workflow it has a draft of, given whose the lock is. */
export function lockDuty(state: LockState): 'hold' | 'let_go' {
  // Free or ours: hold it and keep renewing. Another tab's: that tab took over (or won a simultaneous start) -- this
  // tab forgets its in-memory draft, which the other tab can recover from the stored copy, and becomes a viewer.
  return state === 'other' ? 'let_go' : 'hold'
}

/**
 * One tab edits a workflow at a time. A tab holds the lock of every workflow it has an open draft of, renews it
 * while the draft is open, and releases it when the draft is saved or discarded or the page goes away.
 *
 * Losing the lock never deletes anything: the stored copy of the draft stays (the persistence hook neither writes
 * nor clears a workflow that is another tab's), so the tab that took over is offered it for recovery.
 */
export function useEditLock(state: NodeDraftState): EditLockApi {
  const tabId = useRef('')
  if (!tabId.current) tabId.current = newTabId()
  const [tick, setTick] = useState(0)
  const bump = useCallback(() => setTick(value => value + 1), [])
  const open = Object.keys(state.nodeDrafts)
  const openKey = open.slice().sort().join('\n')
  const held = useRef<Set<string>>(new Set())
  const takenOver = useRef<Set<string>>(new Set())
  const stateRef = useRef(state)
  stateRef.current = state

  // Another tab wrote or removed a lock, or time passed (a lock expired): look again.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => { if (event.key === null || workflowOfLockKey(event.key)) bump() }
    window.addEventListener('storage', onStorage)
    const timer = window.setInterval(bump, LOCK_RENEW_MS)
    return () => { window.removeEventListener('storage', onStorage); window.clearInterval(timer) }
  }, [bump])

  // Hold what we edit, let go of what another tab took, release what we no longer edit.
  useEffect(() => {
    const now = Date.now()
    const openNow = new Set(openKey ? openKey.split('\n') : [])
    for (const workflowId of openNow) {
      if (lockDuty(lockState(readLock(workflowId), tabId.current, now)) === 'hold') {
        claimLock(workflowId, tabId.current, now)
        held.current.add(workflowId)
      } else {
        held.current.delete(workflowId)
        forgetDraft(stateRef.current, workflowId)
      }
    }
    for (const workflowId of [...held.current]) {
      if (!openNow.has(workflowId)) { releaseLock(workflowId, tabId.current); held.current.delete(workflowId) }
    }
    for (const workflowId of [...takenOver.current]) {
      const duty = takenOverDuty(lockState(readLock(workflowId), tabId.current, now), openNow.has(workflowId), loadDraft(workflowId) !== null)
      if (duty === 'hold') { claimLock(workflowId, tabId.current, now); continue }
      if (duty === 'release') releaseLock(workflowId, tabId.current)
      takenOver.current.delete(workflowId) // editing: the loop above holds it now; lost: another tab has it
    }
  }, [openKey, tick])

  // A page that goes away gives its locks back at once instead of making the next tab wait for them to expire.
  useEffect(() => {
    const release = () => { for (const workflowId of [...held.current, ...takenOver.current]) releaseLock(workflowId, tabId.current) }
    window.addEventListener('pagehide', release)
    return () => { window.removeEventListener('pagehide', release); release() }
  }, [])

  return useMemo<EditLockApi>(() => ({
    stateOf: workflowId => lockState(readLock(workflowId), tabId.current, Date.now()),
    takeOver: workflowId => { claimLock(workflowId, tabId.current, Date.now(), { force: true }); takenOver.current.add(workflowId); bump() },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [tick, bump])
}
