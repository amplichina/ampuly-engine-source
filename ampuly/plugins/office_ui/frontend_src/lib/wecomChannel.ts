// 企业微信（微信客服）客户通道 on 配置 · 服务与连接 · 渠道连接 (policy「按推荐做」, W3b). The server's three
// owner-only actions -- wecom_channel_status / wecom_channel_set / wecom_kf_account_set -- answer with one status
// (wecom_channel.channel_status): the honesty word from real evidence only, the owner's switch (default off), whether
// the relay's address and shared token are set (never their values; the address as its host only), the 客服账号 bound
// to each project, the knowledge file's place, and how the AI 客服 answers -- 「JEV 判断 + 现成答案」 or the
// text model -- with the knowledge file's counts (`brain`). Pure: reading those answers, building the requests, the words of an
// order's source and the read-only conversation an order from 企业微信客服 carries (order_timeline's
// `wecom_conversation`). WecomChannelCard draws them; the server checks every write again and has the last word.

export const WECOM_ACTIONS = ['wecom_channel_status', 'wecom_channel_set', 'wecom_kf_account_set'] as const
export type WecomAction = typeof WECOM_ACTIONS[number]

export interface WecomAccount {
  open_kfid: string
  project_id: string
  workflow_id: string
  account_name: string
  owner_only_fields: string[]
  welcome_text: string
  servicer_userid: string
  enabled: boolean
  updated_at: string
  updated_by: string
}

export interface WecomChannelStatus {
  /** unconfigured / needs_authorization / unverified / usable / broken / unknown (project_connections' words). */
  status: string
  status_word: string
  evidence: string[]
  /** The business database is at the version the channel needs. */
  ready: boolean
  switch_on: boolean
  muted: boolean
  configured: { url: boolean; token: boolean }
  /** The relay's host only (never the full address, never the token). */
  relay_host: string
  connected: boolean
  /** This project's 客服账号 (the server names them apart as project_accounts). */
  accounts: WecomAccount[]
  limits: { perConversation: number; perAccount: number }
  knowledge_file: string
  /** How the AI 客服 answers this project's customers (added ); null from a server without it. */
  brain: WecomBrain | null
}

/** 「JEV 判断 + 现成答案」 (policy): JEV judges and the owner's prepared answers are sent when JEV's key is
 * set; otherwise the text model answers. The counts are this project's knowledge file as the server reads it. */
export interface WecomBrain {
  jevConfigured: boolean
  mode: 'jev' | 'text_model'
  knowledge: {
    /** Prepared answers that can be sent as they are. */
    entries: number
    /** Answers that still hold an unfilled 【…】: never sent. */
    unfilled: number
    /** 需求提问 lines: the question the owner wrote for a start field. */
    fieldPrompts: number
    /** Too long for one message, past the 60 the file may hold, or cut at the reading limit: not sent either. */
    skipped: number
    /** The questions of the unfilled answers (the owner's own words, shortened; at most 10). */
    unfilledQuestions: string[]
  }
}

export interface WecomChannelState {
  /** The project the status was read for: its 客服账号 are that project's (another project's are never shown). */
  projectId: string
  status: WecomChannelStatus | null
  /** The last refusal, in the owner's words; '' when the last answer was ok. */
  error: string
  /** The write waiting for its answer: '' none, 'switch', or 'account'. */
  busy: '' | 'switch' | 'account'
}

export const EMPTY_WECOM_CHANNEL: WecomChannelState = { projectId: '', status: null, error: '', busy: '' }

/** The state as the card of ``projectId`` may show it: another project's reading is not shown at all. */
export function wecomStateFor(state: WecomChannelState, projectId: string): WecomChannelState {
  return state.projectId === projectId ? state : { ...EMPTY_WECOM_CHANNEL, projectId }
}

/** A write sent (busy until its answer) or refused on the spot by the socket (said at once, nothing waits). */
export function wecomSent(prev: WecomChannelState, projectId: string, busy: 'switch' | 'account', disposition: string): WecomChannelState {
  const base = wecomStateFor(prev, projectId)
  return disposition === 'sent'
    ? { ...base, busy, error: '' }
    : { ...base, busy: '', error: '现在没连上控制台，这次什么都没有改；连上后再点一次。' }
}

const text = (value: unknown) => (typeof value === 'string' ? value : '')
const record = (value: unknown): Record<string, unknown> =>
  (value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {})

function accountFrom(raw: unknown): WecomAccount | null {
  const item = record(raw)
  const id = text(item.open_kfid).trim()
  if (!id) return null
  return {
    open_kfid: id,
    project_id: text(item.project_id),
    workflow_id: text(item.workflow_id),
    account_name: text(item.account_name),
    owner_only_fields: Array.isArray(item.owner_only_fields) ? item.owner_only_fields.map(text).filter(Boolean) : [],
    welcome_text: text(item.welcome_text),
    servicer_userid: text(item.servicer_userid),
    enabled: item.enabled === true,
    updated_at: text(item.updated_at),
    updated_by: text(item.updated_by),
  }
}

const count = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0)

/** The ``brain`` a status carries, or null when it carries none. */
export function wecomBrainFrom(raw: unknown): WecomBrain | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const item = raw as Record<string, unknown>
  const knowledge = record(item.knowledge)
  const jevConfigured = item.jev_configured === true
  return {
    jevConfigured,
    mode: item.mode === 'jev' && jevConfigured ? 'jev' : 'text_model',
    knowledge: {
      entries: count(knowledge.entries),
      unfilled: count(knowledge.unfilled),
      fieldPrompts: count(knowledge.field_prompts),
      skipped: count(knowledge.too_long) + count(knowledge.over_cap) + count(knowledge.cut),
      unfilledQuestions: (Array.isArray(knowledge.unfilled_questions) ? knowledge.unfilled_questions : [])
        .map(text).filter(Boolean).slice(0, 10),
    },
  }
}

/** The status an ack carries (`wecom_channel`), or null when it carries none. */
export function wecomStatusFrom(raw: unknown): WecomChannelStatus | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const item = raw as Record<string, unknown>
  const status = text(item.status)
  if (!status) return null
  const configured = record(item.configured)
  const limits = record(item.limits)
  const accounts = (Array.isArray(item.project_accounts) ? item.project_accounts : [])
    .map(accountFrom).filter((row): row is WecomAccount => row !== null)
  return {
    status,
    status_word: text(item.status_word),
    evidence: Array.isArray(item.evidence) ? item.evidence.map(text).filter(Boolean) : [],
    ready: item.ready === true,
    switch_on: item.switch_on === true,
    muted: item.muted === true,
    configured: { url: configured.url === true, token: configured.token === true },
    relay_host: text(item.relay_host),
    connected: item.connected === true,
    accounts,
    limits: {
      perConversation: Number(limits.ai_turns_per_conversation_per_day ?? 0) || 0,
      perAccount: Number(limits.ai_turns_per_account_per_day ?? 0) || 0,
    },
    knowledge_file: text(item.knowledge_file),
    brain: wecomBrainFrom(item.brain),
  }
}

/** A refusal in the owner's words (the server's own words are already Chinese; the dispatcher's codes are not). */
export function wecomErrorWords(error: string): string {
  const clean = String(error ?? '').trim()
  if (clean === 'owner_required') return '只有老板能看和改企业微信通道。'
  if (!clean || !/[一-鿿]/.test(clean)) return '企业微信通道这一步没做成，请稍后再试。'
  return clean
}

/** One answer of the three actions, folded into the card's state. Any other ack leaves it as it is. */
export function wecomChannelAck(prev: WecomChannelState, payload: Record<string, unknown>): WecomChannelState {
  if (!(WECOM_ACTIONS as readonly string[]).includes(text(payload.action))) return prev
  const projectId = text(payload.project_id) || prev.projectId
  const base = projectId === prev.projectId ? prev : { ...EMPTY_WECOM_CHANNEL, projectId }
  if (payload.ok === true) {
    return { projectId, status: wecomStatusFrom(payload.wecom_channel) ?? base.status, error: '', busy: '' }
  }
  return { ...base, projectId, error: wecomErrorWords(text(payload.error)), busy: '' }
}

/** The honesty word's tone: only 可用 is green; 失效 and 结果未知 ask for a look. */
export function wecomTone(status: string): 'ok' | 'warn' | '' {
  if (status === 'usable') return 'ok'
  if (status === 'broken' || status === 'unknown') return 'warn'
  return ''
}

// ── binding a 客服账号 ──

export interface WecomFlowChoice { id: string; name: string; fields: Array<{ id: string; label: string }> }

type FlowLike = {
  id: string; name?: string; project_id?: string; status?: string
  intake_fields?: ReadonlyArray<{ id: string; label?: string }>
}

/** The flows a 客服账号 of this project may start orders on: the project's own and the shared ones, not retired. The
 *  server checks the same (``_usable``) and refuses anything else. */
export function flowChoices(workflows: ReadonlyArray<FlowLike> | null | undefined, projectId: string): WecomFlowChoice[] {
  return (workflows ?? [])
    .filter(flow => flow.id && flow.status !== 'retired' && (!flow.project_id || flow.project_id === projectId))
    .map(flow => ({
      id: flow.id,
      name: String(flow.name ?? '').trim() || flow.id,
      fields: (flow.intake_fields ?? []).map(field => ({ id: field.id, label: String(field.label ?? '').trim() || field.id })),
    }))
}

export interface WecomAccountDraft {
  open_kfid: string
  account_name: string
  workflow_id: string
  owner_only_fields: string[]
  welcome_text: string
  servicer_userid: string
  enabled: boolean
}

/** The form's values for an account (a new one: empty, off). */
export function draftOf(account?: WecomAccount | null, flows: readonly WecomFlowChoice[] = []): WecomAccountDraft {
  return {
    open_kfid: account?.open_kfid ?? '',
    account_name: account?.account_name ?? '',
    workflow_id: account?.workflow_id ?? (flows[0]?.id ?? ''),
    owner_only_fields: [...(account?.owner_only_fields ?? [])],
    welcome_text: account?.welcome_text ?? '',
    servicer_userid: account?.servicer_userid ?? '',
    enabled: account?.enabled === true,
  }
}

/** What the form cannot send yet, in the owner's words; '' when it can. The server checks everything again. */
export function draftProblem(draft: WecomAccountDraft, flows: readonly WecomFlowChoice[]): string {
  if (!draft.open_kfid.trim()) return '要填客服账号 ID（在企业微信管理后台「微信客服」里复制）。'
  if (!/^[\x21-\x7e]{1,128}$/.test(draft.open_kfid.trim())) return '客服账号 ID 不像企业微信给的编号（只有英文字母、数字和符号，没有空格）。'
  if (!flows.some(flow => flow.id === draft.workflow_id)) return '要选这个客服账号接哪条业务流。'
  return ''
}

/** The wecom_kf_account_set request of a draft (the project goes with it as everywhere). Owner-only fields that are
 *  not the chosen flow's start fields are dropped here: a flow changed in the form takes its own. */
export function accountRequest(draft: WecomAccountDraft, flows: readonly WecomFlowChoice[] = []): Record<string, unknown> {
  const flow = flows.find(item => item.id === draft.workflow_id)
  const known = flow ? new Set(flow.fields.map(field => field.id)) : null
  return {
    open_kfid: draft.open_kfid.trim(),
    account_name: draft.account_name.trim(),
    workflow_id: draft.workflow_id,
    owner_only_fields: draft.owner_only_fields.filter(id => !known || known.has(id)),
    welcome_text: draft.welcome_text.trim(),
    servicer_userid: draft.servicer_userid.trim(),
    enabled: draft.enabled === true,
  }
}

/** The owner-only fields of an account by their labels in the flow (an id the flow no longer has is said as it is). */
export function ownerOnlyLabels(account: Pick<WecomAccount, 'owner_only_fields' | 'workflow_id'>, flows: readonly WecomFlowChoice[]): string[] {
  const fields = flows.find(flow => flow.id === account.workflow_id)?.fields ?? []
  return account.owner_only_fields.map(id => fields.find(field => field.id === id)?.label ?? id)
}

// ── an order's source, and its conversation (业务工单) ──

/** How an order came in, in the words the project centre uses (project_connections._SOURCE_WORDS). An order placed in
 *  the console says nothing: that is the ordinary way. */
export const ORDER_SOURCE_WORDS: Readonly<Record<string, readonly [string, string]>> = {
  wecom_kf: ['企业微信客服', 'WeCom customer service'],
  master: ['安安起草', 'Drafted by 安安'],
  intake_flow: ['需求表', 'Intake form'],
}

export function orderSourceWord(source: string | null | undefined, zh: boolean): string {
  const words = ORDER_SOURCE_WORDS[String(source ?? '')]
  return words ? words[zh ? 0 : 1] : ''
}

export interface WecomConversationLine { at: string; from: string; text: string; outcome: string }
export interface WecomConversation { stageWord: string; handoffReason: string; lines: WecomConversationLine[] }

/** The conversation the order's timeline carries (owner only, read only), when the timeline is this order's. */
export function conversationOf(timeline: unknown, orderId: string): WecomConversation | null {
  const item = record(timeline)
  if (!orderId || text(item.order_id) !== orderId) return null
  const raw = item.wecom_conversation
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const conv = raw as Record<string, unknown>
  const lines = (Array.isArray(conv.messages) ? conv.messages : []).flatMap(entry => {
    const row = record(entry)
    const said = text(row.text)
    return said ? [{ at: text(row.at), from: text(row.from) || '系统', text: said, outcome: text(row.outcome) }] : []
  })
  return { stageWord: text(conv.stage_word), handoffReason: text(conv.handoff_reason), lines }
}

/** A conversation's time as the page shows it: 月-日 时:分. */
export function lineTime(at: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(at ?? ''))
  return match ? `${match[2]}-${match[3]} ${match[4]}:${match[5]}` : String(at ?? '')
}
