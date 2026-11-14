import type { FlowDraftReference } from './flowComponentCandidate'

export interface ComponentAdoptionState {
  phase: 'idle' | 'inspecting' | 'ready' | 'sending' | 'unknown' | 'done' | 'error'
  message: string; requestId?: string; workflowName?: string
}
type Materialized = { package_id: string; version: string; draft_revision: string }
export type DraftSendResult = boolean | { guaranteedNotSent: true; reason?: string }

/** Original socket only, no queue/retry. An unknown write can only be looked up with its original intent. */
export class FlowComponentAdoptionController {
  state: ComponentAdoptionState = { phase: 'idle', message: '' }
  private timer: ReturnType<typeof setTimeout> | null = null
  private pending: Record<string, unknown> | null = null
  private target: Materialized | null = null
  private source: FlowDraftReference
  private everUnknownWrite = false
  constructor(private readonly options: {
    candidateMessageId: string; candidateId: string; source: FlowDraftReference
    send: (payload: Record<string, unknown>) => DraftSendResult; nextId: () => string
    changed: (state: ComponentAdoptionState) => void
    schedule?: (callback: () => void) => ReturnType<typeof setTimeout>
    cancel?: (timer: ReturnType<typeof setTimeout>) => void
  }) { this.source = JSON.parse(JSON.stringify(options.source)) }
  private emit(phase: ComponentAdoptionState['phase'], message: string, extra = {}) {
    this.state = { ...this.state, phase, message, ...extra }; this.options.changed({ ...this.state })
  }
  private clear() { if (this.timer !== null) (this.options.cancel ?? clearTimeout)(this.timer); this.timer = null }
  private dispatch(payload: Record<string, unknown>) {
    this.clear(); this.pending = JSON.parse(JSON.stringify(payload))
    let sent: DraftSendResult
    try { sent = this.options.send(JSON.parse(JSON.stringify(this.pending))) }
    catch {
      const write = payload.type === 'workflow_adopt_component_draft'
      if (write) this.everUnknownWrite = true
      this.emit(write ? 'unknown' : 'error', write ? '发送结果待核对，请只查询本次结果。' : '读取未完成，请检查连接。')
      return
    }
    if (sent !== true) {
      const knownNotSent = typeof sent === 'object' && sent.guaranteedNotSent === true
      if (payload.type === 'workflow_adopt_component_draft' && (this.everUnknownWrite || !knownNotSent)) {
        this.everUnknownWrite = true; this.emit('unknown', '发送结果待核对，请只查询本次结果。')
      } else this.emit('error', typeof sent === 'object' ? sent.reason ?? '本次没有发出，请核对后继续。' : '尚未发出，请检查连接。')
      return
    }
    this.timer = (this.options.schedule ?? (fn => setTimeout(fn, 30000)))(() => {
      this.timer = null
      if (this.pending?.type === 'workflow_adopt_component_draft') this.everUnknownWrite = true
      this.emit('unknown', '发出去了，结果待核对。')
    })
  }
  private common(type: string) {
    return { type, project_id: this.source.project_id, candidate_message_id: this.options.candidateMessageId,
      candidate_id: this.options.candidateId, source_reference: JSON.parse(JSON.stringify(this.source)), request_id: this.options.nextId() }
  }
  inspect() {
    if (!['idle', 'error'].includes(this.state.phase)) return
    this.emit('inspecting', '正在核对原流程草稿…')
    this.dispatch(this.common('workflow_component_candidate_target'))
  }
  confirm() {
    if (this.state.phase !== 'ready' || !this.target) return
    const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(this.target.version)
    if (!match || Number(match[3]) >= 2147483647) { this.emit('error', '请先在搭建台设置新草稿版本。'); return }
    const payload = { ...this.common('workflow_adopt_component_draft'), expected_materialized: { ...this.target },
      new_version: `${match[1]}.${match[2]}.${Number(match[3]) + 1}` }
    this.emit('sending', '已发出，正在采用为草稿…', { requestId: payload.request_id }); this.dispatch(payload)
  }
  lookup() {
    if (this.state.phase !== 'unknown' || !this.pending) return
    const original = { ...this.pending }
    if (original.type === 'workflow_adopt_component_draft') original.check_only = true
    this.emit(original.type === 'workflow_adopt_component_draft' ? 'sending' : 'inspecting', '正在核对本次结果…')
    this.dispatch(original)
  }
  accept(reply: Record<string, unknown>) {
    if (!this.pending || reply.request_id !== this.pending.request_id || reply.project_id !== this.source.project_id
      || reply.action !== this.pending.type) return false
    this.clear()
    if (reply.ok !== true) {
      const code = String(reply.error ?? reply.outcome ?? '')
      const messages: Record<string, string> = { open_original_draft_first: '请先在搭建台打开原流程，再采用这个组件草稿。',
        draft_revision_changed: '原草稿已更新，请重新核对后采用。', source_node_changed: '这一步已改过，请让安安基于新内容重新起草。',
        owner_required: '当前账户不能采用这个草稿。', draft_required: '请先将目标流程另存为草稿。',
        not_recorded: '尚未找到本次采用记录，请先核对草稿。' }
      const write = this.pending.type === 'workflow_adopt_component_draft'
      const unknown = write && (this.everUnknownWrite || this.pending.check_only === true
        || reply.effects_produced !== false || reply.external_effects_produced !== false)
      if (unknown) this.everUnknownWrite = true
      this.emit(unknown ? 'unknown' : 'error', messages[code] ?? '未能确认采用，请核对原草稿。')
      return true
    }
    if (this.pending.type === 'workflow_component_candidate_target') {
      const value = reply.materialized as Materialized | undefined
      const source = reply.source_reference as Record<string, unknown> | undefined
      if (!value || typeof value.package_id !== 'string' || typeof value.version !== 'string'
        || !/^[0-9a-f]{64}$/.test(value.draft_revision) || reply.candidate_id !== this.options.candidateId
        || !source || Object.entries(this.source).some(([key, value]) => source[key] !== value)
        || reply.effects_produced !== false || reply.external_effects_produced !== false) {
        this.emit('error', '原草稿修订信息不完整。'); return true
      }
      this.target = { ...value }
      this.emit('ready', '采用后只更新原流程草稿，仍需测试与准入。', { workflowName: String(reply.workflow_name ?? value.package_id) })
    } else if (reply.outcome === 'adopted' && reply.candidate_id === this.options.candidateId
      && typeof reply.currently_attached === 'boolean' && typeof reply.effects_produced === 'boolean'
      && reply.external_effects_produced === false) {
      this.emit('done', reply.currently_attached === false ? '本次已采用；原步骤后来有修改，请在搭建台查看。' : '已接回原步骤，等待编辑与测试。')
    } else {
      this.everUnknownWrite = true; this.emit('unknown', '采用回执不完整，请只核对本次结果。')
    }
    return true
  }
  dispose() { this.clear() }
}
