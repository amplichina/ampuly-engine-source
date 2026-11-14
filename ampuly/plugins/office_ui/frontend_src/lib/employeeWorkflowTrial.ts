import type { WorkflowIntakeField } from '../components/WorkflowsPage'

export const EMPLOYEE_WORKFLOW_TRIAL_ACTIONS = {
  prepare: 'employee_workflow_trial_prepare', start: 'employee_workflow_trial_start', read: 'employee_workflow_trial_read',
} as const
export type EmployeeWorkflowTrialAction = typeof EMPLOYEE_WORKFLOW_TRIAL_ACTIONS[keyof typeof EMPLOYEE_WORKFLOW_TRIAL_ACTIONS]
export type EmployeeWorkflowTrialSend = (action: EmployeeWorkflowTrialAction, payload: Record<string, unknown>) => 'sent' | 'send-failed'
export interface EmployeeWorkflowTrialContext {
  projectId: string
  workflowId: string
  workflowVersion: string
  manifestSha256: string
  workflowStatus: string
  saved: boolean
  hasEmployeeBinding: boolean
  intakeFields: readonly WorkflowIntakeField[]
}
/** The server's ACK, not current UI scope dressed up as server facts. */
export interface EmployeeWorkflowTrialAck {
  action: EmployeeWorkflowTrialAction
  project_id: string
  request_id: string
  ok: boolean
  workflow_id?: string
  workflow_version?: string
  manifest_sha256?: string
  result?: Record<string, unknown>
  error?: string
}
export interface EmployeeTrialRecovery {
  requestId: string
  workflowVersion: string
  manifestSha256: string
}
export interface EmployeeTrialRecoveryStore {
  load: () => EmployeeTrialRecovery | null
  save: (record: EmployeeTrialRecovery) => void
  retire?: (record: EmployeeTrialRecovery) => void
}
/** Store request identity only; never persist intake, budget, auth or server receipts. */
export function employeeTrialRecoveryStore(storage: Pick<Storage, 'getItem' | 'setItem'>,
  projectId: string, workflowId: string): EmployeeTrialRecoveryStore {
  const key = `ampuly.employee-trial.v1:${JSON.stringify([projectId, workflowId])}`
  return {
    load() {
      const raw = storage.getItem(key)
      if (raw === null) return null
      if (raw.length > 1024) throw new Error('employee_trial_recovery_invalid')
      const parsed = JSON.parse(raw)
      const row = (parsed?.retired ?? parsed) as EmployeeTrialRecovery
      if (!row || !UUID.test(row.requestId) || !ID.test(row.workflowVersion) || !SHA.test(row.manifestSha256))
        throw new Error('employee_trial_recovery_invalid')
      return parsed?.retired ? null : row
    },
    retire(row) { storage.setItem(key, JSON.stringify({ retired: row })) },
    save(row) { storage.setItem(key, JSON.stringify(row)) },
  }
}
export interface EmployeeWorkflowTrialSnapshot {
  phase: 'idle' | 'preparing' | 'prepared' | 'starting' | 'reading' | 'unknown' | 'receipt'
  fields: Record<string, string>
  environmentId: string
  modelCallLimit: string
  connected: boolean
  requestId: string | null
  startMayHaveBeenSent: boolean
  pending: { action: EmployeeWorkflowTrialAction; deadlineAt: number; waiting: boolean } | null
  result: Record<string, unknown> | null
  message: string
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/
const SHA = /^[a-f0-9]{64}$/
export const EMPLOYEE_TRIAL_ACK_TIMEOUT_MS = 30_000

export function employeeWorkflowTrialContextKey(context: EmployeeWorkflowTrialContext): string {
  return JSON.stringify([context.projectId, context.workflowId, context.workflowVersion, context.manifestSha256,
    context.workflowStatus, context.saved, context.hasEmployeeBinding, context.intakeFields])
}
export function employeeWorkflowTrialEligibility(context: EmployeeWorkflowTrialContext): string {
  if (!context.saved) return '请先保存整条业务流，再创建测试单。'
  if (context.workflowStatus === 'draft') return '请先使用现有“提交测试”流程；此处不会自动发布或修改草稿状态。'
  if (!['testing', 'published'].includes(context.workflowStatus)) return '仅测试中或已发布的业务流可以创建测试单。'
  if (!context.hasEmployeeBinding) return '这条业务流没有员工绑定，请使用现有流程测试入口。'
  if (!ID.test(context.projectId) || !ID.test(context.workflowId) || !ID.test(context.workflowVersion) || !SHA.test(context.manifestSha256)) {
    return '尚未取得准确的项目、保存版本和文件指纹，请刷新已保存的业务流。'
  }
  return ''
}
export function employeeWorkflowTrialDraftError(context: EmployeeWorkflowTrialContext, state: EmployeeWorkflowTrialSnapshot): string {
  const eligibility = employeeWorkflowTrialEligibility(context)
  if (eligibility) return eligibility
  const missing = context.intakeFields.filter(field => field.required && !(state.fields[field.id] ?? '').trim())
  if (missing.length) return `请填写：${missing.map(field => field.label).join('、')}`
  if (!ID.test(state.environmentId)) return '请填写明确的环境 ID（字母、数字、点、横线或下划线）。'
  const limit = Number(state.modelCallLimit)
  if (!/^\d+$/.test(state.modelCallLimit) || !Number.isSafeInteger(limit) || limit <= 0) return '模型调用上限须为正整数。'
  return ''
}
function requestUuid(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  if (typeof globalThis.crypto?.getRandomValues !== 'function') throw new Error('request_identity_unavailable')
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/** Ephemeral request presentation only. Orders and budgets remain server facts. */
export class EmployeeWorkflowTrialController {
  private state: EmployeeWorkflowTrialSnapshot
  private frozen: Record<string, unknown> | null = null
  private listeners = new Set<() => void>()
  readonly context: EmployeeWorkflowTrialContext
  private readonly latestContext: EmployeeWorkflowTrialContext
  constructor(context: EmployeeWorkflowTrialContext, private send: EmployeeWorkflowTrialSend,
    connected: boolean, private options: { now?: () => number; requestId?: () => string; recovery?: EmployeeTrialRecoveryStore } = {}) {
    this.context = { ...context, intakeFields: context.intakeFields.map(field => ({ ...field })) }
    this.latestContext = { ...this.context }
    this.state = { phase: 'idle', fields: {}, environmentId: '', modelCallLimit: '', connected,
      requestId: null, startMayHaveBeenSent: false, pending: null, result: null, message: '' }
    if (options.recovery) {
      try {
        const row = options.recovery.load()
        if (row) {
          this.context.workflowVersion = row.workflowVersion
          this.context.manifestSha256 = row.manifestSha256
          this.state = { ...this.state, phase: 'unknown', requestId: row.requestId, startMayHaveBeenSent: true,
            message: '已恢复原请求。请先查询原测试单；不会自动再次创建。' }
        }
      } catch {
        this.state = { ...this.state, phase: 'unknown', startMayHaveBeenSent: true,
          message: '无法读取原请求恢复记录。已阻止创建，请保留当前页面并核查记录。' }
      }
    }
  }
  getSnapshot = (): EmployeeWorkflowTrialSnapshot => this.state
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
  private change(patch: Partial<EmployeeWorkflowTrialSnapshot>): void {
    this.state = { ...this.state, ...patch }
    this.listeners.forEach(listener => listener())
  }
  setConnected(connected: boolean): void {
    if (this.state.connected === connected) return
    if (!connected && this.state.pending?.waiting) {
      this.change({ connected, phase: 'unknown', pending: { ...this.state.pending, waiting: false },
        message: '连接已断开，原请求结果未知。恢复连接后请查询原测试单。' })
    } else this.change({ connected })
  }
  edit(patch: { fields?: Record<string, string>; environmentId?: string; modelCallLimit?: string }): boolean {
    if (this.state.startMayHaveBeenSent) return false
    this.frozen = null
    this.change({ ...patch, ...(patch.fields ? { fields: { ...patch.fields } } : {}),
      phase: 'idle', requestId: null, pending: null, result: null, message: '' })
    return true
  }
  /** Explicitly start another test only after the original order was identified. */
  newTest(): boolean {
    if (this.state.phase !== 'receipt' || typeof this.state.result?.order_id !== 'string' || !this.state.requestId) return false
    try { this.options.recovery?.retire?.({requestId:this.state.requestId,
      workflowVersion:this.context.workflowVersion, manifestSha256:this.context.manifestSha256}) }
    catch { this.change({message:'无法保存原请求核对记录，未开启新测试。'}); return false }
    Object.assign(this.context, this.latestContext)
    this.frozen = null
    this.change({phase:'idle', fields:{}, environmentId:'', modelCallLimit:'', requestId:null,
      startMayHaveBeenSent:false, pending:null, result:null, message:'原单已核对。新测试需要重新检查条件并明确创建。'})
    return true
  }
  canPrepare(): boolean {
    return this.state.connected && !this.state.startMayHaveBeenSent && !this.state.pending?.waiting
      && this.state.phase === 'idle' && !employeeWorkflowTrialDraftError(this.context, this.state)
  }
  canStart(): boolean { return this.state.connected && this.state.phase === 'prepared' && !this.state.startMayHaveBeenSent }
  canRead(): boolean { return this.state.connected && !!this.state.requestId && !this.state.pending?.waiting }
  prepare(): boolean {
    if (!this.canPrepare()) return false
    let id: string
    try { id = this.state.requestId ?? (this.options.requestId ?? requestUuid)() } catch {
      this.change({ message: '无法生成可靠的请求编号，尚未发送。' }); return false
    }
    if (!UUID.test(id)) { this.change({ message: '请求编号无效，尚未发送。' }); return false }
    this.frozen = { request_id: id, project_id: this.context.projectId, workflow_id: this.context.workflowId,
      workflow_version: this.context.workflowVersion, manifest_sha256: this.context.manifestSha256,
      environment_id: this.state.environmentId, fields: Object.fromEntries(this.context.intakeFields.map(field => [field.id, this.state.fields[field.id] ?? ''])),
      budget_limits: { model_call: Number(this.state.modelCallLimit) } }
    this.change({ requestId: id, result: null, message: '' })
    return this.dispatch(EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.prepare, this.frozen)
  }
  start(): boolean {
    if (!this.canStart() || !this.frozen) return false
    return this.dispatch(EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.start, this.frozen)
  }
  read(): boolean {
    if (!this.canRead()) return false
    return this.dispatch(EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.read,
      { project_id: this.context.projectId, request_id: this.state.requestId })
  }
  private dispatch(action: EmployeeWorkflowTrialAction, payload: Record<string, unknown>): boolean {
    const starts = action === EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.start
    const phase = starts ? 'starting' : action === EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.prepare ? 'preparing' : 'reading'
    this.change({ phase, message: '请求已发出，等待回执。',
      startMayHaveBeenSent: this.state.startMayHaveBeenSent || starts,
      pending: { action, deadlineAt: (this.options.now ?? Date.now)() + EMPLOYEE_TRIAL_ACK_TIMEOUT_MS, waiting: true } })
    // Persist BEFORE start can cross the transport boundary. A failed write never sends.
    if (starts && this.options.recovery) {
      try { this.options.recovery.save({ requestId: this.state.requestId!,
        workflowVersion: this.context.workflowVersion, manifestSha256: this.context.manifestSha256 }) }
      catch {
        this.change({ phase: 'unknown', pending: null,
          message: '无法保存请求恢复记录，创建请求未发出。请核查存储后查询原编号。' })
        return false
      }
    }
    let disposition: 'sent' | 'send-failed'
    try { disposition = this.send(action, JSON.parse(JSON.stringify(payload)) as Record<string, unknown>) } catch {
      this.change({ phase: 'unknown', pending: { ...this.state.pending!, waiting: false },
        message: '发送回执未知，请查询原测试单，不能自动重发。' })
      return false
    }
    if (disposition === 'sent') return true
    // The transport explicitly confirms this request was not sent/queued.
    this.change({ phase: starts ? 'prepared' : action === EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.prepare ? 'idle' : 'unknown',
      startMayHaveBeenSent: starts ? false : this.state.startMayHaveBeenSent, pending: null,
      message: '请求未发出，请检查连接。未自动重试。' })
    return false
  }
  timeout(now = (this.options.now ?? Date.now)()): boolean {
    if (!this.state.pending?.waiting || now < this.state.pending.deadlineAt) return false
    this.change({ phase: 'unknown', pending: { ...this.state.pending, waiting: false },
      message: '30 秒内未收到回执，结果未知。请查询原测试单，不要重复创建。' })
    return true
  }
  receive(ack: EmployeeWorkflowTrialAck): boolean {
    const pending = this.state.pending
    if (!pending || ack.action !== pending.action || ack.request_id !== this.state.requestId
      || ack.project_id !== this.context.projectId
      || (ack.workflow_id !== undefined && ack.workflow_id !== this.context.workflowId)
      || (ack.workflow_version !== undefined && ack.workflow_version !== this.context.workflowVersion)
      || (ack.manifest_sha256 !== undefined && ack.manifest_sha256 !== this.context.manifestSha256)) return false
    const prepare = ack.action === EMPLOYEE_WORKFLOW_TRIAL_ACTIONS.prepare
    if (!ack.ok) {
      this.change({ phase: prepare ? 'idle' : 'unknown', pending: null,
        message: typeof ack.error === 'string' && ack.error ? ack.error : '本次请求未取得可确认结果，请查询原测试单。' })
      return true
    }
    const result = ack.result
    if (!result || typeof result.state !== 'string') {
      this.change({ phase: 'unknown', pending: null, message: '回执缺少可核对的状态，请查询原测试单。' }); return true
    }
    if ((result.project_id !== undefined && result.project_id !== this.context.projectId)
      || (result.workflow_id !== undefined && result.workflow_id !== this.context.workflowId)
      || (result.workflow_version !== undefined && result.workflow_version !== this.context.workflowVersion)
      || (result.manifest_sha256 !== undefined && result.manifest_sha256 !== this.context.manifestSha256)) return false
    if (prepare && result.state === 'ready_for_test_order' && typeof result.request_digest === 'string' && SHA.test(result.request_digest)) {
      this.change({ phase: 'prepared', pending: null, result: { ...result }, message: '当前测试条件已检查；创建时仍会重新核对权限、预算和员工版本。' })
      return true
    }
    if (prepare) {
      this.change({ phase: 'unknown', pending: null, message: '未收到明确的准备回执，尚不能创建测试单。' }); return true
    }
    const orderKnown = typeof result.order_id === 'string' && !!result.order_id && result.project_id === this.context.projectId
    const safeNotFound = result.state === 'not_found' && !this.state.startMayHaveBeenSent
    this.change({ phase: orderKnown ? 'receipt' : safeNotFound ? 'idle' : 'unknown', pending: null, result: { ...result },
      message: orderKnown ? '已读到原测试单。启动记录不代表执行成功，请在原单查看进展。'
        : safeNotFound ? '当前未查到测试单，可用原编号重新检查条件。'
          : '尚不能确认原请求结果；保留原编号继续查询，不会自动重开单。' })
    return true
  }
}
