// 项目中心 = 一列项目 + 四个页签 + 业务全景 (frontend redesign S3-2B, policy). Pure helpers over what the
// console already holds: the server's judgement of every order (`attention`, S0-A), the four numbers per project
// and per flow (`standing` / `flows` on project_order_overview, S0), the live steps of an order (`current_nodes`,
// S0-B), a flow's stage map (`stages` on workflow_list, S3-2A), a project's business boundary (`profile`) and its
// four connection lanes (project_connections_*, S3-2A). Nothing here is a new rule: the words are the owner's
// prototype's (owner-business-ui.js), the numbers are the server's, and a field the server did not send is read as
// "not known", never as zero.

import { attentionOf, dueWords, rowTitle, statusWord, stepText, type InboxOrderLike } from './ordersInbox'

// ── the four numbers ──────────────────────────────────────────────────────

export const STANDING_KEYS = ['waiting_on_me', 'abnormal_or_overdue', 'in_flight', 'delivered'] as const
export type StandingKey = typeof STANDING_KEYS[number]

/** The four numbers as the server counts them (order_attention.count_standing); `partial` = the waiting and abnormal
 *  numbers are unknown (the project's pauses or budgets could not be read), `truncated` = every number is a floor. */
export interface Standing {
  waiting_on_me: number
  abnormal_or_overdue: number
  in_flight: number
  delivered: number
  truncated: boolean
  partial: boolean
}

/** 「N 单等你处理」 and its three siblings, in the owner's words (the vocabulary table). */
export const STANDING_WORDS: Record<StandingKey, readonly [string, string]> = {
  waiting_on_me: ['等你处理', 'waiting on you'],
  abnormal_or_overdue: ['出错或逾期', 'in trouble or overdue'],
  in_flight: ['在途', 'in flight'],
  delivered: ['已交付', 'delivered'],
}

/** The inbox state each number opens (lib/ordersInbox INBOX_STATES): the list a number leads to is the list it
 *  counted. 在途 is the inbox's whole 进行中 group (`live`), not 「在做 · 别人负责」 alone, which leaves out the customer;
 *  已交付 is `delivered` (closed, not cancelled), not 已完成, which holds the refused orders too. */
export const STANDING_STATES: Record<StandingKey, string> = {
  waiting_on_me: 'mine',
  abnormal_or_overdue: 'risk',
  in_flight: 'live',
  delivered: 'delivered',
}

/** The server's standing, or null when it sent none (an older server, a project not read): the page then says
 *  nothing rather than zero. */
export function standingFrom(value: unknown): Standing | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const read = (key: StandingKey): number | null => {
    const n = raw[key]
    return typeof n === 'number' && Number.isFinite(n) && n >= 0 ? n : null
  }
  const numbers = STANDING_KEYS.map(read)
  if (numbers.some(n => n === null)) return null
  return {
    waiting_on_me: numbers[0] as number,
    abnormal_or_overdue: numbers[1] as number,
    in_flight: numbers[2] as number,
    delivered: numbers[3] as number,
    truncated: raw.truncated === true,
    partial: raw.partial === true,
  }
}

/** What a number's link hands 业务工单 (lib/orderSpread ordersTarget): the state, and the flow when the number is a
 *  flow's. */
export function standingTarget(key: StandingKey, flowId = ''): string {
  return flowId ? `state:${STANDING_STATES[key]};flow:${flowId}` : `state:${STANDING_STATES[key]}`
}

/** A number's text. Unknown (partial) waiting and abnormal numbers say so instead of showing a zero; a truncated
 *  standing says 至少. */
export function standingText(standing: Standing, key: StandingKey, zh: boolean): string {
  if (standing.partial && (key === 'waiting_on_me' || key === 'abnormal_or_overdue')) return zh ? '未知' : 'unknown'
  const n = standing[key]
  return standing.truncated ? (zh ? `至少 ${n}` : `at least ${n}`) : String(n)
}

// ── the profile (本项目的业务边界) ────────────────────────────────────────

export interface ProjectProfile {
  /** 类型: the kind of business, in the owner's words (品牌设计服务). */
  type: string
  /** 从哪里接手. */
  start: string
  /** 安普里负责什么、不负责什么. */
  scope: string
  /** 什么结果算交付完成. */
  end: string
  /** 规划管理者: an employee id; '' = 待配置. Planning responsibility only, never a grant. */
  manager_employee_id: string
  /** The manager's name when the server resolved it (else the page resolves the id through the roster). */
  manager_name: string
  updated_at: string
}

export const EMPTY_PROFILE: ProjectProfile = Object.freeze({ type: '', start: '', scope: '', end: '', manager_employee_id: '', manager_name: '', updated_at: '' })

const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

/** The profile as the server carries it (`profile` on a list_projects row or a project_profile_* reply, S3-2A).
 *  Every field optional on the wire; unknown fields ignored; null when there is nothing profile-shaped at all. */
export function profileFrom(value: unknown): ProjectProfile | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  return {
    type: text(raw.type ?? raw.business_type),
    start: text(raw.start),
    scope: text(raw.scope),
    end: text(raw.end),
    manager_employee_id: text(raw.manager_employee_id ?? raw.manager),
    manager_name: text(raw.manager_name),
    updated_at: text(raw.updated_at),
  }
}

/** Whether a reply says the store is behind (S3-2A: a business migration recorded, not applied): the page then says
 *  「要先运行 ampuly db-version --apply」 -- or the server's own `why_zh` sentence -- instead of drawing an empty profile
 *  as if it were the truth. */
export function needsMigration(payload: Record<string, unknown> | null | undefined): boolean {
  if (!payload) return false
  if (payload.needs_migration === true) return true
  const code = text(payload.error_code) || text(payload.error)
  return code === 'needs_migration'
}

/** The server's own sentence for a store that is behind (`why_zh`), '' when it sent none. */
export function migrationWhy(payload: Record<string, unknown> | null | undefined): string {
  return payload ? text(payload.why_zh) : ''
}

/** A refused write (project_profile_set / project_connections_set) in plain words, for the button it was pressed on
 *  and the block it belongs to: the server's own sentence when it sent one (「客户不能被规划为管理者…」), a plain sentence
 *  for the bare codes it can send instead, never a code shown as is. '' when there is no refusal. The request tracker
 *  leaves a settled entry unworded on purpose (lib/requestTracker wordsFor): the page says the refusal. */
export function refusalText(code: string, zh: boolean): string {
  const said = text(code)
  if (!said) return ''
  if (said === 'owner_required') return zh ? '只有老板能改这一项。' : 'Only the owner can change this.'
  if (said === 'needs_migration') return zh ? '要先运行 ampuly db-version --apply，这一项才能读写。' : 'Run ampuly db-version --apply first; until then this cannot be read or written.'
  // A bare code (letters, digits, dots, underscores) is not a sentence anyone should read.
  if (/^[A-Za-z0-9_.:-]+$/.test(said)) return zh ? '服务端没有接受这次保存，请稍后再试。' : 'The server did not accept this save; try again later.'
  return said
}

/** 待确定 words for a boundary nobody wrote yet (the prototype's defaults, said as "to be decided", never as facts). */
export const BOUNDARY_PLACEHOLDERS = {
  start: ['待确定：什么事件或资料启动业务', 'To decide: what starts the work'],
  scope: ['待确定：安普里负责的范围与不负责的事项', 'To decide: what Ampuly handles, and what it does not'],
  end: ['待确定：交出什么、由谁确认结束', 'To decide: what is handed over, and who confirms the end'],
} as const

// ── the connection lanes (软件与连接) ────────────────────────────────────

export const CONNECTION_LANES = ['intake', 'review', 'customer', 'files'] as const
export type ConnectionLaneId = typeof CONNECTION_LANES[number]

export interface ConnectionLane {
  /** The lane id (`lane` on the wire): intake / review / customer / files. */
  id: string
  /** 业务来源 / 内部协作 / 客户确认 / 交付存储 (`label` on the wire). */
  name: string
  /** 使用的软件或平台. */
  tool: string
  /** 交进去什么，拿回来什么. */
  purpose: string
  /** 交互方式: one of CONNECTION_MODES. */
  mode: string
  /** 主要关联阶段 (a stage id of the project's first flow with stages); '' = 待关联. */
  stage: string
  /** The server's status WORD (`status_word`: 未配置 / 待验证 / 可用 …), judged from real facts only; shown as is. */
  status: string
  /** The server's status code behind the word (unconfigured / unverified / usable / broken …); '' when it sent none. */
  statusCode?: string
  /** What the server saw that led to the word, in its own sentences. */
  evidence?: string[]
}

export const CONNECTION_MODES = ['人工录入', '人工交接', 'API 接口', '内置浏览器', '桌面软件', '本地文件'] as const

const LANE_DEFAULTS: Record<ConnectionLaneId, Omit<ConnectionLane, 'id'>> = {
  intake: { name: '业务来源', tool: '', purpose: '原有系统的订单或需求 → 本项目的业务输入', mode: '人工录入', stage: '', status: '未配置' },
  review: { name: '内部协作', tool: '', purpose: '固定版本材料 → 审核人与意见回传', mode: 'API 接口', stage: '', status: '未配置' },
  customer: { name: '客户确认', tool: '', purpose: '指定交付版本 → 客户确认或修改意见', mode: '人工交接', stage: '', status: '未配置' },
  files: { name: '交付存储', tool: '', purpose: '工单产物 → 版本归档与交付引用', mode: '本地文件', stage: '', status: '未配置' },
}

/** The four lanes, always in their order: what the server sent for each (project_connections_get's `lanes`, a list of
 *  `{lane, label, tool, purpose, mode, stage, status, status_word, evidence}`, or a record by lane id), filled with the
 *  lane's name and the 未配置 word where it sent nothing. The word shown is the server's `status_word`; a reply that
 *  carries only a `status` word (no code) is read as the word. A lane the server names that is not one of the four is
 *  kept after them. */
export function connectionsFrom(value: unknown): ConnectionLane[] {
  const given = new Map<string, Record<string, unknown>>()
  if (Array.isArray(value)) {
    for (const item of value) {
      if (item && typeof item === 'object') {
        const id = text((item as Record<string, unknown>).id ?? (item as Record<string, unknown>).lane)
        if (id) given.set(id, item as Record<string, unknown>)
      }
    }
  } else if (value && typeof value === 'object') {
    for (const [id, item] of Object.entries(value as Record<string, unknown>)) {
      if (item && typeof item === 'object') given.set(id, item as Record<string, unknown>)
    }
  }
  const lane = (id: string, defaults: Omit<ConnectionLane, 'id'>): ConnectionLane => {
    const raw = given.get(id) ?? {}
    const word = text(raw.status_word)
    const status = text(raw.status)
    return {
      id,
      name: text(raw.label) || text(raw.name) || defaults.name,
      tool: text(raw.tool),
      purpose: text(raw.purpose) || defaults.purpose,
      mode: text(raw.mode) || defaults.mode,
      stage: text(raw.stage),
      status: word || status || defaults.status,
      statusCode: word ? status : '',
      evidence: Array.isArray(raw.evidence) ? raw.evidence.map(text).filter(Boolean) : [],
    }
  }
  const lanes = CONNECTION_LANES.map(id => lane(id, LANE_DEFAULTS[id]))
  for (const id of given.keys()) {
    if (!(CONNECTION_LANES as readonly string[]).includes(id)) lanes.push(lane(id, { ...LANE_DEFAULTS.intake, name: id, purpose: '' }))
  }
  return lanes
}

/** 交付到哪里 shows the customer and files lanes; 业务从哪里来 the intake lane. */
export function endcapLanes(lanes: readonly ConnectionLane[], side: 'in' | 'out'): ConnectionLane[] {
  const wanted: readonly string[] = side === 'in' ? ['intake'] : ['customer', 'files']
  return lanes.filter(lane => wanted.includes(lane.id))
}

// ── stages (业务全景) ─────────────────────────────────────────────────────

export interface FlowStage {
  id: string
  name: string
  /** The canvas nodes this stage groups; an order stands here when one of its live steps is among them. */
  nodes: string[]
  input: string
  work: string
  output: string
  next: string
  back: string
  /** Connection lane ids used in this stage (用什么协作). */
  systems: string[]
  parallel: boolean
}

/** The stage map a flow carries (`stages` on its workflow_list row, S3-2A); [] when it carries none -- the flow line
 *  then says 这条业务流还没有阶段映射 and lists its orders unplaced. */
export function stagesOf(flow: object | null | undefined): FlowStage[] {
  const raw = (flow as { stages?: unknown } | null | undefined)?.stages
  if (!Array.isArray(raw)) return []
  const out: FlowStage[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const stage = item as Record<string, unknown>
    const id = text(stage.id)
    if (!id) continue
    out.push({
      id,
      name: text(stage.name) || id,
      nodes: Array.isArray(stage.nodes) ? stage.nodes.map(text).filter(Boolean) : [],
      input: text(stage.input),
      work: text(stage.work),
      output: text(stage.output),
      next: text(stage.next),
      back: text(stage.back),
      systems: Array.isArray(stage.systems) ? stage.systems.map(text).filter(Boolean) : [],
      parallel: stage.parallel === true,
    })
  }
  return out
}

/** The node fields the panorama reads: the builder's WorkflowNode plus S3-2A's three (channel / sla_hours /
 *  confirm_mode), every one optional. Structural, so a test hands in a plain object. */
export type PanoramaNode = {
  id: string
  name?: string
  type?: string
  role?: string
  assignee?: string
  gate?: string
  deliverables?: string[]
  rework_to?: string
  responsibility?: string
  skills?: string[]
  /** S3-2A: how this step is confirmed: none / human / customer. Read first; the gate and the type stand in without it. */
  confirm_mode?: string
  /** S3-2A: the channel the confirmation goes out on (飞书 …). */
  channel?: string
  /** S3-2A: the confirmation's time limit, in hours; absent = 未配置期限. */
  sla_hours?: number | null
}

export type ConfirmMode = 'none' | 'human' | 'customer'

/** How a step is confirmed. `confirm_mode` when the server sends it; otherwise a human review or a human gate is a
 *  person's confirmation, and the acceptance step is the customer's (the one step the engine gives the customer). */
export function confirmModeOf(node: PanoramaNode): ConfirmMode {
  const mode = text(node.confirm_mode).toLowerCase()
  if (mode === 'customer' || mode === '客户确认') return 'customer'
  if (mode === 'human' || mode === '人工确认') return 'human'
  if (mode === 'none') return 'none'
  if (node.id === 'acceptance') return 'customer'
  if (node.type === 'human_review' || node.type === 'human_do' || node.gate === 'human_required') return 'human'
  return 'none'
}

export const STAGE_MODE_WORDS = {
  ai: ['AI 执行', 'AI does it'],
  human: ['AI + 人工确认', 'AI + a person confirms'],
  customer: ['AI + 客户确认', 'AI + the customer confirms'],
} as const

export interface StageView {
  stage: FlowStage
  /** The nodes of the flow this stage groups, in the flow's order. */
  nodes: PanoramaNode[]
  /** ai / human / customer: the customer's confirmation wins over a person's, a person's over none. */
  mode: keyof typeof STAGE_MODE_WORDS
  /** The confirming nodes (human or customer). */
  gates: PanoramaNode[]
  /** Who confirms, by name (through `nameOf`), deduplicated; 待指定 for a gate with nobody. */
  names: string[]
}

export function stageView(stage: FlowStage, nodes: readonly PanoramaNode[], nameOf: (employeeId: string) => string, zh: boolean): StageView {
  const own = nodes.filter(node => stage.nodes.includes(node.id))
  const gates = own.filter(node => confirmModeOf(node) !== 'none')
  const mode = gates.some(node => confirmModeOf(node) === 'customer') ? 'customer' : gates.length ? 'human' : 'ai'
  const names = [...new Set(gates.map(node => {
    if (confirmModeOf(node) === 'customer') return zh ? '客户' : 'the customer'
    const who = text(node.assignee)
    return who ? nameOf(who) : (zh ? '待指定' : 'to assign')
  }))]
  return { stage, nodes: own, mode, gates, names }
}

/** The stage an order stands in: the first stage (in the flow's order) that holds one of the order's live steps
 *  (current_nodes ∩ stage.nodes). '' for an ended order, an order with no live step, or one whose steps are in no
 *  stage (待定位). One stage, whatever how many parallel steps the order has there. */
export function positionOf(order: InboxOrderLike, stages: readonly FlowStage[]): string {
  if (attentionOf(order).ended) return ''
  const live = new Set((order.current_nodes ?? []).map(node => text(node.node_id)).filter(Boolean))
  if (live.size === 0) return ''
  for (const stage of stages) if (stage.nodes.some(id => live.has(id))) return stage.id
  return ''
}

/** Abnormal or overdue first, then waiting on the owner, then the nearest due date, then the id (so two machines
 *  agree). The order of the prototype's `priority`. */
export function byPriority<T extends InboxOrderLike>(rows: readonly T[]): T[] {
  return [...rows].sort((a, b) => {
    const x = attentionOf(a), y = attentionOf(b)
    const hot = (v: typeof x) => Number(!v.ended && (v.abnormal || v.overdue))
    const mine = (v: typeof x) => Number(!v.ended && v.waiting_on === 'owner')
    return hot(y) - hot(x)
      || mine(y) - mine(x)
      || (x.due_days ?? Number.POSITIVE_INFINITY) - (y.due_days ?? Number.POSITIVE_INFINITY)
      || (a.order_id < b.order_id ? -1 : a.order_id > b.order_id ? 1 : 0)
  })
}

/** The in-flight orders of one flow, each once, with the stage each stands in ('' = 待定位). An order in two
 *  parallel steps of one stage is one order: the position is decided once per order, never per step. */
export function placedOrders<T extends InboxOrderLike>(orders: readonly T[], flowId: string, stages: readonly FlowStage[]): Array<{ order: T; stage: string }> {
  const seen = new Set<string>()
  const out: Array<{ order: T; stage: string }> = []
  for (const order of byPriority(orders.filter(order => (order.workflow_id ?? '') === flowId && !attentionOf(order).ended))) {
    if (seen.has(order.order_id)) continue
    seen.add(order.order_id)
    out.push({ order, stage: positionOf(order, stages) })
  }
  return out
}

// ── the words on an order ─────────────────────────────────────────────────

export interface OrderLine {
  orderId: string
  /** The customer and brand, else the flow's name -- never the order's number. */
  title: string
  /** The live step(s), in words. */
  step: string
  /** The one status word (the server's), '' when there is none. */
  word: string
  /** Red only for an abnormal reason (the design: red only overdue / abnormal). */
  hot: boolean
  /** The due date in words, '' when none; red when overdue. */
  due: string
  overdue: boolean
  /** Whose turn: 你 / an employee's name / 客户 / '' . */
  who: string
}

export function orderLine(order: InboxOrderLike, flowName: string, nameOf: (employeeId: string) => string, zh: boolean): OrderLine {
  const attention = attentionOf(order)
  const word = statusWord(attention, zh)
  const due = dueWords(attention, zh)
  const who = attention.ended ? ''
    : attention.waiting_on === 'owner' ? (zh ? '你' : 'you')
      : attention.waiting_on === 'employee' ? (attention.waiting_employee_id ? nameOf(attention.waiting_employee_id) : (zh ? '别人' : 'someone'))
        : attention.waiting_on === 'customer' ? (zh ? '客户' : 'the customer') : ''
  return {
    orderId: order.order_id,
    title: rowTitle(order, flowName, zh),
    step: stepText(order, zh),
    word: word.text,
    hot: !attention.ended && (attention.abnormal || attention.overdue),
    due: due.text,
    overdue: due.tone === 'red',
    who,
  }
}

/** 现在需要关注: the live orders that are abnormal, overdue or waiting on the owner, abnormal first (`byPriority`),
 *  the first `limit` of them and how many there are in all. From the server's verdict on each row, nothing else. */
export function attentionTop<T extends InboxOrderLike>(orders: readonly T[] | null | undefined, limit = 3): { items: T[]; total: number } {
  const found = byPriority((orders ?? []).filter(order => {
    const attention = attentionOf(order)
    return !attention.ended && (attention.abnormal || attention.overdue || attention.waiting_on === 'owner')
  }))
  return { items: found.slice(0, Math.max(0, limit)), total: found.length }
}

// ── 步骤责任 (谁负责哪一步) ───────────────────────────────────────────────

export interface ResponsibilityRow {
  node: PanoramaNode
  name: string
  /** What executes it: the step's responsibility / skills / role, in that order of what is known. */
  execution: string
  gate: boolean
  mode: ConfirmMode
  /** Who confirms: a name, 客户, 待指定, or 无人工关口. */
  confirmer: string
  channel: string
  /** N 小时 · 超时不自动放行, or 未配置期限. */
  deadline: string
  /** The deliverables, or 待补交付约定. */
  deliverables: string
  /** 意见返回：… -- the step a 打回 goes back to, by name; 按流程配置 when none; a missing target says so. */
  returnTo: string
}

export function responsibilityRows(nodes: readonly PanoramaNode[], nameOf: (employeeId: string) => string, zh: boolean, onlyGates = true): ResponsibilityRow[] {
  const byId = new Map(nodes.map(node => [node.id, node]))
  const rows = nodes.map((node): ResponsibilityRow => {
    const mode = confirmModeOf(node)
    const gate = mode !== 'none'
    const who = text(node.assignee)
    const confirmer = !gate ? (zh ? '无人工关口' : 'no human gate')
      : mode === 'customer' ? (zh ? '客户' : 'the customer')
        : who ? nameOf(who) : (zh ? '待指定' : 'to assign')
    const hours = typeof node.sla_hours === 'number' && Number.isFinite(node.sla_hours) && node.sla_hours > 0 ? node.sla_hours : null
    const back = text(node.rework_to)
    const returnTo = !back ? (zh ? '按流程配置' : 'as the flow says')
      : byId.has(back) ? (text(byId.get(back)?.name) || back) : (zh ? '原目标已不存在，需重新配置' : 'the target no longer exists; configure it again')
    const execution = text(node.responsibility) || (node.skills ?? []).map(text).filter(Boolean).join('、') || text(node.role) || (zh ? '按节点执行配置' : 'as the step is configured')
    return {
      node,
      name: text(node.name) || node.id,
      execution,
      gate,
      mode,
      confirmer,
      channel: text(node.channel) || '—',
      deadline: hours !== null ? (zh ? `${hours} 小时 · 超时不自动放行` : `${hours} h · never released on timeout`) : (zh ? '未配置期限' : 'no time limit set'),
      deliverables: (node.deliverables ?? []).map(text).filter(Boolean).join('、') || (zh ? '待补交付约定' : 'deliverables to be written'),
      returnTo,
    }
  })
  return onlyGates ? rows.filter(row => row.gate) : rows
}

// ── 员工 / 客户视角 (read-only preview) ───────────────────────────────────

/** 分给我的步骤: the rows an employee's own list (my_tasks_list) would carry, from the orders the console already
 *  holds -- every live order the server says waits on that employee, in that list's shape (title · status · node ·
 *  standard). A read-only projection: nothing is asked for and nobody's identity is assumed. */
export function employeePreviewRows(orders: readonly InboxOrderLike[] | null | undefined, employeeId: string, flowNameOf: (flowId: string) => string, zh: boolean): Array<{ orderId: string; title: string; status: string; node: string; standard: string }> {
  const id = text(employeeId)
  if (!id) return []
  return byPriority((orders ?? []).filter(order => {
    const attention = attentionOf(order)
    return !attention.ended && attention.waiting_on === 'employee' && attention.waiting_employee_id === id
  })).map(order => ({
    orderId: order.order_id,
    title: rowTitle(order, flowNameOf(order.workflow_id ?? ''), zh),
    status: attentionOf(order).waiting_kind || (zh ? '轮到你' : 'your turn'),
    node: stepText(order, zh),
    standard: dueWords(attentionOf(order), zh).text,
  }))
}

/** 我的交付: the orders of one customer, for the customer preview (customer_view_preview is asked per order). */
export function customerPreviewOrders<T extends InboxOrderLike & { customer_id?: string }>(orders: readonly T[] | null | undefined, customerId: string): T[] {
  const id = text(customerId)
  if (!id) return []
  return byPriority((orders ?? []).filter(order => text(order.customer_id) === id))
}
