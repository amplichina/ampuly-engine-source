// 客户记忆 (4.7-A09): what a project remembers about its customers, for the owner to read and to delete one entry
// at a time. Pure: the shape of the two replies, the card's state, and a refusal in words.
//
// A deleted entry is never simply gone: the server first writes its words into the project's 回收区, and the recycle
// card's own 恢复 brings them back (as a file in the project folder).

export interface MemoryEntry {
  id: string
  /** When it was written, as the memory file has it ("2026-09-25 10:00"). */
  at: string
  text: string
  chars: number
}

export interface MemoryGroup {
  /** The key the customer's memory is filed under; sent back as is when an entry is deleted. */
  customer: string
  /** What to call the customer: the roster's name, or the key when the roster does not know it. */
  name: string
  entries: MemoryEntry[]
}

export interface MemoryOverview {
  /** Entries that belong to no customer in particular. */
  project: { entries: MemoryEntry[] }
  customers: MemoryGroup[]
  total: number
}

export interface CustomerMemoryState {
  projectId: string
  overview: MemoryOverview | null
  loading: boolean
  /** The entry whose 删除 was pressed and waits for the second answer. */
  confirming: { customer: string; id: string } | null
  /** The entry being deleted right now. */
  busy: string
  error: string
  note: '' | 'deleted'
}

export const EMPTY_CUSTOMER_MEMORY: CustomerMemoryState = {
  projectId: '', overview: null, loading: false, confirming: null, busy: '', error: '', note: '',
}

export type CustomerMemoryAction =
  | { kind: 'project'; projectId: string }
  | { kind: 'load' }
  | { kind: 'askDelete'; customer: string; id: string }
  | { kind: 'cancelDelete' }
  | { kind: 'deleting'; id: string }
  | { kind: 'ack'; ack: Record<string, unknown> }

/** The key of the no-customer group, as the server files it (and expects it back). */
export const NO_CUSTOMER = ''

/** How much of an entry the card shows. */
export const MEMORY_PREVIEW_CHARS = 80

function entries(value: unknown): MemoryEntry[] {
  if (!Array.isArray(value)) return []
  return value.filter(item => item && typeof item === 'object').map(item => {
    const row = item as Record<string, unknown>
    return { id: String(row.id ?? ''), at: String(row.at ?? ''), text: String(row.text ?? ''), chars: Number(row.chars ?? 0) }
  }).filter(entry => entry.id)
}

export function overviewFrom(ack: Record<string, unknown>): MemoryOverview {
  const project = (ack.project ?? {}) as { entries?: unknown }
  const customers = Array.isArray(ack.customers) ? ack.customers : []
  return {
    project: { entries: entries(project.entries) },
    customers: customers.filter(item => item && typeof item === 'object').map(item => {
      const row = item as Record<string, unknown>
      const key = String(row.customer ?? '')
      return { customer: key, name: String(row.name ?? '') || key, entries: entries(row.entries) }
    }),
    total: Number(ack.total ?? 0),
  }
}

export function customerMemoryReducer(state: CustomerMemoryState, action: CustomerMemoryAction): CustomerMemoryState {
  if (action.kind === 'project') {
    return action.projectId === state.projectId ? state : { ...EMPTY_CUSTOMER_MEMORY, projectId: action.projectId }
  }
  if (action.kind === 'load') return { ...state, loading: true, error: '' }
  if (action.kind === 'askDelete') return { ...state, confirming: { customer: action.customer, id: action.id }, error: '', note: '' }
  if (action.kind === 'cancelDelete') return { ...state, confirming: null }
  if (action.kind === 'deleting') return { ...state, busy: action.id, confirming: null, error: '', note: '' }

  const ack = action.ack || {}
  const name = String(ack.action ?? '')
  if (name !== 'memory_overview' && name !== 'memory_delete') return state
  const project = String(ack.project_id ?? '')
  if (state.projectId && project && project !== state.projectId) return state
  if (ack.ok === false) {
    return { ...state, loading: false, busy: '', confirming: null, error: String(ack.error_code ?? '') || String(ack.error ?? 'error') }
  }
  const overview = overviewFrom(ack)
  if (name === 'memory_delete') return { ...state, loading: false, busy: '', confirming: null, error: '', overview, note: 'deleted' }
  return { ...state, loading: false, error: '', overview }
}

/** The first words of an entry, on one line. */
export function memoryPreview(text: string, limit = MEMORY_PREVIEW_CHARS): string {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim()
  const chars = Array.from(flat)
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : flat
}

/** What the server refused, in words. */
export const MEMORY_ERROR_LABELS: Record<string, [string, string]> = {
  owner_only: ['客户记忆只有老板本人能查看和删除', 'Only the owner can see or delete what a project remembers'],
  entry_not_found: ['这条记忆已经变了或者已经删掉了，请刷新后再看', 'That entry changed or is gone; refresh and look again'],
  entry_required: ['没说要删哪一条', 'Which entry was not said'],
  customer_required: ['没说是哪个客户的记忆', 'Which customer was not said'],
  customer_invalid: ['这个客户的名字不对，删不了', 'That customer cannot be named here'],
  project_required: ['没有选中项目', 'No project is open'],
  recycle_unavailable: ['回收区写不进去，这条记忆没有删', 'The recycle area could not be written; nothing was deleted'],
  project_not_found: ['找不到这个项目', 'This project was not found'],
  project_destroyed: ['这个项目已经删除了', 'This project was deleted'],
  unauthorized: ['客户记忆只有老板本人能查看和删除', 'Only the owner can see or delete what a project remembers'],
}

export function memoryError(code: string, zh: boolean): string {
  const text = String(code ?? '')
  const label = MEMORY_ERROR_LABELS[text]
  if (label) return label[zh ? 0 : 1]
  // A sentence the server already said in words is shown as it is; a code this build does not know is not.
  if (/[^\x00-\x7f]/.test(text)) return text
  return zh ? '没有办成，请稍后再试' : 'That did not work; try again later'
}
