import { decodeLifecycleHistory, historyBlocksNewTest } from './aiEmployeeLifecycleHistory'
import type { LifecycleHistory } from './aiEmployeeLifecycleHistory'
import type { LifecyclePendingId, LifecyclePendingJournal } from './aiEmployeeLifecycleJournal'
/** Owner gestures over the existing socket. No executor, retry, polling or fabricated result. */
export const LIFECYCLE_ACTIONS = {
  prepare: 'employee_package_test_prepare', start: 'employee_package_test_start',
  detail: 'employee_package_test_detail', lookup: 'employee_package_test_lookup',
  review: 'employee_package_review', publish: 'employee_package_publish', publishLookup: 'employee_package_publish_lookup',
  history: 'employee_package_lifecycle_history',
  abandon: 'employee_package_test_abandon',
} as const
type Action = typeof LIFECYCLE_ACTIONS[keyof typeof LIFECYCLE_ACTIONS]
type Row = Record<string, unknown>
export interface LifecycleInstanceSelection { instance_id: string; expected_revision: number; expected_fingerprint: string }
export interface LifecycleCandidate { project_id: string; package_id: string; revision: string; fingerprint: string; instance_selection?: LifecycleInstanceSelection }
export interface LifecycleBudget { max_cost_cny: string; max_model_calls: number; max_wall_clock_s: number }
export interface LifecycleJudgment { case_id: string; non_inferior_met: boolean; absolute_threshold_met: boolean }
export interface LifecycleContext {
  model_config: { provider: string; model: string; tier: string }
  capability_impls: Record<string, never>; runtime_profile_fingerprint: string
  checker_versions: Record<string, string>; skill_tree_fingerprints: Record<string, string>; params_fingerprint: string
  qualification_scope?: { kind: 'package' } | { kind: 'instance'; project_id: string; instance_id: string }
  knowledge_refs_fingerprint?: string
}
export interface LifecyclePrepared { sample_count: number; context: LifecycleContext; allowed_actions: readonly string[] }
export type TrialState = 'prepared' | 'running' | 'awaiting_review' | 'approved' | 'rejected' | 'failed' | 'cancelled' | 'outcome_unknown' | 'abandoned'
export interface LifecycleAbandonAudit {
  kind: 'employee_trial_abandon'; trial_id: string; package_id: string; fingerprint: string; prior_state: 'outcome_unknown'
  actor: string; request_id: string; reason: string; at: string; budget_id: string | null; observations_sha256: string
  spend_reset: false; reviewable: false; publishable: false
}
export interface LifecycleObservation { case_id: string; completed: boolean; hard_fail_count: number; output: string; model: string }
export interface LifecycleTrial extends LifecyclePrepared {
  trial_id: string; state: TrialState; test_request_id: string; candidate_revision: string; revision: string
  review_request_id: string | null; decision: 'approve' | 'reject' | null
  test_passed: boolean; cases: readonly Readonly<Row>[]; observations: readonly LifecycleObservation[]
  abandon?: Readonly<LifecycleAbandonAudit>
}
export interface LifecycleEntry {
  key: string; candidate: Readonly<LifecycleCandidate>; prepared: LifecyclePrepared | null
  trial: LifecycleTrial | null; published: boolean; stale: boolean; latest_request_id: string | null
}
export interface LifecycleReceipt {
  request_id: string; action: Action; project_id: string; package_id: string; entry_key: string
  write: boolean; state: 'pending' | 'success' | 'failed' | 'unknown'; message: string
}
export interface LifecycleSnapshot {
  connected: boolean; entries: readonly LifecycleEntry[]; receipts: readonly LifecycleReceipt[]
  histories: readonly Readonly<LifecycleHistory>[]; journal_error: string | null
}
export interface EmployeeLifecycleControllerOptions {
  send: (message: Readonly<Row>) => 'sent' | 'queued' | 'queue-full' | 'send-failed'
  nextRequestId: () => string; projectId: () => string | null
  /** Re-read the parent's current draft detail and unsaved/pending writes, not this child. */
  canWrite: (candidate: Readonly<LifecycleCandidate>) => boolean
  /** Actual parent enables history and a durable opaque-ID journal. Legacy isolated callers may omit them. */
  requireHistory?: boolean; journal?: LifecyclePendingJournal
  setTimer?: (callback: () => void, delay: number) => unknown; clearTimer?: (handle: unknown) => void
}
interface Pending { message: Readonly<Row>; candidate: Readonly<LifecycleCandidate>; entry_key: string; write: boolean; timer?: unknown }
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/, PACKAGE = /^[a-z0-9][a-z0-9_-]{2,63}$/, SHA = /^[a-f0-9]{64}$/, TRIAL = /^et-[a-f0-9]{24}$/
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const str = (value: unknown, max = 4000, empty = false): value is string => typeof value === 'string' && value.length <= max && !value.includes('\0') && (empty || !!value.trim())
const int = (value: unknown, low: number, high: number): value is number => Number.isInteger(value) && Number(value) >= low && Number(value) <= high
const valid = (value: unknown, re: RegExp): value is string => typeof value === 'string' && re.test(value)
const active = (entry: LifecycleEntry): boolean => !!entry.trial && ['prepared', 'running', 'outcome_unknown'].includes(entry.trial.state)
const writeAction = (action: unknown): boolean => [LIFECYCLE_ACTIONS.start, LIFECYCLE_ACTIONS.review, LIFECYCLE_ACTIONS.publish, LIFECYCLE_ACTIONS.abandon].includes(action as never)
const isBlocking = (receipt: LifecycleReceipt): boolean => receipt.write && ['pending', 'unknown'].includes(receipt.state)
const ACTIONS = new Set<string>([...Object.values(LIFECYCLE_ACTIONS), 'employee_instance_create'])
const STATES = new Set<string>(['prepared', 'running', 'awaiting_review', 'approved', 'rejected', 'failed', 'cancelled', 'outcome_unknown', 'abandoned'])
export class EmployeeLifecycleError extends Error {
  constructor(message = '先核对当前草稿、连接和原请求；没有启动新的测试或发布。') { super(message); this.name = 'EmployeeLifecycleError' }
}
function need(condition: unknown): asserts condition { if (!condition) throw new EmployeeLifecycleError() }
/** Bound JSON before cloning/freezing; never render or execute model-supplied markup. */
function frozenJson<T>(value: T, maxBytes = 2 * 1024 * 1024): T {
  let count = 0; const seen = new Set<object>()
  const walk = (v: unknown, depth: number): void => {
    need(++count <= 15000 && depth <= 20)
    if (v !== null && typeof v === 'object') {
      need(!seen.has(v)); seen.add(v)
      need(Array.isArray(v) || Object.getPrototypeOf(v) === Object.prototype || Object.getPrototypeOf(v) === null)
      for (const item of Object.values(v)) walk(item, depth + 1)
    } else need(v === null || ['string', 'boolean'].includes(typeof v) || typeof v === 'number' && Number.isFinite(v))
  }
  walk(value, 0); const serialized = JSON.stringify(value)
  need(new TextEncoder().encode(serialized).length <= maxBytes)
  const copy = JSON.parse(serialized) as T
  const freeze = (v: unknown): void => { if (v !== null && typeof v === 'object') { Object.values(v).forEach(freeze); Object.freeze(v) } }
  freeze(copy); return copy
}
export const lifecycleCandidateKey = (c: LifecycleCandidate): string => `${c.project_id}/${c.package_id}/${c.revision}/${c.fingerprint}`
  + (c.instance_selection ? `/instance/${c.instance_selection.instance_id}/${c.instance_selection.expected_revision}/${c.instance_selection.expected_fingerprint}` : '')
function instanceSelection(value: unknown): LifecycleInstanceSelection {
  need(row(value) && Object.keys(value).length === 3 && valid(value.instance_id, /^ei-[a-f0-9]{16}$/)
    && int(value.expected_revision, 1, Number.MAX_SAFE_INTEGER) && valid(value.expected_fingerprint, SHA))
  return frozenJson(value) as unknown as LifecycleInstanceSelection
}
function candidate(value: LifecycleCandidate): Readonly<LifecycleCandidate> {
  need(row(value) && Object.keys(value).length === (value.instance_selection === undefined ? 4 : 5) && valid(value.project_id, ID) && valid(value.package_id, PACKAGE) && valid(value.revision, SHA) && valid(value.fingerprint, SHA))
  return Object.freeze({ project_id: value.project_id, package_id: value.package_id, revision: value.revision, fingerprint: value.fingerprint,
    ...(value.instance_selection === undefined ? {} : { instance_selection: instanceSelection(value.instance_selection) }) })
}
function context(value: unknown): LifecycleContext {
  need(row(value) && [6, 8].includes(Object.keys(value).length) && row(value.model_config) && Object.keys(value.model_config).length === 3)
  if (Object.keys(value).length === 8) {
    const scope = value.qualification_scope
    need(valid(value.knowledge_refs_fingerprint, SHA) && row(scope)
      && (scope.kind === 'package' && Object.keys(scope).length === 1 || scope.kind === 'instance' && Object.keys(scope).length === 3
        && valid(scope.project_id, ID) && valid(scope.instance_id, /^ei-[a-f0-9]{16}$/)))
    need(row(value.checker_versions) && valid(value.checker_versions.employee_configuration, SHA))
  } else need(value.qualification_scope === undefined && value.knowledge_refs_fingerprint === undefined)
  need(['provider', 'model', 'tier'].every(k => str((value.model_config as Row)[k], 256)))
  need(row(value.capability_impls) && Object.keys(value.capability_impls).length === 0 && valid(value.runtime_profile_fingerprint, SHA) && valid(value.params_fingerprint, SHA))
  for (const key of ['checker_versions', 'skill_tree_fingerprints']) {
    const mapping = value[key]; need(row(mapping) && Object.keys(mapping).length <= 64 && Object.entries(mapping).every(([k, v]) => str(k, 256) && str(v, 256)))
  }
  return frozenJson(value) as unknown as LifecycleContext
}
function abandonAudit(value: unknown, trial: Row, c: Readonly<LifecycleCandidate>): Readonly<LifecycleAbandonAudit> {
  const keys = ['kind', 'trial_id', 'package_id', 'fingerprint', 'prior_state', 'actor', 'request_id', 'reason',
    'at', 'budget_id', 'observations_sha256', 'spend_reset', 'reviewable', 'publishable']
  need(row(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key)) && value.kind === 'employee_trial_abandon'
    && value.trial_id === trial.trial_id && value.package_id === c.package_id && value.fingerprint === c.fingerprint
    && value.prior_state === 'outcome_unknown' && str(value.actor, 128) && valid(value.request_id, ID)
    && str(value.reason, 4000) && str(value.at, 128) && !/[\x00-\x1f]/.test(value.at)
    && (value.budget_id === null || str(value.budget_id, 128)) && valid(value.observations_sha256, SHA)
    && value.spend_reset === false && value.reviewable === false && value.publishable === false)
  return frozenJson(value, 65536) as unknown as Readonly<LifecycleAbandonAudit>
}
export class EmployeeLifecycleController {
  private snapshot: LifecycleSnapshot = Object.freeze({ connected: false, entries: Object.freeze([]), receipts: Object.freeze([]), histories: Object.freeze([]), journal_error: null })
  private readonly listeners = new Set<() => void>(); private readonly requests = new Map<string, Pending>()
  private readonly setTimer: (callback: () => void, delay: number) => unknown
  private readonly clearTimer: (handle: unknown) => void; private disposed = false
  constructor(private readonly options: EmployeeLifecycleControllerOptions) {
    need((options.setTimer === undefined) === (options.clearTimer === undefined))
    this.setTimer = options.setTimer ?? ((fn, delay) => globalThis.setTimeout(fn, delay))
    this.clearTimer = options.clearTimer ?? (handle => globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>))
    try { for (const record of options.journal?.list() ?? []) this.restorePending(record) }
    catch { this.snapshot = Object.freeze({ ...this.snapshot, journal_error: '原请求编号不能核实；禁止新的测试、审核、作废或发布，不要清理未知请求。' }) }
  }
  getSnapshot = (): LifecycleSnapshot => this.snapshot
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private emit(next: LifecycleSnapshot): void {
    this.snapshot = Object.freeze({ ...next, entries: Object.freeze([...next.entries]), receipts: Object.freeze([...next.receipts]), histories: Object.freeze([...next.histories]) })
    for (const listener of [...this.listeners]) listener()
  }
  hasBlockingWrite = (): boolean => !!this.snapshot.journal_error || this.snapshot.receipts.some(isBlocking) || this.snapshot.entries.some(active)
  hasProtectedProject = (id: string): boolean => this.snapshot.entries.some(e => e.candidate.project_id === id && active(e))
    || this.snapshot.receipts.some(r => r.project_id === id && (r.state === 'pending' || isBlocking(r)))
  forgetProject(id: string): void {
    need(!this.hasProtectedProject(id)); for (const [rid, req] of this.requests) if (req.candidate.project_id === id) { this.stopTimer(req); this.requests.delete(rid) }
    this.emit({ ...this.snapshot, entries: this.snapshot.entries.filter(e => e.candidate.project_id !== id), receipts: this.snapshot.receipts.filter(r => r.project_id !== id), histories: this.snapshot.histories.filter(h => h.project_id !== id) })
  }
  connectionChanged(connected: boolean): void {
    need(!this.disposed); if (this.snapshot.connected === connected) return
    this.emit({ ...this.snapshot, connected, entries: this.snapshot.entries.map(e => Object.freeze({ ...e, stale: true })) })
    if (!connected) this.closeTimers()
  }
  private scope(c: LifecycleCandidate): void { need(!this.disposed && this.snapshot.connected && this.options.projectId() === c.project_id) }
  private entry(key: string): LifecycleEntry { const value = this.snapshot.entries.find(e => e.key === key); need(value); return value }
  private changeEntry(key: string, change: Partial<LifecycleEntry>): void {
    this.emit({ ...this.snapshot, entries: this.snapshot.entries.map(e => e.key === key ? Object.freeze({ ...e, ...change }) : e) })
  }
  private assertWrite(e: LifecycleEntry, action: Action): void {
    this.scope(e.candidate); need(!e.stale && !this.hasBlockingWrite() && this.options.canWrite(e.candidate))
    if (action === LIFECYCLE_ACTIONS.start) this.requireCurrentHistory(e.candidate)
    need(!this.snapshot.receipts.some(r => r.entry_key === e.key && r.state === 'pending'))
    const grants = action === LIFECYCLE_ACTIONS.start ? e.prepared?.allowed_actions : e.trial?.allowed_actions
    need(grants?.includes(action) && !e.published)
  }
  prepare(input: LifecycleCandidate): string {
    const c = candidate(input); this.scope(c); need(this.options.canWrite(c) && !this.hasBlockingWrite())
    this.requireCurrentHistory(c)
    const key = lifecycleCandidateKey(c)
    need(!this.snapshot.entries.find(e => e.key === key)?.published)
    need(!this.snapshot.receipts.some(r => r.entry_key === key && r.state === 'pending'))
    if (!this.snapshot.entries.some(e => e.key === key)) {
      let entries = this.snapshot.entries
      if (entries.length >= 8) {
        const victim = entries.find(e => !active(e) && !this.snapshot.receipts.some(r => r.entry_key === e.key && (r.state === 'pending' || isBlocking(r))))
        need(victim); entries = entries.filter(e => e.key !== victim.key)
      }
      this.emit({ ...this.snapshot, entries: [...entries, Object.freeze({ key, candidate: c, prepared: null, trial: null, published: false, stale: true, latest_request_id: null })] })
    }
    return this.send(key, LIFECYCLE_ACTIONS.prepare, { package_id: c.package_id, expected_revision: c.revision, expected_fingerprint: c.fingerprint,
      ...(c.instance_selection ? { instance_selection: c.instance_selection } : {}) })
  }
  private requireCurrentHistory(c: LifecycleCandidate): void {
    if (!this.options.requireHistory) return
    const history = this.snapshot.histories.find(h => h.project_id === c.project_id && h.package_id === c.package_id)
    need(history && !historyBlocksNewTest(history))
  }
  private ensureRecoveryEntry(c: Readonly<LifecycleCandidate>): string {
    const key = lifecycleCandidateKey(c)
    if (this.snapshot.entries.some(e => e.key === key)) return key
    let entries = this.snapshot.entries
    if (entries.length >= 8) {
      const victim = entries.find(e => !active(e) && !this.snapshot.receipts.some(r => r.entry_key === e.key && (r.state === 'pending' || isBlocking(r))))
      need(victim); entries = entries.filter(e => e.key !== victim.key)
    }
    this.emit({ ...this.snapshot, entries: [...entries, Object.freeze({ key, candidate: c, prepared: null, trial: null, published: false, stale: true, latest_request_id: null })] })
    return key
  }
  private restorePending(record: Readonly<LifecyclePendingId>): string {
    const c = candidate({ project_id: record.project_id, package_id: record.package_id, revision: record.revision, fingerprint: record.fingerprint,
      ...(record.instance_selection ? { instance_selection: record.instance_selection } : {}) })
    const key = this.ensureRecoveryEntry(c), old = this.snapshot.receipts.find(r => r.request_id === record.request_id)
    need(!old || old.entry_key === key && old.action === record.action)
    const message = frozenJson({ type: record.action, project_id: c.project_id, request_id: record.request_id,
      package_id: c.package_id, expected_revision: c.revision, expected_fingerprint: c.fingerprint,
      ...(record.trial_id ? { trial_id: record.trial_id } : {}), recovered: true })
    this.requests.set(record.request_id, { message, candidate: c, entry_key: key, write: true })
    const receipt: LifecycleReceipt = Object.freeze({ request_id: record.request_id, action: record.action, project_id: c.project_id,
      package_id: c.package_id, entry_key: key, write: true, state: 'unknown', message: '已恢复原请求编号；只读核对，不会再次测试、审核、作废或发布。' })
    this.emit({ ...this.snapshot, receipts: [...this.snapshot.receipts.filter(r => r.request_id !== record.request_id), receipt] })
    return key
  }
  readHistory(input: LifecycleCandidate): string {
    const c = candidate(input); this.scope(c)
    need(!this.snapshot.receipts.some(r => r.project_id === c.project_id && r.package_id === c.package_id && r.action === LIFECYCLE_ACTIONS.history && r.state === 'pending'))
    const key = this.ensureRecoveryEntry(c), old = this.snapshot.histories.find(h => h.project_id === c.project_id && h.package_id === c.package_id)
    need(!this.snapshot.receipts.some(r => r.entry_key === key && r.state === 'pending'))
    const history: Readonly<LifecycleHistory> = Object.freeze({ project_id: c.project_id, package_id: c.package_id, status: 'pending',
      trials: old?.trials ?? Object.freeze([]), publications: old?.publications ?? Object.freeze([]), has_more: false, blocked_elsewhere: true })
    this.emit({ ...this.snapshot, histories: [...this.snapshot.histories.filter(h => h.project_id !== c.project_id || h.package_id !== c.package_id), history] })
    try { return this.send(key, LIFECYCLE_ACTIONS.history, { package_id: c.package_id }) }
    catch (error) {
      this.emit({ ...this.snapshot, histories: this.snapshot.histories.map(h => h.project_id === c.project_id && h.package_id === c.package_id ? Object.freeze({ ...h, status: 'failed' as const }) : h) })
      throw error
    }
  }
  restoreTrial(projectId: string, packageId: string, trialId: string): string {
    const history = this.snapshot.histories.find(h => h.project_id === projectId && h.package_id === packageId && h.status === 'ready')
    const source = history?.trials.find(t => t.trial_id === trialId); need(source)
    const c = candidate({ project_id: projectId, package_id: packageId, revision: source.candidate_revision, fingerprint: source.fingerprint,
      ...(source.instance_selection ? { instance_selection: source.instance_selection } : {}) }); this.scope(c)
    const key = this.ensureRecoveryEntry(c), e = this.entry(key)
    need(!e.trial || !active(e) || e.trial.trial_id === source.trial_id)
    need(!this.snapshot.receipts.some(r => r.entry_key === key && r.state === 'pending'))
    this.restorePending({ ...c, action: LIFECYCLE_ACTIONS.start, request_id: source.request_id })
    return this.lookupOriginalStart(source.request_id)
  }
  restorePublication(projectId: string, packageId: string, requestId: string): string {
    const history = this.snapshot.histories.find(h => h.project_id === projectId && h.package_id === packageId && h.status === 'ready')
    const source = history?.publications.find(p => p.request_id === requestId); need(source)
    const c = candidate({ project_id: projectId, package_id: packageId, revision: source.candidate_revision, fingerprint: source.fingerprint }); this.scope(c)
    const key = this.ensureRecoveryEntry(c)
    need(!this.snapshot.receipts.some(r => r.entry_key === key && r.state === 'pending'))
    this.restorePending({ ...c, action: LIFECYCLE_ACTIONS.publish, request_id: source.request_id })
    return this.lookupOriginalPublish(source.request_id)
  }
  lookupOriginalReview(requestId: string): string {
    const original = this.snapshot.receipts.find(r => r.request_id === requestId), request = this.requests.get(requestId)
    need(original?.action === LIFECYCLE_ACTIONS.review && original.state === 'unknown' && request && valid(request.message.trial_id, TRIAL))
    const e = this.entry(original.entry_key); this.scope(e.candidate)
    return this.send(e.key, LIFECYCLE_ACTIONS.detail, { trial_id: request.message.trial_id })
  }
  lookupOriginalAbandon(requestId: string): string {
    const original = this.snapshot.receipts.find(r => r.request_id === requestId), request = this.requests.get(requestId)
    need(original?.action === LIFECYCLE_ACTIONS.abandon && original.state === 'unknown' && request && valid(request.message.trial_id, TRIAL))
    const e = this.entry(original.entry_key); this.scope(e.candidate)
    return this.send(e.key, LIFECYCLE_ACTIONS.detail, { trial_id: request.message.trial_id })
  }
  /** This closes an original trial, not the current draft. The server rechecks owner authority.
   * Old revisions remain recoverable; stale reads and uncertain write receipts never authorize it. */
  canAbandonTrial = (key: string): boolean => {
    const e = this.snapshot.entries.find(item => item.key === key)
    return !this.disposed && this.snapshot.connected && !!e && this.options.projectId() === e.candidate.project_id
      && !e.stale && !e.published && e.trial?.state === 'outcome_unknown' && !this.snapshot.journal_error
      && !this.snapshot.receipts.some(r => isBlocking(r) || r.entry_key === key && r.state === 'pending')
  }
  abandonTrial(key: string, reason: string): string {
    const e = this.entry(key); this.scope(e.candidate)
    need(this.canAbandonTrial(key) && e.trial && str(reason, 4000))
    return this.send(key, LIFECYCLE_ACTIONS.abandon, { trial_id: e.trial.trial_id, expected_state: 'outcome_unknown', reason })
  }
  startTest(key: string, budget: LifecycleBudget, reason: string): string {
    const e = this.entry(key); this.assertWrite(e, LIFECYCLE_ACTIONS.start)
    need(row(budget) && Object.keys(budget).length === 3 && valid(budget.max_cost_cny, /^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$/) && Number(budget.max_cost_cny) > 0
      && int(budget.max_model_calls, 1, 128) && int(budget.max_wall_clock_s, 1, 600) && str(reason, 4000))
    return this.send(key, LIFECYCLE_ACTIONS.start, { package_id: e.candidate.package_id, expected_revision: e.candidate.revision,
      expected_fingerprint: e.candidate.fingerprint, reason, data_scope: e.candidate.instance_selection ? 'instance_taskset' : 'package_taskset', budget: { ...budget },
      ...(e.candidate.instance_selection ? { instance_selection: e.candidate.instance_selection } : {}) })
  }
  refreshTrial(key: string): string {
    const e = this.entry(key); this.scope(e.candidate); need(e.trial)
    return this.send(key, LIFECYCLE_ACTIONS.detail, { trial_id: e.trial.trial_id })
  }
  lookupOriginalStart(requestId: string): string {
    const r = this.snapshot.receipts.find(item => item.request_id === requestId)
    need(r?.action === LIFECYCLE_ACTIONS.start && r.state === 'unknown')
    const e = this.entry(r.entry_key); this.scope(e.candidate)
    return this.send(e.key, LIFECYCLE_ACTIONS.lookup, { original_request_id: requestId })
  }
  lookupOriginalPublish(requestId: string): string {
    const r = this.snapshot.receipts.find(item => item.request_id === requestId)
    need(r?.action === LIFECYCLE_ACTIONS.publish && r.state === 'unknown')
    const e = this.entry(r.entry_key); this.scope(e.candidate)
    return this.send(e.key, LIFECYCLE_ACTIONS.publishLookup, { original_request_id: requestId,
      package_id: e.candidate.package_id, expected_fingerprint: e.candidate.fingerprint })
  }
  review(key: string, decision: 'approve' | 'reject', judgments: readonly LifecycleJudgment[], note: string): string {
    const e = this.entry(key); this.assertWrite(e, LIFECYCLE_ACTIONS.review)
    const t = e.trial; need(t?.state === 'awaiting_review' && t.observations.length === t.sample_count)
    need(['approve', 'reject'].includes(decision) && str(note, 4000, true) && Array.isArray(judgments) && judgments.length === t.sample_count)
    judgments.forEach((j, index) => {
      need(row(j) && Object.keys(j).length === 3 && j.case_id === t.observations[index].case_id && typeof j.non_inferior_met === 'boolean' && typeof j.absolute_threshold_met === 'boolean')
      if (decision === 'approve') need(j.non_inferior_met && j.absolute_threshold_met && t.observations[index].completed && t.observations[index].hard_fail_count === 0)
    })
    return this.send(key, LIFECYCLE_ACTIONS.review, { trial_id: t.trial_id, expected_fingerprint: e.candidate.fingerprint, decision, judgments: frozenJson(judgments, 32768), note })
  }
  publishPackage(key: string): string {
    const e = this.entry(key); need(!e.candidate.instance_selection && e.trial?.context.qualification_scope?.kind !== 'instance')
    this.assertWrite(e, LIFECYCLE_ACTIONS.publish); need(e.trial?.state === 'approved' && e.trial.test_passed === true)
    return this.send(key, LIFECYCLE_ACTIONS.publish, { package_id: e.candidate.package_id, expected_fingerprint: e.candidate.fingerprint })
  }
  private send(key: string, action: Action, fields: Row): string {
    const e = this.entry(key); this.scope(e.candidate)
    need(!this.snapshot.receipts.some(r => r.entry_key === key && r.state === 'pending'))
    const id = this.options.nextRequestId(); need(valid(id, ID) && !this.requests.has(id))
    if (this.requests.size >= 64) {
      const victim = this.snapshot.receipts.find(r => ['success', 'failed'].includes(r.state) && !this.snapshot.entries.some(entry => entry.latest_request_id === r.request_id))
      need(victim); this.requests.delete(victim.request_id)
      this.emit({ ...this.snapshot, receipts: this.snapshot.receipts.filter(r => r.request_id !== victim.request_id) })
    }
    const message = frozenJson({ type: action, project_id: e.candidate.project_id, request_id: id, ...fields }, 65536)
    if (writeAction(action) && this.options.journal) {
      try { this.options.journal.record({ ...e.candidate, action: action as LifecyclePendingId['action'], request_id: id,
        ...([LIFECYCLE_ACTIONS.review, LIFECYCLE_ACTIONS.abandon].includes(action as never) ? { trial_id: String(fields.trial_id) } : {}) }) }
      catch { throw new EmployeeLifecycleError('原请求编号未能可靠保存；本次操作没有发送。') }
    }
    const pending: Pending = { message, candidate: e.candidate, entry_key: key, write: writeAction(action) }
    this.requests.set(id, pending)
    this.emit({ ...this.snapshot, entries: this.snapshot.entries.map(item => item.key === key ? Object.freeze({ ...item, latest_request_id: id, stale: true,
      ...(action === LIFECYCLE_ACTIONS.start ? { prepared: null } : {}) }) : item),
      receipts: [...this.snapshot.receipts, Object.freeze({ request_id: id, action, project_id: e.candidate.project_id, package_id: e.candidate.package_id,
        entry_key: key, write: pending.write, state: 'pending', message: '已发出，正在等回执；不会自动重发。' })] })
    pending.timer = this.setTimer(() => this.timeout(id), 30000)
    try {
      const result = this.options.send(message)
      if (result === 'queue-full') this.finishReceipt(id, 'failed', '发送队列已满，本次请求未进入发送队列。')
      else if (result !== 'sent' && result !== 'queued') this.timeout(id)
    } catch { this.timeout(id) }
    return id
  }
  private stopTimer(p: Pending): void { if (p.timer !== undefined) { this.clearTimer(p.timer); delete p.timer } }
  private finishReceipt(id: string, state: LifecycleReceipt['state'], message: string): void {
    const p = this.requests.get(id); if (p) this.stopTimer(p)
    const receipt = this.snapshot.receipts.find(r => r.request_id === id)
    if (receipt?.write && (state === 'success' || state === 'failed') && this.options.journal) {
      try { this.options.journal.settle(id) }
      catch { this.emit({ ...this.snapshot, journal_error: '结果已收到，但原请求编号无法可靠结清；暂停新的写操作，保留编号供只读核对。' }) }
    }
    if (receipt?.action === LIFECYCLE_ACTIONS.history && state === 'failed') this.emit({ ...this.snapshot,
      histories: this.snapshot.histories.map(h => h.project_id === receipt.project_id && h.package_id === receipt.package_id ? Object.freeze({ ...h, status: 'failed' as const }) : h) })
    this.emit({ ...this.snapshot, receipts: this.snapshot.receipts.map(r => r.request_id === id ? Object.freeze({ ...r, state, message }) : r) })
  }
  private timeout(id: string): void {
    const r = this.snapshot.receipts.find(item => item.request_id === id); if (!r || r.state !== 'pending') return
    this.finishReceipt(id, r.write ? 'unknown' : 'failed', r.write ? '尚未收到可核实回执，结果未知；不自动重试、不重复付费或发布。' : '读取未完成；可手动重新读取，不把旧内容当作最新结果。')
  }
  receive(input: unknown): boolean { try { return this.accept(input) } catch { return false } }
  private accept(input: unknown): boolean {
    if (this.disposed || !row(input)) return false
    const p = input.type === 'ack' && row(input.payload) ? input.payload : input
    if (!valid(p.request_id, ID)) return false
    const req = this.requests.get(p.request_id), receipt = this.snapshot.receipts.find(r => r.request_id === p.request_id)
    if (!req || !receipt || !['pending', 'unknown'].includes(receipt.state)) return false
    if (p.action !== req.message.type || p.project_id !== req.candidate.project_id) return false
    const bounded = frozenJson(p); const actions = bounded.allowed_actions
    need(Array.isArray(actions) && actions.every(a => typeof a === 'string' && ACTIONS.has(a)) && Array.isArray(bounded.problems))
    if (p.action === LIFECYCLE_ACTIONS.history && bounded.ok === true && bounded.result === 'success') {
      const history = decodeLifecycleHistory(bounded, req.candidate.project_id, req.candidate.package_id)
      this.finishReceipt(p.request_id, 'success', '已读取原请求历史；没有启动测试、审核或发布。')
      this.emit({ ...this.snapshot, histories: [...this.snapshot.histories.filter(h => h.project_id !== history.project_id || h.package_id !== history.package_id), history] })
      return true
    }
    if (p.action === LIFECYCLE_ACTIONS.publishLookup && typeof bounded.lookup_found === 'boolean') {
      need(bounded.original_request_id === req.message.original_request_id && bounded.package_id === req.candidate.package_id && bounded.fingerprint === req.candidate.fingerprint
        && bounded.effects_produced === false && bounded.external_effects_produced === false)
      const original = this.snapshot.receipts.find(r => r.request_id === req.message.original_request_id)
      need(original?.action === LIFECYCLE_ACTIONS.publish && original.entry_key === req.entry_key && original.state === 'unknown')
      if (bounded.ok === true && bounded.result === 'success') {
        need(bounded.lookup_found === true && bounded.original_effects_produced === true && valid(bounded.revision, SHA)
          && actions.every(a => a === 'employee_instance_create'))
        this.finishReceipt(p.request_id, 'success', '只读核对已找到原发布完成记录，没有再次执行发布。')
        this.finishReceipt(original.request_id, 'success', '原发布已由服务端的只读事实记录确认。')
        this.changeEntry(req.entry_key, { published: true, stale: !this.snapshot.connected })
      } else {
        need(bounded.ok === false && bounded.result === 'outcome_unknown' && bounded.original_effects_produced === null && actions.length === 0)
        this.finishReceipt(p.request_id, 'failed', '原发布仍不能确认；没有记录也不代表未发布，禁止重新发布。')
      }
      return true
    }
    if (bounded.ok !== true || bounded.result !== 'success') {
      need(bounded.ok === false && ['failed', 'unsupported', 'outcome_unknown'].includes(String(bounded.result)) && actions.length === 0)
      const unknown = bounded.result === 'outcome_unknown' || bounded.effects_produced !== false || bounded.external_effects_produced !== false
      // A late failure is not evidence that a previously uncertain write had no effects.
      const state = req.write && (unknown || receipt.state === 'unknown') ? 'unknown' : 'failed'
      this.finishReceipt(p.request_id, state, str(bounded.message) ? bounded.message : state === 'unknown' ? '操作结果未知；保留原请求，不能重发。' : '服务端未接受此操作，请核对当前草稿与权限。')
      return true
    }
    need(bounded.package_id === req.candidate.package_id && bounded.fingerprint === req.candidate.fingerprint)
    // Starting a paid test confirms its local admission, not absence of model effects.
    need(p.action === LIFECYCLE_ACTIONS.start ? bounded.external_effects_produced === null : bounded.external_effects_produced === false)
    const e = this.entry(req.entry_key), latest = e.latest_request_id === p.request_id
    if (p.action === LIFECYCLE_ACTIONS.publish) {
      need(bounded.effects_produced === true && valid(bounded.revision, SHA) && actions.every(a => a === 'employee_instance_create'))
      this.finishReceipt(p.request_id, 'success', '发布结果已由服务端确认；可重新读取发布目录。')
      this.changeEntry(e.key, { published: true, stale: false }); return true
    }
    need(bounded.effects_produced === writeAction(p.action) && valid(bounded.revision, SHA) && int(bounded.sample_count, 1, 64))
    const ctx = context(bounded.context), selected = req.candidate.instance_selection
    need(selected ? ctx.qualification_scope?.kind === 'instance'
      && ctx.qualification_scope.project_id === req.candidate.project_id && ctx.qualification_scope.instance_id === selected.instance_id
      : ctx.qualification_scope === undefined || ctx.qualification_scope.kind === 'package')
    if (p.action === LIFECYCLE_ACTIONS.prepare) {
      need(bounded.revision === req.candidate.revision && bounded.data_scope === (selected ? 'instance_taskset' : 'package_taskset') && bounded.human_quality_review_required === true
        && actions.every(a => a === LIFECYCLE_ACTIONS.start))
      this.finishReceipt(p.request_id, 'success', '只读准备完成；尚未调用模型或计费。')
      if (latest) this.changeEntry(e.key, { prepared: Object.freeze({ sample_count: bounded.sample_count, context: ctx, allowed_actions: actions as string[] }), trial: null, stale: !this.snapshot.connected })
      return true
    }
    need(valid(bounded.trial_id, TRIAL) && valid(bounded.test_request_id, ID) && bounded.candidate_revision === req.candidate.revision && STATES.has(String(bounded.state))
      && typeof bounded.test_passed === 'boolean' && bounded.test_passed === (bounded.state === 'approved'))
    if (req.message.trial_id) need(bounded.trial_id === req.message.trial_id)
    if (req.message.trial_id && e.trial) need(bounded.test_request_id === e.trial.test_request_id)
    if (p.action === LIFECYCLE_ACTIONS.start) need(bounded.test_request_id === p.request_id)
    if (p.action === LIFECYCLE_ACTIONS.lookup) need(bounded.test_request_id === req.message.original_request_id)
    need(Array.isArray(bounded.cases) && bounded.cases.length === bounded.sample_count && Array.isArray(bounded.observations) && bounded.observations.length <= bounded.sample_count)
    const cases = bounded.cases, observations = bounded.observations; const ids = new Set<string>()
    cases.forEach(item => { need(row(item) && str(item.id, 128) && !ids.has(item.id)); ids.add(item.id) })
    observations.forEach((o, i) => {
      need(row(o) && Object.keys(o).length === 5 && o.case_id === (cases[i] as Row).id && typeof o.completed === 'boolean' && int(o.hard_fail_count, 0, 1000)
        && typeof o.output === 'string' && new TextEncoder().encode(o.output).length <= 65536 && o.model === ctx.model_config.model)
    })
    need(actions.every(a => bounded.state === 'awaiting_review' ? a === LIFECYCLE_ACTIONS.review : bounded.state === 'approved' && !selected ? a === LIFECYCLE_ACTIONS.publish : false))
    if (['awaiting_review', 'approved', 'rejected'].includes(String(bounded.state))) need(observations.length === bounded.sample_count)
    if (bounded.state === 'approved') need(observations.every(o => row(o) && o.completed === true && o.hard_fail_count === 0))
    const decided = ['approved', 'rejected'].includes(String(bounded.state))
    need(decided ? valid(bounded.review_request_id, ID) && bounded.decision === (bounded.state === 'approved' ? 'approve' : 'reject')
      : bounded.review_request_id === null && bounded.decision === null)
    if (p.action === LIFECYCLE_ACTIONS.review) need(bounded.state === (req.message.decision === 'approve' ? 'approved' : 'rejected')
      && bounded.review_request_id === p.request_id && bounded.decision === req.message.decision)
    const abandon = bounded.state === 'abandoned' ? abandonAudit(bounded.abandon, bounded, req.candidate) : undefined
    if (!abandon) need(bounded.abandon === undefined)
    if (p.action === LIFECYCLE_ACTIONS.abandon) need(abandon && abandon.request_id === p.request_id
      && (req.message.recovered === true || abandon.reason === req.message.reason))
    // A read already in flight cannot turn a confirmed terminal record back into an active trial.
    if (e.trial?.trial_id === bounded.trial_id && e.trial.state === 'abandoned') need(!!abandon
      && abandon.request_id === e.trial.abandon?.request_id)
    const t: LifecycleTrial = frozenJson({ trial_id: bounded.trial_id, state: bounded.state as TrialState, test_request_id: bounded.test_request_id,
      candidate_revision: bounded.candidate_revision, revision: bounded.revision, test_passed: bounded.test_passed, sample_count: bounded.sample_count,
      review_request_id: bounded.review_request_id, decision: bounded.decision, context: ctx, cases, observations, allowed_actions: actions,
      ...(abandon ? { abandon } : {}) }) as unknown as LifecycleTrial
    if (abandon && e.trial?.state !== 'abandoned') this.emit({ ...this.snapshot,
      histories: this.snapshot.histories.map(h => h.project_id === req.candidate.project_id ? Object.freeze({ ...h, status: 'failed' as const }) : h) })
    this.finishReceipt(p.request_id, 'success', abandon ? '已核实作废记录；原费用和观察保留，不算测试通过。刷新历史后可准备新测试。'
      : '已读取服务端真实测试记录；人工审核与发布分别确认。')
    // A late original start can arrive while a read lookup is still pending.
    // Preserve its running state even then, or clearing the unknown receipt
    // would temporarily allow unrelated writes while the worker is active.
    if (latest || p.action === LIFECYCLE_ACTIONS.start && e.trial === null || p.action === LIFECYCLE_ACTIONS.abandon)
      this.changeEntry(e.key, { trial: t, stale: !this.snapshot.connected || !latest })
    if (p.action === LIFECYCLE_ACTIONS.lookup) {
      const original = this.snapshot.receipts.find(r => r.request_id === req.message.original_request_id)
      if (original?.action === LIFECYCLE_ACTIONS.start && original.entry_key === e.key && original.state === 'unknown') {
        this.finishReceipt(original.request_id, 'success', '已通过只读查询找到原测试；没有启动第二次测试。')
      }
    }
    if (p.action === LIFECYCLE_ACTIONS.detail && decided) {
      const old = this.snapshot.receipts.find(r => r.request_id === bounded.review_request_id)
      const request = old ? this.requests.get(old.request_id) : undefined
      if (old?.action === LIFECYCLE_ACTIONS.review && old.entry_key === e.key && old.state === 'unknown'
        && request?.message.trial_id === bounded.trial_id && (request.message.recovered === true || request.message.decision === bounded.decision)) {
        this.finishReceipt(old.request_id, 'success', '只读测试记录已匹配原审核请求与决定，没有再次提交审核。')
      }
    }
    if (p.action === LIFECYCLE_ACTIONS.detail && abandon) {
      const old = this.snapshot.receipts.find(r => r.request_id === abandon.request_id)
      const request = old ? this.requests.get(old.request_id) : undefined
      if (old?.action === LIFECYCLE_ACTIONS.abandon && old.entry_key === e.key && old.state === 'unknown'
        && request?.message.trial_id === bounded.trial_id && (request.message.recovered === true || request.message.reason === abandon.reason)) {
        this.finishReceipt(old.request_id, 'success', '只读记录已匹配原作废请求；没有再次提交，原费用和观察未清除。')
      }
    }
    return true
  }
  closeTimers(): void { for (const [id, p] of this.requests) { this.timeout(id); this.stopTimer(p) } }
  dispose(): void { this.closeTimers(); this.listeners.clear(); this.disposed = true }
}

