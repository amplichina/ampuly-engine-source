import { useEffect, useRef } from 'react'

import { clearDraft, storeDraft, type StoredDraft } from '../lib/draftRecovery'
import type { NodeDraftState } from './useNodeDraftState'

/**
 * Keeps each open builder draft in this browser, so a reload or a closed tab does not lose it, and asks before the
 * page is left with a draft open. It never saves anything to the server and never applies a stored draft by itself:
 * recovery is offered by the page and taken by a person.
 *
 * A stored draft is removed only when a draft that was open in this session goes away (saved, or 放弃草稿). A page
 * that has just loaded has no drafts in memory yet -- that is exactly when a stored one must survive.
 */
type DraftFields = Pick<NodeDraftState, 'nodeDrafts' | 'nodeBaseVersions' | 'nodeVersions' | 'nodeChangeNotes'>

/** What one pass writes and removes. A draft that was open before and is not now was saved or discarded: its copy
 *  goes. A page that has just loaded had nothing open before, so it removes nothing -- that is exactly when a stored
 *  draft has to survive to be offered. */
export function persistencePlan(openBefore: ReadonlySet<string>, state: DraftFields, now: string, anotherTabs: (workflowId: string) => boolean = () => false): {
  open: Set<string>; clear: string[]; store: Array<{ workflowId: string; draft: StoredDraft }>
} {
  const open = new Set(Object.keys(state.nodeDrafts))
  return {
    open,
    // A workflow another tab is editing is that tab's to write and to clear: a tab that stepped aside forgets its
    // draft, and that must not take the stored copy with it -- the other tab is about to be offered it.
    clear: [...openBefore].filter(workflowId => !open.has(workflowId) && !anotherTabs(workflowId)),
    store: [...open].filter(workflowId => !anotherTabs(workflowId)).map(workflowId => ({
      workflowId,
      draft: {
        baseVersion: state.nodeBaseVersions[workflowId] ?? '',
        nodes: state.nodeDrafts[workflowId],
        newVersion: state.nodeVersions[workflowId] ?? '',
        changeNote: state.nodeChangeNotes[workflowId] ?? '',
        storedAt: now,
      },
    })),
  }
}

export function useDraftPersistence(state: Pick<NodeDraftState, 'nodeDrafts' | 'nodeBaseVersions' | 'nodeVersions' | 'nodeChangeNotes'>, anotherTabs: (workflowId: string) => boolean = () => false): void {
  const { nodeDrafts, nodeBaseVersions, nodeVersions, nodeChangeNotes } = state
  const openBefore = useRef<Set<string>>(new Set())

  useEffect(() => {
    const plan = persistencePlan(openBefore.current, { nodeDrafts, nodeBaseVersions, nodeVersions, nodeChangeNotes }, new Date().toISOString(), anotherTabs)
    for (const workflowId of plan.clear) clearDraft(workflowId)
    for (const { workflowId, draft } of plan.store) {
      // A copy that could not be refreshed (too large, storage full) is removed rather than left behind: an older
      // copy would later be offered as if it were this draft.
      if (!storeDraft(workflowId, draft)) clearDraft(workflowId)
    }
    openBefore.current = plan.open
  }, [nodeDrafts, nodeBaseVersions, nodeVersions, nodeChangeNotes])

  const anyOpen = Object.keys(nodeDrafts).length > 0
  useEffect(() => {
    if (!anyOpen) return undefined
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [anyOpen])
}
