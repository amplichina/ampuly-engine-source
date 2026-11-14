import { useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'

import type { WorkflowNode } from '../components/WorkflowsPage'
import { clearDraft } from '../lib/draftRecovery'

/**
 * The builder's unsaved node drafts, held above the 搭建台 page.
 *
 * The page unmounts when the owner switches to 组件库 to pick a component (调用) and comes back; state kept
 * inside the page was lost on the way, together with a master-control proposal already applied to a draft.
 * App holds this, so a draft lives until it is saved or 放弃草稿 drops it.
 */
export interface NodeDraftState {
  editingWorkflowId: string | null
  setEditingWorkflowId: Dispatch<SetStateAction<string | null>>
  nodeDrafts: Record<string, WorkflowNode[]>
  setNodeDrafts: Dispatch<SetStateAction<Record<string, WorkflowNode[]>>>
  /** Which workflow the builder canvas shows (null = the first one worth building). */
  builderWorkflowId: string | null
  setBuilderWorkflowId: Dispatch<SetStateAction<string | null>>
  nodeVersions: Record<string, string>
  setNodeVersions: Dispatch<SetStateAction<Record<string, string>>>
  /** The version each open draft started from: what the save names as expected_version. */
  nodeBaseVersions: Record<string, string>
  setNodeBaseVersions: Dispatch<SetStateAction<Record<string, string>>>
  /** The draft_revision each open draft started from: what the save names as expected_revision. A layout-only save
   *  keeps the version, so only this tells a draft that someone saved over it at the same version. '' = unknown
   *  (a draft recovered from this browser, or a proposal applied): the save then relies on the version alone. */
  nodeBaseRevisions: Record<string, string>
  setNodeBaseRevisions: Dispatch<SetStateAction<Record<string, string>>>
  nodeChangeNotes: Record<string, string>
  setNodeChangeNotes: Dispatch<SetStateAction<Record<string, string>>>
  /**
   * The last node-save seq already acted on. A remounted page sees the same `nodesSaved` again; without this it
   * would drop a draft opened after that save, for the same workflow.
   */
  handledSavedSeq: MutableRefObject<number>
  /** The last 调用 already opened on the builder; coming back to the page later does not jump there again. */
  handledBroughtSeq: MutableRefObject<number>
}

export function useNodeDraftState(): NodeDraftState {
  const [editingWorkflowId, setEditingWorkflowId] = useState<string | null>(null)
  const [nodeDrafts, setNodeDrafts] = useState<Record<string, WorkflowNode[]>>({})
  const [builderWorkflowId, setBuilderWorkflowId] = useState<string | null>(null)
  const [nodeVersions, setNodeVersions] = useState<Record<string, string>>({})
  const [nodeBaseVersions, setNodeBaseVersions] = useState<Record<string, string>>({})
  const [nodeBaseRevisions, setNodeBaseRevisions] = useState<Record<string, string>>({})
  const [nodeChangeNotes, setNodeChangeNotes] = useState<Record<string, string>>({})
  const handledSavedSeq = useRef(0)
  const handledBroughtSeq = useRef(0)
  return {
    editingWorkflowId, setEditingWorkflowId,
    nodeDrafts, setNodeDrafts,
    builderWorkflowId, setBuilderWorkflowId,
    nodeVersions, setNodeVersions,
    nodeBaseVersions, setNodeBaseVersions,
    nodeBaseRevisions, setNodeBaseRevisions,
    nodeChangeNotes, setNodeChangeNotes,
    handledSavedSeq,
    handledBroughtSeq,
  }
}

/**
 * Drop one workflow's draft: from memory and from this browser's copy, in the same step. Discard and a save that
 * passed both come here -- from the page, and from the app when the save's answer arrives while the page is not
 * mounted (otherwise the draft stays open, "leave site?" keeps asking after a save, and the saved draft comes back
 * later as an older draft).
 */
export function dropDraft(state: DraftSetters, workflowId: string): void {
  clearDraft(workflowId)
  forgetDraft(state, workflowId)
}

type DraftSetters = Pick<NodeDraftState, 'setEditingWorkflowId' | 'setNodeDrafts' | 'setNodeVersions' | 'setNodeBaseVersions' | 'setNodeChangeNotes'>
  & Partial<Pick<NodeDraftState, 'setNodeBaseRevisions'>>

/** Forget a draft in this tab only. The browser's stored copy stays: another tab took the workflow over and is
 *  offered that copy, so nothing a person typed is lost by this tab stepping aside. */
export function forgetDraft(state: DraftSetters, workflowId: string): void {
  state.setEditingWorkflowId(current => (current === workflowId ? null : current))
  const without = <T,>(prev: Record<string, T>): Record<string, T> => {
    if (!(workflowId in prev)) return prev
    const next = { ...prev }
    delete next[workflowId]
    return next
  }
  state.setNodeDrafts(without)
  state.setNodeVersions(without)
  state.setNodeBaseVersions(without)
  state.setNodeBaseRevisions?.(without)
  state.setNodeChangeNotes(without)
}

/**
 * Whether a notice naming a workflow (a node save, a 调用) still has to be acted on; marks it handled when it
 * does. The page may mount again and see the same notice: it is acted on once.
 */
export function takeSavedNotice(handled: MutableRefObject<number>, saved: { workflowId: string; seq: number } | null | undefined): string | null {
  if (!saved?.workflowId || saved.seq <= handled.current) return null
  handled.current = saved.seq
  return saved.workflowId
}
