/** Original socket transport injected by the asset controller; no polling or auto-save. */
export interface EmployeeKnowledgeScope { project_id: string; instance_id: string; revision: number; fingerprint: string; allowed: boolean }
export interface EmployeeKnowledgeDescriptor {
  kind: 'enterprise_shared'; ref: 'project-memory'; version: string; access_scope: string
  label: '本项目共享知识'; content_bytes: number; reader_fingerprint: string
}
type Wire = Readonly<Record<string, unknown>>
type Send = 'sent' | 'queued' | 'queue-full' | 'send-failed'
export interface EmployeeKnowledgeOptions {
  send: (message: Wire) => Send; nextRequestId: () => string
  current: () => EmployeeKnowledgeScope | null; confirmed: (scope: EmployeeKnowledgeScope) => void
  setTimer: (callback: () => void, delay: number) => unknown; clearTimer: (handle: unknown) => void
}
export interface EmployeeKnowledgeSnapshot {
  state: 'idle' | 'loading' | 'ready' | 'saving' | 'succeeded' | 'failed' | 'unknown'
  scope: EmployeeKnowledgeScope | null; descriptor: EmployeeKnowledgeDescriptor | null
  selected_version: string | null; choice: 'project-memory' | 'none' | null; message: string
}
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)
const sha = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const version = (value: unknown): value is string => typeof value === 'string' && /^sha256:[a-f0-9]{64}$/.test(value)
const key = (scope: EmployeeKnowledgeScope | null): string => scope ? JSON.stringify([scope.project_id, scope.instance_id, scope.revision, scope.fingerprint]) : ''
const initial = (): EmployeeKnowledgeSnapshot => Object.freeze({ state: 'idle', scope: null, descriptor: null, selected_version: null, choice: null, message: '' })
function descriptor(value: unknown, project: string): EmployeeKnowledgeDescriptor {
  if (!object(value) || Object.keys(value).sort().join(',') !== 'access_scope,content_bytes,kind,label,reader_fingerprint,ref,version'
    || value.kind !== 'enterprise_shared' || value.ref !== 'project-memory' || value.access_scope !== project
    || value.label !== '本项目共享知识' || !version(value.version) || !sha(value.reader_fingerprint)
    || !Number.isSafeInteger(value.content_bytes) || Number(value.content_bytes) < 1 || Number(value.content_bytes) > 262144) throw new Error('知识来源返回无效；没有使用旧版本。')
  return Object.freeze({ ...value }) as unknown as EmployeeKnowledgeDescriptor
}
export class EmployeeInstanceKnowledgeController {
  private snapshot: EmployeeKnowledgeSnapshot = initial()
  private readonly listeners = new Set<() => void>()
  private pending: { wire: Wire; scope: EmployeeKnowledgeScope; write: boolean; timer?: unknown; live: boolean; recovering: boolean } | null = null
  constructor(private readonly options: EmployeeKnowledgeOptions) {}
  getSnapshot = (): EmployeeKnowledgeSnapshot => this.snapshot
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private update(change: Partial<EmployeeKnowledgeSnapshot>): void { this.snapshot = Object.freeze({ ...this.snapshot, ...change }); for (const notify of [...this.listeners]) notify() }
  private current(): EmployeeKnowledgeScope {
    const scope = this.options.current()
    if (!scope || !scope.allowed || !id(scope.project_id) || !/^ei-[a-f0-9]{16}$/.test(scope.instance_id)
      || !Number.isSafeInteger(scope.revision) || scope.revision < 1 || !sha(scope.fingerprint)) throw new Error('请先读取当前项目岗位修订。')
    return Object.freeze({ ...scope })
  }
  isCurrent = (): boolean => key(this.snapshot.scope) !== '' && key(this.snapshot.scope) === key(this.options.current())
  hasBlockingWrite = (): boolean => !!this.pending?.write && (this.pending.live || this.snapshot.state === 'unknown')
  hasProtectedProject = (project: string): boolean => this.hasBlockingWrite() && this.pending?.scope.project_id === project
  private stopTimer(): void { if (this.pending?.timer !== undefined) { this.options.clearTimer(this.pending.timer); delete this.pending.timer } }
  private send(wire: Wire, scope: EmployeeKnowledgeScope, write: boolean, recovering = false): void {
    if (!id(wire.request_id)) throw new Error('请求标识无效。')
    this.stopTimer()
    this.pending = { wire: Object.freeze({ ...wire }), scope, write, live: true, recovering }
    this.update({ state: write ? 'saving' : 'loading', message: write ? '正在保存，等待服务端回执。' : '正在读取本项目知识来源版本。' })
    const requestId = wire.request_id
    this.pending.timer = this.options.setTimer(() => this.timeout(requestId), 30000)
    let result: Send
    try { result = this.options.send(wire) } catch { result = 'send-failed' }
    if (result !== 'sent') this.timeout(requestId)
  }
  load(): void {
    if (this.hasBlockingWrite()) throw new Error('先核对原保存结果，不会自动重发。')
    const scope = this.current()
    this.update({ ...initial(), scope })
    this.send({ type: 'employee_instance_knowledge_candidates', project_id: scope.project_id, instance_id: scope.instance_id,
      expected_revision: scope.revision, expected_fingerprint: scope.fingerprint, request_id: this.options.nextRequestId() }, scope, false)
  }
  choose(choice: 'project-memory' | 'none'): void {
    if (!this.isCurrent() || this.snapshot.state !== 'ready' || !this.snapshot.descriptor || this.hasBlockingWrite()
      || (choice !== 'project-memory' && choice !== 'none')) throw new Error('请重新读取来源后明确选择。')
    this.current(); this.update({ choice })
  }
  save(): void {
    const scope = this.current(), state = this.snapshot, source = state.descriptor
    if (state.state !== 'ready' || key(scope) !== key(state.scope) || !source || !state.choice || this.hasBlockingWrite()) throw new Error('知识选择尚不能保存。')
    this.send({ type: 'employee_instance_knowledge_select', request_id: this.options.nextRequestId(),
      project_id: scope.project_id, instance_id: scope.instance_id, expected_revision: scope.revision,
      expected_fingerprint: scope.fingerprint, selection: state.choice,
      expected_source_version: state.choice === 'none' ? null : source.version,
      expected_reader_fingerprint: state.choice === 'none' ? null : source.reader_fingerprint, note: '' }, scope, true)
  }
  revoke(): void {
    if (this.hasBlockingWrite()) throw new Error('先核对原保存结果。')
    const scope = this.current()
    this.update({ ...initial(), scope, choice: 'none' })
    this.send({ type: 'employee_instance_knowledge_select', request_id: this.options.nextRequestId(),
      project_id: scope.project_id, instance_id: scope.instance_id, expected_revision: scope.revision,
      expected_fingerprint: scope.fingerprint, selection: 'none', expected_source_version: null,
      expected_reader_fingerprint: null, note: '' }, scope, true)
  }
  recover(): void {
    const pending = this.pending, current = this.current()
    if (!pending?.write || pending.live || this.snapshot.state !== 'unknown' || key(current) !== key(pending.scope)) throw new Error('原保存请求尚不能核实。')
    this.send(Object.freeze({ ...pending.wire, check_only: true }), pending.scope, true, true)
  }
  timeout(requestId: string): void {
    if (!this.pending || this.pending.wire.request_id !== requestId || !this.pending.live) return
    this.stopTimer(); this.pending.live = false
    this.update({ state: this.pending.write ? 'unknown' : 'failed', message: this.pending.write
      ? '保存已超时或连接已断，结果未知；仅可核对原请求。' : '未取得来源版本，不使用旧快照。' })
  }
  connectionChanged(connected: boolean): void { if (!connected && this.pending?.live) this.timeout(String(this.pending.wire.request_id)) }
  receive(value: unknown): boolean {
    try {
      const reply = object(value) && value.type === 'ack' ? value.payload : value
      const pending = this.pending
      if (!object(reply) || !pending || reply.request_id !== pending.wire.request_id || reply.action !== pending.wire.type
        || reply.project_id !== pending.scope.project_id) return false
      if (pending.write && !pending.live && this.snapshot.state !== 'unknown') return false
      if (!pending.write && (!pending.live || key(pending.scope) !== key(this.options.current()))) return false
      if (reply.ok !== true || reply.result !== 'success') {
        this.stopTimer(); pending.live = false
        const unknown = pending.write && (pending.recovering || this.snapshot.state === 'unknown'
          || reply.result === 'outcome_unknown' || reply.effects_produced !== false)
        this.update({ state: unknown ? 'unknown' : 'failed', message: unknown ? '原保存结果尚不能确认；不会重新保存。' : '服务端拒绝了本次请求，请重新核对来源与岗位。' })
        return true
      }
      if (reply.instance_id !== pending.scope.instance_id || !sha(reply.fingerprint) || !Array.isArray(reply.allowed_actions)) return false
      if (!pending.write) {
        if (reply.revision !== pending.scope.revision || reply.fingerprint !== pending.scope.fingerprint
          || reply.effects_produced !== false || reply.projection_version !== 'employee-project-memory-selection.v1'
          || !Array.isArray(reply.candidates) || reply.candidates.length !== 1 || !Array.isArray(reply.selected) || reply.selected.length > 1
          || !reply.allowed_actions.includes('employee_instance_knowledge_select')) return false
        const source = descriptor(reply.candidates[0], pending.scope.project_id)
        let selected: string | null = null
        if (reply.selected.length) {
          const row = reply.selected[0]
          if (!object(row) || Object.keys(row).sort().join(',') !== 'access_scope,kind,ref,version'
            || row.kind !== 'enterprise_shared' || row.ref !== 'project-memory' || row.access_scope !== pending.scope.project_id || !version(row.version)) return false
          selected = row.version
        }
        this.stopTimer(); pending.live = false
        this.update({ state: 'ready', descriptor: source, selected_version: selected, choice: null, message: '已读取当前来源；请选择后单独保存。' })
      } else {
        if (reply.revision !== pending.scope.revision + 1 || reply.effects_produced !== true) return false
        this.stopTimer(); pending.live = false
        this.update({ state: 'succeeded', choice: null, message: '知识配置已保存；需重新评测，不代表已获准运行。' })
        this.options.confirmed(pending.scope)
      }
      return true
    } catch { return false }
  }
  closeTimers(): void { if (this.pending?.live) this.timeout(String(this.pending.wire.request_id)); this.stopTimer() }
  dispose(): void { this.closeTimers(); this.listeners.clear() }
}
