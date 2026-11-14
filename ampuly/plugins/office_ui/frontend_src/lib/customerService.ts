// 客服验收（4.8 customer service）：the owner's suite lifecycle as the console
// last saw it. The server owns every fact; this only keeps the latest ack for a
// project so the card can show it and carry the suite id to the next action.
export type CustomerServiceDetail = Record<string, unknown>

export interface CustomerServiceState {
  projectId: string
  suiteId: string
  detail: CustomerServiceDetail | null
  lastAction: string
  lastOk: boolean | null
  error: string
}

export const EMPTY_CUSTOMER_SERVICE: CustomerServiceState = {
  projectId: '',
  suiteId: '',
  detail: null,
  lastAction: '',
  lastOk: null,
  error: '',
}

export const CUSTOMER_SERVICE_ACTIONS = [
  'customer_service_prepare',
  'customer_service_detail',
  'customer_service_test_start',
  'customer_service_review',
  'customer_service_admit',
  'customer_service_revoke',
  'customer_service_channel_select',
  'customer_service_channel_replace',
  'customer_service_channel_pause',
] as const

export type CustomerServiceAction = (typeof CUSTOMER_SERVICE_ACTIONS)[number]

export function customerServiceSent(
  prev: CustomerServiceState, projectId: string, action: string, sent: boolean,
): CustomerServiceState {
  if (prev.projectId !== projectId) prev = { ...EMPTY_CUSTOMER_SERVICE, projectId }
  if (!sent) return { ...prev, projectId, lastAction: action, lastOk: false, error: 'send-failed' }
  return { ...prev, projectId, lastAction: action, lastOk: null, error: '' }
}

export function customerServiceAck(
  prev: CustomerServiceState, payload: Record<string, unknown>,
): CustomerServiceState {
  const projectId = String(payload.project_id ?? prev.projectId ?? '')
  if (prev.projectId && projectId !== prev.projectId) return prev
  const action = String(payload.action ?? '')
  const ok = payload.ok === true
  const error = String(payload.error ?? '')
  const result = payload.customer_service
  const detail = result && typeof result === 'object' && !Array.isArray(result)
    ? result as CustomerServiceDetail : null
  const suiteId = detail && typeof detail.suite_id === 'string' ? detail.suite_id : prev.suiteId
  return {
    projectId,
    suiteId,
    // `detail` is the one answer that replaces the panel; other acks keep it.
    detail: action === 'customer_service_detail' && ok ? detail
      : action === 'customer_service_prepare' && ok ? null : prev.detail,
    lastAction: action,
    lastOk: ok,
    error: ok ? '' : (error || 'customer_service_operation_failed'),
  }
}


export interface CustomerServiceSetup {
  packageId: string; version: string; fingerprint: string
  channel: 'console' | 'wecom_cs'; accountId: string
  instructions: string; validUntil: string; maxClarifications: number; maxMessages: number
  knowledge: Array<{ question: string; answer: string; title: string; reference: string }>
  cases: Array<{ id: string; message: string; action: 'auto_reply' | 'auto_clarify' | 'handoff'; intent: string }>
}

export function newCustomerServiceSetup(now = Date.now()): CustomerServiceSetup {
  return {
    packageId: '', version: '', fingerprint: '', channel: 'console', accountId: '',
    instructions: '只依据已审核资料回答；未知问题交人工，不承诺订单或售后已经执行。',
    validUntil: new Date(now + 7 * 86400000).toISOString().slice(0, 16),
    maxClarifications: 2, maxMessages: 20,
    knowledge: [{ question: '', answer: '', title: '', reference: '' }],
    cases: [
      { id: 'normal', message: '', action: 'auto_reply', intent: 'product_question' },
      { id: 'clarify', message: '想了解商品，请先问我具体需求', action: 'auto_clarify', intent: 'product_question' },
      { id: 'human', message: '我要人工客服', action: 'handoff', intent: 'human_handoff' },
    ],
  }
}

const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
    .map(([key, item]) => JSON.stringify(key) + ':' + canonical(item)).join(',') + '}'
  return JSON.stringify(value)
}

export async function customerServicePrepareParams(setup: CustomerServiceSetup, now = Date.now()): Promise<Record<string, unknown>> {
  const text = (value: string, name: string, max: number) => {
    const result = value.trim()
    if (!result || result.length > max) throw new Error(`${name}：请填写1–${max}字`)
    return result
  }
  const packageId = text(setup.packageId, '员工包 ID', 128)
  const version = text(setup.version, '员工包版本', 128)
  if (!/^[a-f0-9]{64}$/.test(setup.fingerprint.trim())) throw new Error('请从已发布员工包复制64位内容指纹')
  const validUntil = Date.parse(setup.validUntil + 'Z') / 1000
  if (!Number.isInteger(validUntil) || validUntil <= now / 1000 || validUntil > now / 1000 + 30 * 86400) throw new Error('有效期须在未来30天内（UTC）')
  if (!Number.isInteger(setup.maxClarifications) || setup.maxClarifications < 0 || setup.maxClarifications > 8
      || !Number.isInteger(setup.maxMessages) || setup.maxMessages < 1 || setup.maxMessages > 100000) throw new Error('请检查澄清轮数与自动消息上限')
  if (setup.knowledge.length < 1 || setup.knowledge.length > 200) throw new Error('需要1–200条知识')
  const day = new Date(now + 8 * 3600000).toISOString().slice(0, 10)
  const records = await Promise.all(setup.knowledge.map(async (row, index) => {
    const question = text(row.question, '知识问题', 500), answer = text(row.answer, '审核答案', 2000)
    const title = text(row.title, '来源标题', 128), reference = text(row.reference, '来源位置', 500)
    const record = { kind: 'faq', id: `faq-${index + 1}`, version: 'v1', title, status: 'approved', channels: [setup.channel],
      valid_from: null, valid_until: null, source: { title, reference, content: answer }, questions: [question], keywords: [], answer }
    const hash = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(record)))
    return { ...record, review: { by: 'owner-supplied', at: day,
      record_hash: Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('') } }
  }))
  if (setup.cases.length < 3 || setup.cases.length > 32 || new Set(setup.cases.map(row => row.id)).size !== setup.cases.length) throw new Error('需要3–32个不同ID的验收用例')
  const cases = setup.cases.map((row, index) => ({ case_id: text(row.id, '用例ID', 128),
    turns: [{ id: `input-${index + 1}`, role: 'customer', text: text(row.message, '测试消息', 2000) }],
    expected_action: row.action, expected_intents: [row.intent],
    ...(row.action === 'handoff' ? { required_reason_codes: ['autonomy_customer_requested_human'] } : {}),
  }))
  return { package: { package_id: packageId, version, fingerprint: setup.fingerprint.trim() },
    knowledge: { schema_version: 'ampuly.cs.knowledge/1', knowledge_base_id: 'owner-faq', name: '店铺审核问答',
      tenant_id: 'host-derived', shop_id: 'host-derived', version: 'v1', mode: 'service_snapshot', records },
    instructions: text(setup.instructions, '话术规则', 4000), channel: setup.channel,
    account_id: setup.channel === 'console' ? 'console' : text(setup.accountId, '企业微信客服账号ID', 128),
    valid_until: validUntil, allowed_intents: ['product_question'], max_clarification_turns: setup.maxClarifications,
    authorized_reply_actions: ['auto_reply', 'auto_clarify'], max_auto_messages: setup.maxMessages, cases }
}

export function customerServiceCaseRows(detail: CustomerServiceDetail | null) {
  const results = (detail?.case_results ?? {}) as Record<string, Record<string, unknown>>
  const reviews = (detail?.reviews ?? {}) as Record<string, Record<string, unknown>>
  const cases = Array.isArray(detail?.cases) ? detail.cases as Array<Record<string, unknown>> : []
  return cases.map(row => {
    const id = String(row.case_id), result = results[id] ?? {}
    const candidate = result.candidate as Record<string, unknown> | undefined
    const draft = candidate?.draft as Record<string, unknown> | undefined
    const evaluation = result.evaluation as Record<string, unknown> | undefined
    return { id, turns: row.turns as Array<{ text: string }>, status: String(result.status ?? 'pending'),
      reply: String(draft?.reply ?? ''), sources: Array.isArray(candidate?.sources) ? candidate.sources as Array<Record<string, unknown>> : [],
      handoff: candidate?.handoff as Record<string, unknown> | undefined,
      hash: typeof evaluation?.candidate_sha256 === 'string' ? evaluation.candidate_sha256 : '', review: reviews[id],
      errors: evaluation?.reason_codes ?? candidate?.validation_errors ?? [] }
  })
}
