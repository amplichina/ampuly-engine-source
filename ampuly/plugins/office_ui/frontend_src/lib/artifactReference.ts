/** Existing master_send reference, not an attachment upload or model request. */
export interface MasterArtifactReference {
  kind: 'artifact'
  project_id: string
  path: string
  preview_revision: string
  selection_start: number
  selection_end: number
}

export interface ArtifactQuoteSelection {
  reference: MasterArtifactReference
  /** UI only. The server derives its own quote from the verified file. */
  text: string
}

export interface ArtifactQuoteDraft extends ArtifactQuoteSelection {
  key: string
  principal: string
  threadId: string
}

export function artifactQuoteSelection(preview: {
  project_id?: string; path: string; kind: string; content?: string; preview_revision?: string
}, start: number, end: number): ArtifactQuoteSelection | null {
  const text = preview.content
  if (preview.kind !== 'text' || !preview.project_id || !preview.preview_revision
    || !/^[0-9a-f]{64}$/.test(preview.preview_revision) || typeof text !== 'string'
    || !Number.isSafeInteger(start) || !Number.isSafeInteger(end)
    || start < 0 || end <= start || end > text.length || end - start > 4000) return null
  // Never split a Unicode surrogate pair; textarea offsets are UTF-16 units.
  const split = (at: number) => at > 0 && at < text.length
    && /[\uD800-\uDBFF]/.test(text[at - 1]) && /[\uDC00-\uDFFF]/.test(text[at])
  const selected = text.slice(start, end)
  if (split(start) || split(end) || !selected.trim()) return null
  return { reference: {kind: 'artifact', project_id: preview.project_id, path: preview.path,
    preview_revision: preview.preview_revision, selection_start: start, selection_end: end}, text: selected }
}

/** A hidden pane or old callback must not move a quote into a different principal/project/thread. */
export function quoteForConversation(quote: ArtifactQuoteDraft | null, scope: {
  principal: string; projectId: string; threadId: string
}): ArtifactQuoteDraft | null {
  return quote && !!scope.principal && quote.principal === scope.principal
    && quote.reference.project_id === scope.projectId && quote.threadId === scope.threadId ? quote : null
}
