// What the order drawer says (frontend refactor U3). Pure, over what the page already holds for an order: the order
// itself and, once asked for, its timeline. Nothing here is a new fact; it is the same facts read as "where is this
// order, whose turn is it, what happened, what was said".

export const DRAWER_TABS = ['progress', 'deliverables', 'feedback', 'runs'] as const
export type DrawerTab = typeof DRAWER_TABS[number]
export const DRAWER_TAB_LABELS: Record<DrawerTab, readonly [string, string]> = {
  progress: ['进度', 'Progress'],
  deliverables: ['交付物', 'Deliverables'],
  feedback: ['沟通与反馈', 'Feedback'],
  runs: ['执行记录', 'Runs'],
}

type OrderLike = {
  order_id: string
  status: string
  review_note?: string
  launched_task_id?: string
  running_child_orders?: number
  acceptance?: {
    status?: string
    round_no?: number
    decision_note?: string
    rework_feedback?: string
    rework_target?: string
    evidence?: Array<Record<string, unknown>>
    artifact_snapshot?: { verified?: boolean; files?: Array<{ source_path: string; archive_path?: string; size: number }>; error?: string }
  } | null
}

export type StepAction = 'review' | 'acceptance' | 'task' | 'none'
export type OrderStep = { key: string; action: StepAction; person: boolean }

/** Where the order is and what, if anything, a person should do next. Three different decisions stay three: the
 *  intake review (it launches the order and may spend), the acceptance (contractual), and everything that happens
 *  inside the run (the approval centre's). */
export function orderStep(order: OrderLike): OrderStep {
  const acceptance = order.acceptance?.status ?? ''
  if (order.status === 'closed') return { key: 'closed', action: 'none', person: false }
  if (order.status === 'rejected') return { key: 'rejected', action: 'none', person: false }
  if (order.status === 'pending_review') return { key: 'intake_review', action: 'review', person: true }
  if (acceptance === 'submitted') return { key: 'acceptance_decision', action: 'acceptance', person: true }
  if (acceptance === 'rejected') return { key: 'rework', action: 'acceptance', person: false }
  // Accepted and not yet closed: the run is over as far as the customer is concerned; saying "running" would be wrong.
  if (acceptance === 'accepted') return { key: 'accepted', action: 'acceptance', person: false }
  if (order.status === 'collecting') return { key: 'collecting', action: 'none', person: false }
  return { key: 'running', action: order.launched_task_id ? 'task' : 'none', person: false }
}

export type FeedbackNote = { kind: 'intake' | 'decision' | 'rework'; text: string; round?: number }

/** What people said about this order, as far as the order carries it. Notes only: who and when are the timeline's. */
export function feedbackNotes(order: OrderLike): FeedbackNote[] {
  const notes: FeedbackNote[] = []
  const round = order.acceptance?.round_no
  if (order.review_note?.trim()) notes.push({ kind: 'intake', text: order.review_note.trim() })
  if (order.acceptance?.decision_note?.trim()) notes.push({ kind: 'decision', text: order.acceptance.decision_note.trim(), round })
  if (order.acceptance?.rework_feedback?.trim()) notes.push({ kind: 'rework', text: order.acceptance.rework_feedback.trim(), round })
  return notes
}

type EntryLike = { source: string }

/** 进度 shows what happened to the order as a piece of business (the order's own events and the gates); 执行记录
 *  shows the run (runtime and effects). An entry of a source this page has never heard of is business: shown, not lost. */
export function entriesFor<T extends EntryLike>(tab: 'progress' | 'runs', entries: readonly T[]): T[] {
  // 'browser': a browser step's decisions (who suggested, who checked, who has it now) are part of the run.
  const run = (entry: T) => entry.source === 'runtime' || entry.source === 'effect' || entry.source === 'browser'
  return entries.filter(entry => (tab === 'runs' ? run(entry) : !run(entry)))
}

export type FileKind = 'image' | 'text' | 'other'
const IMAGE = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'])
const TEXT = new Set(['md', 'txt', 'json', 'yaml', 'yml', 'csv', 'html', 'svg', 'py', 'ts', 'tsx', 'js', 'jsx', 'css', 'sql', 'xml', 'log'])
/** What the console's preview can show of a file, by its name: the same split the server's preview makes. */
export function fileKind(path: string): FileKind {
  const name = path.slice(path.lastIndexOf('/') + 1)
  const dot = name.lastIndexOf('.')
  const suffix = dot > 0 ? name.slice(dot + 1).toLowerCase() : ''
  return IMAGE.has(suffix) ? 'image' : TEXT.has(suffix) ? 'text' : 'other'
}

export type EvidenceFile = { path: string; kind: FileKind; criterion: string }
/** The files the acceptance evidence points at, once each, in the order the evidence was recorded. */
export function evidenceFiles(order: OrderLike): EvidenceFile[] {
  const seen = new Set<string>()
  const files: EvidenceFile[] = []
  for (const item of order.acceptance?.evidence ?? []) {
    const path = [item.artifact_path, item.path].find(value => typeof value === 'string' && value.trim()) as string | undefined
    if (!path || seen.has(path.trim())) continue
    seen.add(path.trim())
    files.push({ path: path.trim(), kind: fileKind(path.trim()), criterion: typeof item.criterion === 'string' ? item.criterion : '' })
  }
  return files
}

/** The files the last acceptance round recorded, smallest claim first: nothing recorded is "none", not "0 files". */
export type RecordedFile = { path: string; size: number; kind: FileKind; openPath: string }
export function recordedFiles(order: OrderLike): { state: 'none' | 'unverified' | 'verified'; files: RecordedFile[] } {
  const snapshot = order.acceptance?.artifact_snapshot
  // What opens is the copy the round fixed (the version that was submitted), as the order's table does -- never the
  // working file, which may have changed since. A record without a fixed copy is listed and does not open.
  const files = (snapshot?.files ?? []).map(file => ({ path: file.source_path, size: file.size, kind: fileKind(file.source_path), openPath: file.archive_path ?? '' }))
  if (!snapshot || files.length === 0) return { state: 'none', files: [] }
  return { state: snapshot.verified === true ? 'verified' : 'unverified', files }
}
