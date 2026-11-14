/** Explicit project-instance creation/adoption, over the App-owned existing socket.
 * No default grants, execution, polling, automatic retry or readiness inference.
 */
import type { EmployeeAssetSnapshot, EmployeeAssetReadView } from './aiEmployeeAssetController'

type Row = Record<string, unknown>
export type InstanceAdoptionAction = 'employee_instance_create' | 'employee_instance_adopt'
export interface InstanceAdoptionCandidate {
  kind: 'create' | 'adopt'; project_id: string; package_id: string; package_version: string; package_fingerprint: string
  package_revision: string; instance_id: string | null; instance_revision: number | null; instance_fingerprint: string | null
}
export interface InstanceAdoptionResult {
  instance_id: string; revision: number; fingerprint: string; package_version: string; package_fingerprint: string
}
export interface InstanceAdoptionReceipt {
  request_id: string; action: InstanceAdoptionAction; candidate: Readonly<InstanceAdoptionCandidate>
  state: 'pending' | 'unknown' | 'failed' | 'success'; checking: boolean; message: string
  result: Readonly<InstanceAdoptionResult> | null
}
export interface InstanceAdoptionSnapshot { connected: boolean; receipts: readonly InstanceAdoptionReceipt[] }
export interface InstanceAdoptionOptions {
  send: (message: Readonly<Row>) => 'sent' | 'queued' | 'queue-full' | 'send-failed'
  nextRequestId: () => string; projectId: () => string | null
  canWrite: (candidate: Readonly<InstanceAdoptionCandidate>) => boolean
  confirmed?: (candidate: Readonly<InstanceAdoptionCandidate>, result: Readonly<InstanceAdoptionResult>) => void
  setTimer?: (callback: () => void, delay: number) => unknown; clearTimer?: (handle: unknown) => void
}
interface Pending { original: Readonly<Row>; timer?: unknown; checking: boolean }
const SHA = /^[a-f0-9]{64}$/, ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/, PACKAGE = /^[a-z0-9][a-z0-9_-]{2,63}$/, INSTANCE = /^ei-[a-f0-9]{16}$/
const row = (v: unknown): v is Row => v !== null && typeof v === 'object' && !Array.isArray(v)
const valid = (v: unknown, re: RegExp): v is string => typeof v === 'string' && re.test(v)
const text = (v: unknown, max: number, empty = false): v is string => typeof v === 'string' && v.length <= max && (empty || !!v.trim()) && !/[\x00-\x1f\x7f]/.test(v)
const revision = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0 && v < Number.MAX_SAFE_INTEGER
const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b)
const actions = (v: unknown, action: string): boolean => Array.isArray(v) && v.includes(action)
const protectedReceipt = (v: InstanceAdoptionReceipt): boolean => v.state === 'pending' || v.state === 'unknown'
export class EmployeeInstanceAdoptionError extends Error {
  constructor(message = '请先重新核对固定岗位版本、当前项目与授权；尚未提交采用。') { super(message); this.name = 'EmployeeInstanceAdoptionError' }
}
function need(v: unknown): asserts v { if (!v) throw new EmployeeInstanceAdoptionError() }
export const instanceAdoptionKey = (v: InstanceAdoptionCandidate): string => JSON.stringify(v)
function candidate(v: InstanceAdoptionCandidate): Readonly<InstanceAdoptionCandidate> {
  need(row(v) && Object.keys(v).length === 9 && ['create', 'adopt'].includes(v.kind) && valid(v.project_id, ID) && valid(v.package_id, PACKAGE)
    && text(v.package_version, 128) && v.package_version.trim() === v.package_version && valid(v.package_fingerprint, SHA) && valid(v.package_revision, SHA))
  need(v.kind === 'create' ? v.instance_id === null && v.instance_revision === null && v.instance_fingerprint === null
    : valid(v.instance_id, INSTANCE) && revision(v.instance_revision) && valid(v.instance_fingerprint, SHA))
  return Object.freeze({ ...v })
}
function ready(view: EmployeeAssetReadView | undefined, project: string): view is EmployeeAssetReadView & { data: Row } {
  return view?.state === 'ready' && view.request?.project_id === project && !!view.data && view.data.effects_produced === false
}
function published(view: EmployeeAssetReadView | undefined, project: string): Row | null {
  if (!ready(view, project) || view.request?.action !== 'employee_package_detail' || view.request.source !== 'published') return null
  const data = view.data, manifest = row(data.package) ? data.package : null
  if (!manifest || data.status !== 'published' || manifest.status !== 'published' || data.new_adoption_allowed !== true
    || data.is_current_verified !== true || !valid(data.fingerprint, SHA) || !valid(data.revision, SHA)
    || manifest.package_fingerprint !== data.fingerprint || manifest.package_id !== view.request.package_id
    || !text(manifest.version, 128) || view.request.version !== manifest.version) return null
  return data
}
/** Only current server read projections produce gestures; a catalog green badge is insufficient. */
export function instanceAdoptionCandidates(snapshot: EmployeeAssetSnapshot): { create: InstanceAdoptionCandidate | null; adopt: InstanceAdoptionCandidate | null } {
  const result: { create: InstanceAdoptionCandidate | null; adopt: InstanceAdoptionCandidate | null } = { create: null, adopt: null }
  const id = snapshot.project_id, p = id ? snapshot.projects[id] : undefined
  if (!id || !snapshot.connected || !p) return result
  const create = published(p.detail, id)
  if (create && row(create.package) && ready(p.instances, id) && actions(p.instances.data.allowed_actions, 'employee_instance_create')) {
    result.create = candidate({ kind: 'create', project_id: id, package_id: String(create.package.package_id), package_version: String(create.package.version),
      package_fingerprint: String(create.fingerprint), package_revision: String(create.revision), instance_id: null, instance_revision: null, instance_fingerprint: null })
  }
  const target = published(p.adoption_target, id), detail = ready(p.detail, id) ? p.detail.data : null
  const instance = row(detail?.instance) ? detail.instance : null, preview = ready(p.adoption, id) ? p.adoption.data : null, req = p.adoption.request
  if (target && row(target.package) && instance && detail && p.detail.request?.action === 'employee_instance_detail'
    && instance.project_id === id && detail.revision === instance.instance_revision && detail.fingerprint === instance.revision_fingerprint
    && actions(detail.allowed_actions, 'employee_instance_adopt') && target.package.package_id === instance.package_id
    && target.package.version !== instance.package_version && p.adoption_target.request?.target_fingerprint === target.fingerprint
    && preview && p.adoption.receipt?.state === 'succeeded' && req?.action === 'employee_instance_adoption_preview'
    && req.instance_id === instance.instance_id && req.adoption_revision === detail.revision && req.adoption_instance_fingerprint === detail.fingerprint
    && req.target_fingerprint === target.fingerprint && req.version === target.package.version
    && preview.new_adoption_allowed === true && preview.is_current_verified === true && preview.target_status === 'published'
    && preview.to_fingerprint === target.fingerprint && preview.to_version === target.package.version
    && Array.isArray(preview.gaps) && !preview.gaps.includes('private_configuration_incompatible')) {
    result.adopt = candidate({ kind: 'adopt', project_id: id, package_id: String(instance.package_id), package_version: String(target.package.version),
      package_fingerprint: String(target.fingerprint), package_revision: String(target.revision), instance_id: String(instance.instance_id),
      instance_revision: Number(detail.revision), instance_fingerprint: String(detail.fingerprint) })
  }
  return result
}

export class EmployeeInstanceAdoptionController {
  private snapshot: InstanceAdoptionSnapshot = Object.freeze({ connected: false, receipts: Object.freeze([]) })
  private readonly listeners = new Set<() => void>(); private readonly requests = new Map<string, Pending>(); private disposed = false
  private readonly setTimer: (callback: () => void, delay: number) => unknown; private readonly clearTimer: (handle: unknown) => void
  constructor(private readonly options: InstanceAdoptionOptions) {
    need((options.setTimer === undefined) === (options.clearTimer === undefined))
    this.setTimer = options.setTimer ?? ((fn, delay) => globalThis.setTimeout(fn, delay))
    this.clearTimer = options.clearTimer ?? (h => globalThis.clearTimeout(h as ReturnType<typeof globalThis.setTimeout>))
  }
  getSnapshot = (): InstanceAdoptionSnapshot => this.snapshot
  subscribe = (fn: () => void): (() => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn) } }
  private emit(receipts: readonly InstanceAdoptionReceipt[] = this.snapshot.receipts): void {
    this.snapshot = Object.freeze({ ...this.snapshot, receipts: Object.freeze([...receipts]) }); for (const fn of [...this.listeners]) fn()
  }
  hasBlockingWrite = (): boolean => this.snapshot.receipts.some(protectedReceipt)
  hasProtectedProject = (id: string): boolean => this.snapshot.receipts.some(r => r.candidate.project_id === id && protectedReceipt(r))
  forgetProject(id: string): void {
    need(!this.hasProtectedProject(id)); for (const receipt of this.snapshot.receipts) if (receipt.candidate.project_id === id) this.requests.delete(receipt.request_id)
    this.emit(this.snapshot.receipts.filter(r => r.candidate.project_id !== id))
  }
  connectionChanged(connected: boolean): void {
    need(!this.disposed); this.snapshot = Object.freeze({ ...this.snapshot, connected }); if (!connected) this.closeTimers(); else this.emit()
  }
  private scope(project: string): void { need(!this.disposed && this.snapshot.connected && this.options.projectId() === project) }
  private settle(id: string, change: Partial<InstanceAdoptionReceipt>): void { this.emit(this.snapshot.receipts.map(r => r.request_id === id ? Object.freeze({ ...r, ...change }) : r)) }
  private stop(req: Pending): void { if (req.timer !== undefined) { this.clearTimer(req.timer); delete req.timer } }
  private send(id: string, req: Pending): void {
    req.timer = this.setTimer(() => this.markTimeout(id), 30_000)
    let result: ReturnType<InstanceAdoptionOptions['send']> = 'send-failed'
    try { result = this.options.send(req.checking ? Object.freeze({ ...req.original, check_only: true }) : req.original) } catch { /* unknown, never resubmit */ }
    if (result === 'queue-full' || result === 'send-failed') this.markTimeout(id)
  }
  private begin(input: InstanceAdoptionCandidate, note: string, config?: { display_name: string }): string {
    const c = candidate(input); this.scope(c.project_id); need(!this.hasBlockingWrite() && this.options.canWrite(c) && text(note, 4000, true))
    need(c.kind === 'create' ? !!config && Object.keys(config).length === 1 && text(config.display_name, 256) : config === undefined)
    const id = this.options.nextRequestId(); need(valid(id, ID) && !this.requests.has(id))
    if (this.snapshot.receipts.length >= 8) { const victim = this.snapshot.receipts.find(r => !protectedReceipt(r)); need(victim); this.requests.delete(victim.request_id); this.emit(this.snapshot.receipts.filter(r => r !== victim)) }
    const action: InstanceAdoptionAction = c.kind === 'create' ? 'employee_instance_create' : 'employee_instance_adopt'
    const original: Readonly<Row> = Object.freeze({ type: action, project_id: c.project_id, request_id: id, package_version: c.package_version,
      expected_fingerprint: c.package_fingerprint, note, ...(c.kind === 'create' ? { package_id: c.package_id, config: Object.freeze({ display_name: config!.display_name }) }
        : { instance_id: c.instance_id, expected_revision: c.instance_revision }) })
    const req: Pending = { original, checking: false }; this.requests.set(id, req)
    this.emit([...this.snapshot.receipts, Object.freeze({ request_id: id, action, candidate: c, state: 'pending', checking: false,
      result: null, message: '已提交，正在等待原请求回执；不会启动员工执行。' })]); this.send(id, req); return id
  }
  create(input: InstanceAdoptionCandidate, displayName: string, note: string): string { need(input.kind === 'create'); return this.begin(input, note, { display_name: displayName }) }
  adopt(input: InstanceAdoptionCandidate, note: string): string { need(input.kind === 'adopt'); return this.begin(input, note) }
  lookupOriginal(id: string): void {
    const r = this.snapshot.receipts.find(v => v.request_id === id), req = this.requests.get(id)
    need(r?.state === 'unknown' && req && !r.checking); this.scope(r.candidate.project_id)
    req.checking = true; this.settle(id, { checking: true, message: '只读核对原请求；不补写、不换标识、不重新创建。' }); this.send(id, req)
  }
  markTimeout(id: string): void {
    const r = this.snapshot.receipts.find(v => v.request_id === id), req = this.requests.get(id)
    if (!r || !req || r.state === 'success' || r.state === 'failed') return
    this.stop(req); this.settle(id, { state: 'unknown', checking: false, message: '结果未知，已保留原项目、版本与完整请求。只可读取原回执，不要重复创建或升级。' })
  }
  receive(envelope: unknown): boolean {
    if (!row(envelope)) return false
    const raw = envelope.type === 'ack' && row(envelope.payload) ? envelope.payload : envelope
    const id = typeof raw.request_id === 'string' ? raw.request_id : '', req = this.requests.get(id), receipt = this.snapshot.receipts.find(r => r.request_id === id)
    if (!req || !receipt || raw.action !== receipt.action || raw.project_id !== receipt.candidate.project_id) return false
    if (receipt.state === 'success' || receipt.state === 'failed') return true
    this.stop(req); const c = receipt.candidate
    const unknown = (): boolean => { this.settle(id, { state: 'unknown', checking: false, message: '回执尚不能完整核实；保留原请求，仅可只读核对。' }); return true }
    if (raw.ok !== true || raw.result !== 'success') {
      if (!req.checking && receipt.state === 'pending' && raw.ok === false && ['failed', 'unsupported'].includes(String(raw.result))
        && raw.effects_produced === false && raw.external_effects_produced === false && raw.original_effects_produced === null
        && same(raw.allowed_actions, [])) {
        this.settle(id, { state: 'failed', checking: false, message: '服务端拒绝了本次采用，未写入。请重新读取权限、版本与配置。' }); return true
      }
      return unknown()
    }
    const expectedKeys = ['ok','action','project_id','request_id','instance_id','revision','fingerprint','package_version','package_fingerprint','result',
      'effects_produced','original_effects_produced','external_effects_produced','retest_scope','allowed_actions','problems', ...(c.kind === 'create' ? ['package_id'] : [])]
    if (Object.keys(raw).some(k => !expectedKeys.includes(k) && k !== 'idempotent') || expectedKeys.some(k => !Object.hasOwn(raw, k))
      || Object.hasOwn(raw, 'idempotent') && raw.idempotent !== true || !valid(raw.instance_id, INSTANCE)
      || c.kind === 'adopt' && raw.instance_id !== c.instance_id || raw.revision !== (c.kind === 'create' ? 1 : c.instance_revision! + 1)
      || !valid(raw.fingerprint, SHA) || raw.package_version !== c.package_version || raw.package_fingerprint !== c.package_fingerprint
      || c.kind === 'create' && raw.package_id !== c.package_id || raw.effects_produced !== !req.checking || raw.original_effects_produced !== true
      || raw.external_effects_produced !== false || !same(raw.allowed_actions, ['employee_instance_detail']) || !same(raw.problems, [])
      || !same(raw.retest_scope, c.kind === 'create' ? [] : ['package_version', 'model_config', 'capability_impls'])) return unknown()
    const result = Object.freeze({ instance_id: raw.instance_id, revision: Number(raw.revision), fingerprint: raw.fingerprint,
      package_version: c.package_version, package_fingerprint: c.package_fingerprint })
    this.settle(id, { state: 'success', checking: false, result, message: c.kind === 'create'
      ? '项目岗位已创建；尚未配置的模型、人员与授权不会自动补齐。请读取实例就绪检查。'
      : '项目岗位已采用指定版本；私有配置保留，在途订单不改版。请重新检查本项目就绪情况。' })
    this.options.confirmed?.(c, result); return true
  }
  closeTimers(): void { for (const r of this.snapshot.receipts) if (protectedReceipt(r)) this.markTimeout(r.request_id) }
  dispose(): void { this.closeTimers(); this.listeners.clear(); this.disposed = true }
}
