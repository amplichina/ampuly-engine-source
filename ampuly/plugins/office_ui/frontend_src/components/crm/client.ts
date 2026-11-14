export type CRMKind = 'company' | 'contact' | 'opportunity' | 'activity'
export type CRMRecord = {
  id: string; kind: CRMKind; revision: number; archived: boolean
  fields: Record<string, unknown>; created_at: string; updated_at: string
}
export type CRMReply = { records?: CRMRecord[]; owners?: string[] } & Record<string, unknown>
export type CRMAction = 'crm_list' | 'crm_get' | 'crm_save' | 'crm_archive' | 'crm_export'
  | 'crm_erasure_preview' | 'crm_erase' | 'crm_settings_get' | 'crm_settings_set'
export type CRMRequest = (action: CRMAction, params: Record<string, unknown>) => Promise<CRMReply>
export type CRMSettings = { enabled: boolean; admission: 'synthetic_only' | 'real'; revision: number
  updated_by: string; updated_at: string; real_data_admission: string }

type Envelope = { project_id: string; request_id: string; params: Record<string, unknown> }
// One literal wire type per server action, so each registered action has a
// visible sender (and nothing can be sent that the server does not register).
export function crmMessage(action: CRMAction, envelope: Envelope): Record<string, unknown> {
  switch (action) {
    case 'crm_list': return { type: 'crm_list', ...envelope }
    case 'crm_get': return { type: 'crm_get', ...envelope }
    case 'crm_save': return { type: 'crm_save', ...envelope }
    case 'crm_archive': return { type: 'crm_archive', ...envelope }
    case 'crm_export': return { type: 'crm_export', ...envelope }
    case 'crm_erasure_preview': return { type: 'crm_erasure_preview', ...envelope }
    case 'crm_erase': return { type: 'crm_erase', ...envelope }
    case 'crm_settings_get': return { type: 'crm_settings_get', ...envelope }
    case 'crm_settings_set': return { type: 'crm_settings_set', ...envelope }
    default: throw new Error('crm_request_invalid')
  }
}

// Reuses the console's existing send/ack transport; no second socket/login.
export function crmClient(projectId: string, send: (data: Record<string, unknown>) => void,
  subscribe: (listener: (ack: Record<string, unknown>) => void) => () => void) {
  let closed = false
  const pending = new Map<string, { resolve: (value: CRMReply) => void; reject: (reason: Error) => void; timer: ReturnType<typeof setTimeout> }>()
  const unsubscribe = subscribe((ack) => {
    if (typeof ack.request_id !== 'string') return
    const item = pending.get(ack.request_id)
    if (!item) return
    pending.delete(ack.request_id)
    clearTimeout(item.timer)
    if (ack.ok === true && typeof ack.crm === 'object' && ack.crm !== null) item.resolve(ack.crm as CRMReply)
    else item.reject(new Error(typeof ack.error === 'string' ? ack.error : 'crm_operation_failed'))
  })
  const request: CRMRequest = (action, params) => new Promise((resolve, reject) => {
    if (closed) { reject(new Error('crm_context_closed')); return }
    const request_id = crypto.randomUUID()
    const timer = setTimeout(() => {
      pending.delete(request_id)
      reject(new Error(action === 'crm_list' || action === 'crm_get' || action === 'crm_settings_get' ? 'crm_read_timeout' : 'crm_outcome_unknown'))
    }, 15000)
    pending.set(request_id, { resolve, reject, timer })
    try { send(crmMessage(action, { project_id: projectId, request_id, params })) }
    catch {
      clearTimeout(timer); pending.delete(request_id); reject(new Error('crm_disconnected'))
    }
  })
  return { request, close() {
    closed = true
    unsubscribe()
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('crm_context_closed')) }
    pending.clear()
  } }
}

export const labels: Record<CRMKind, string> = { company: '销售对象公司', contact: '联系人', opportunity: '商机', activity: '跟进活动' }
export function crmErrorMessage(code: string): string {
  const messages: Record<string, string> = {
    crm_project_not_registered: '当前项目尚未登记，请先到项目中心完成登记。',
    crm_project_customer_binding_required: '当前项目未绑定客户或已停止，先核对项目中心的客户归属。',
    crm_customer_forbidden: '当前账号无权访问该客户。', crm_owner_required: '此销售工作台需要经营负责人的权限。',
    crm_revision_conflict: '记录已被其他操作更新。请刷新后核对，再保存。',
    crm_storage_scope_conflict: '项目客户归属与历史销售记录不一致，已停止访问，请由负责人核对迁移。',
    crm_archive_has_active_links: '仍有活动关联记录，请先处理关联内容后再归档。',
    crm_input_invalid: '请核对必填字段、格式与长度。', crm_owner_invalid: '负责人已失效或不在当前授权范围。',
    crm_outcome_unknown: '服务器尚未确认结果，请先刷新核对；重试将保留原请求身份。',
    crm_disconnected: '连接已断开，尚未发送。', crm_context_closed: '项目或账号已切换，请重新打开工作台。',
    crm_storage_migration_required: '旧版销售数据需要显式迁移，系统没有自动修改旧库。请联系负责人；暂勿录入真实联系资料。',
    crm_replay_obsolete: '该请求对应的记录已更新或移除，请刷新核对；不会重放旧字段。',
    crm_confirmation_required: '请核对目标、操作类型和确认语句，并勾选确认。',
    crm_confirmation_expired: '清理确认已过期或目标不匹配，请重新预览。',
    crm_confirmation_stale: '记录或关联内容已变化，请重新预览范围后确认。',
    crm_erasure_storage_mode_unavailable: '当前库的日志模式不支持此清理保证，操作已停止，请联系负责人。',
    crm_governance_not_admitted: '删除与匿名化尚未开放，当前只能使用合成数据。',
    crm_read_timeout: '读取超时，请稍后刷新。',
    crm_not_enabled: '本项目尚未启用销售记录（默认关闭），需经营负责人启用。',
    crm_synthetic_data_required: '当前仅允许合成数据：邮箱和网站须使用 example.com、*.test、*.invalid 等保留域名，电话须使用 555-01xx 虚构号码或 TEST- 前缀。',
    crm_real_data_admission_closed: '真实数据准入未开放，本项目只能使用合成数据。',
    crm_settings_revision_conflict: '设置已被其他操作更新，请刷新后再试。',
    crm_settings_invalid: '销售记录设置文件异常，已停止访问，请联系负责人核对。',
    crm_settings_unavailable: '销售记录设置不可读取，已停止访问。',
    crm_storage_key_unavailable: '销售记录密钥文件缺失或异常，已停止访问，请联系负责人从备份核对。',
    crm_tombstones_unavailable: '资料清理记录不可读取，已停止访问，避免恢复已清除的资料。',
    crm_pipeline_status_invalid: '请选择有效的销售进展。',
  }
  return messages[code] ?? '操作未完成，请刷新核对记录与当前权限。'
}
export function defaults(kind: CRMKind, owner: string): Record<string, unknown> {
  const common = { name: '', notes: '', owner_id: owner }
  switch (kind) {
    case 'company': return { ...common, website: '' }
    case 'contact': return { ...common, company_id: '', email: '', phone: '' }
    case 'opportunity': return { ...common, company_id: '', contact_id: '', pipeline_status: 'lead', amount_minor: 0,
      currency: 'CNY', next_action: '', next_due: '' }
    case 'activity': return { ...common, opportunity_id: '', occurred_at: new Date().toISOString(), source_reference: '' }
    default: return {}
  }
}

export function money(record: CRMRecord): string {
  const amount = record.fields.amount_minor
  const currency = record.fields.currency
  if (typeof amount !== 'number' || typeof currency !== 'string') return '未记录'
  // Amounts are stored in ISO currency minor units (JPY has no decimals).
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(amount / (currency === 'JPY' ? 1 : 100))
}
