// An unsaved builder draft survives a reload or a closed tab -- in this browser only. This is recovery, not saving:
// nothing here reaches the server, and a saved version is still only ever made by a person pressing save. Browser
// storage can be missing, full or blocked; every access is guarded and the builder works the same without it.
import type { WorkflowNode } from '../components/WorkflowsPage'

export interface StoredDraft {
  /** The saved version this draft was started from. A draft of another version is not offered for recovery. */
  baseVersion: string
  nodes: WorkflowNode[]
  newVersion: string
  changeNote: string
  /** ISO time of the last local write. */
  storedAt: string
}

const PREFIX = 'superones.builderDraft.v1'
export const MAX_DRAFT_CHARS = 512 * 1024

/** Workflow packages belong to the console, not to a project, so a draft is kept per workflow. Another console
 *  (another port) is another origin and has its own storage. */
export function draftKey(workflowId: string): string {
  return `${PREFIX}:${workflowId}`
}

type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function storage(): Store | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null
  } catch {
    return null
  }
}

/** Returns false when nothing was written (no storage, too large, quota): the caller carries on either way. */
export function storeDraft(workflowId: string, draft: StoredDraft, store: Store | null = storage()): boolean {
  if (!store || !workflowId) return false
  try {
    const text = JSON.stringify(draft)
    if (text.length > MAX_DRAFT_CHARS) return false
    store.setItem(draftKey(workflowId), text)
    return true
  } catch {
    return false
  }
}

export function loadDraft(workflowId: string, store: Store | null = storage()): StoredDraft | null {
  if (!store || !workflowId) return null
  try {
    const raw = store.getItem(draftKey(workflowId))
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<StoredDraft> | null
    if (!value || typeof value !== 'object' || !Array.isArray(value.nodes) || typeof value.baseVersion !== 'string') return null
    if (value.nodes.some(node => !node || typeof node !== 'object' || typeof (node as WorkflowNode).id !== 'string')) return null
    return {
      baseVersion: value.baseVersion, nodes: value.nodes as WorkflowNode[],
      newVersion: typeof value.newVersion === 'string' ? value.newVersion : '',
      changeNote: typeof value.changeNote === 'string' ? value.changeNote : '',
      storedAt: typeof value.storedAt === 'string' ? value.storedAt : '',
    }
  } catch {
    return null
  }
}

export function clearDraft(workflowId: string, store: Store | null = storage()): void {
  if (!store || !workflowId) return
  try { store.removeItem(draftKey(workflowId)) } catch { /* nothing to clear */ }
}

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value as Record<string, unknown>).sort().map(key => [key, stable((value as Record<string, unknown>)[key])]))
  }
  return value
}

/** One string per node table, independent of key order and of optional fields left at their empty default. */
export function comparable(nodes: readonly WorkflowNode[]): string {
  return JSON.stringify(nodes.map(node => stable({ ...node, inputs: node.inputs ?? [], skills: node.skills ?? [], tools: node.tools ?? [], model: node.model ?? '', tier: node.tier ?? '', rework_to: node.rework_to ?? '', connector: node.connector ?? null, canvas: node.canvas ?? null })))
}

export type RecoveryOffer =
  | { kind: 'none' }
  | { kind: 'recover'; draft: StoredDraft }
  /** Started from a version that is no longer the saved one: applying it would silently undo what was saved since. */
  | { kind: 'stale'; draft: StoredDraft }

/** What recovering puts into the draft state, or null when the offer is not one to recover. The draft keeps the
 *  version it was started from -- that is what its save names as expected_version, so a draft can never land on a
 *  version it was not made against. */
export function recoveredDraftState(offer: RecoveryOffer, savedVersion: string): { nodes: WorkflowNode[]; baseVersion: string; newVersion: string; changeNote: string } | null {
  if (offer.kind !== 'recover') return null
  return { nodes: offer.draft.nodes, baseVersion: offer.draft.baseVersion, newVersion: offer.draft.newVersion || savedVersion, changeNote: offer.draft.changeNote }
}

/** What to offer when the builder opens a workflow with no draft in memory. Never applied without being asked. */
export function recoveryOffer(stored: StoredDraft | null, savedVersion: string, savedNodes: readonly WorkflowNode[]): RecoveryOffer {
  if (!stored) return { kind: 'none' }
  if (stored.baseVersion !== savedVersion) return { kind: 'stale', draft: stored }
  // Identical to what is saved: there is nothing to recover. Compared by content, not by shape: a draft stored
  // before a field existed (no `inputs` key) is the same draft as one that carries the empty default.
  if (comparable(stored.nodes) === comparable(savedNodes)) return { kind: 'none' }
  return { kind: 'recover', draft: stored }
}
