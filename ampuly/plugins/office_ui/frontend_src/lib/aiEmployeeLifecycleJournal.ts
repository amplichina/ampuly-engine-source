/** Opaque pending IDs only. No prompts, inputs, outputs, budget or credentials. */
export interface LifecyclePendingId {
  project_id: string; package_id: string; revision: string; fingerprint: string
  request_id: string; action: 'employee_package_test_start' | 'employee_package_review' | 'employee_package_publish' | 'employee_package_test_abandon'
  trial_id?: string
  instance_selection?: { instance_id: string; expected_revision: number; expected_fingerprint: string }
}
export interface LifecyclePendingJournal {
  list(): readonly Readonly<LifecyclePendingId>[]
  record(value: LifecyclePendingId): void
  settle(requestId: string): void
}
export type JournalStorage = Pick<Storage, 'getItem' | 'setItem'>
const KEY = 'ampuly.employee-lifecycle.pending-ids.v1'
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/, PACKAGE = /^[a-z0-9][a-z0-9_-]{2,63}$/, SHA = /^[a-f0-9]{64}$/, TRIAL = /^et-[a-f0-9]{24}$/
const writes = new Set(['employee_package_test_start', 'employee_package_review', 'employee_package_publish', 'employee_package_test_abandon'])
export class LifecycleJournalError extends Error {
  constructor() { super('无法可靠保存或核对原请求编号；未发送新的测试、审核、作废或发布。请保留此页，不要清理未知请求。'); this.name = 'LifecycleJournalError' }
}
function need(value: unknown): asserts value { if (!value) throw new LifecycleJournalError() }
export function pendingId(value: unknown): Readonly<LifecyclePendingId> {
  need(value !== null && typeof value === 'object' && !Array.isArray(value))
  const r = value as Record<string, unknown>, fields = ['project_id', 'package_id', 'revision', 'fingerprint', 'request_id', 'action']
  if (r.action === 'employee_package_review' || r.action === 'employee_package_test_abandon') fields.push('trial_id')
  if (r.instance_selection !== undefined) {
    fields.push('instance_selection')
    need(r.action !== 'employee_package_publish' && r.instance_selection !== null && typeof r.instance_selection === 'object' && !Array.isArray(r.instance_selection))
    const selection = r.instance_selection as Record<string, unknown>
    need(Object.keys(selection).length === 3 && typeof selection.instance_id === 'string' && /^ei-[a-f0-9]{16}$/.test(selection.instance_id)
      && Number.isSafeInteger(selection.expected_revision) && Number(selection.expected_revision) > 0
      && typeof selection.expected_fingerprint === 'string' && SHA.test(selection.expected_fingerprint))
  }
  need(Object.keys(r).length === fields.length && fields.every(k => Object.prototype.hasOwnProperty.call(r, k)))
  need(typeof r.project_id === 'string' && ID.test(r.project_id) && typeof r.package_id === 'string' && PACKAGE.test(r.package_id)
    && typeof r.revision === 'string' && SHA.test(r.revision) && typeof r.fingerprint === 'string' && SHA.test(r.fingerprint)
    && typeof r.request_id === 'string' && ID.test(r.request_id) && typeof r.action === 'string' && writes.has(r.action))
  if (r.action === 'employee_package_review' || r.action === 'employee_package_test_abandon') need(typeof r.trial_id === 'string' && TRIAL.test(r.trial_id))
  return Object.freeze({ ...r, ...(r.instance_selection ? { instance_selection: Object.freeze({ ...r.instance_selection as Record<string, unknown> }) } : {}) }) as unknown as Readonly<LifecyclePendingId>
}
/** Re-read before every mutation, then read back exact bytes; fail closed on corruption/quota/access denial. */
export function createLifecyclePendingJournal(storage: JournalStorage): LifecyclePendingJournal {
  const read = (): Readonly<LifecyclePendingId>[] => {
    try {
      const raw = storage.getItem(KEY)
      if (raw === null) return []
      need(raw.length <= 16384)
      const value: unknown = JSON.parse(raw)
      need(value !== null && typeof value === 'object' && !Array.isArray(value))
      const data = value as Record<string, unknown>
      need(Object.keys(data).length === 2 && data.version === 1 && Array.isArray(data.pending) && data.pending.length <= 8)
      const entries = data.pending.map(pendingId)
      need(new Set(entries.map(r => r.request_id)).size === entries.length)
      return entries
    } catch { throw new LifecycleJournalError() }
  }
  const write = (entries: readonly Readonly<LifecyclePendingId>[]): void => {
    try {
      need(entries.length <= 8)
      const raw = JSON.stringify({ version: 1, pending: entries }); need(raw.length <= 16384)
      storage.setItem(KEY, raw); need(storage.getItem(KEY) === raw)
    } catch { throw new LifecycleJournalError() }
  }
  return {
    list: () => Object.freeze(read()),
    record(value) {
      const record = pendingId(value), entries = read(), old = entries.find(r => r.request_id === record.request_id)
      if (old) { need(JSON.stringify(old) === JSON.stringify(record)); return }
      write([...entries, record])
    },
    settle(requestId) {
      need(typeof requestId === 'string' && ID.test(requestId))
      const entries = read()
      if (entries.some(r => r.request_id === requestId)) write(entries.filter(r => r.request_id !== requestId))
    },
  }
}
/** Actual parent injects this once; SSR has no browser and therefore sends nothing. */
export function browserLifecyclePendingJournal(): LifecyclePendingJournal | undefined {
  if (typeof window === 'undefined') return undefined
  // Per-tab storage avoids two tabs replacing one another's pending IDs.
  // Closing this tab is outside the refresh guarantee; keep unknown tabs open.
  try { return createLifecyclePendingJournal(window.sessionStorage) }
  catch { return { list() { throw new LifecycleJournalError() }, record() { throw new LifecycleJournalError() }, settle() { throw new LifecycleJournalError() } } }
}
