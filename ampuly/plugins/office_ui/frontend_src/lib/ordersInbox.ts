// 业务工单 = 收件箱 (frontend redesign step 3, first page; policy, design MASTER-PAGE-REDESIGN §十一 and
// FRONTEND-IA 「工单：收件箱」 / 「第二轮定稿」). Pure helpers over what the page already holds: the order rows with the
// server's `attention` verdict (S0-A: who each order waits on, abnormal, overdue, how it ended), their current steps
// (S0-B `current_nodes`), the pauses the console polls and the flows. The grouping, the sort inside each group, the four
// words of a row and the 「现在」 block all come from here, so the page draws and the test reads one and the same thing.
//
// Nothing here is a new fact and nothing is asked for: the server said whom an order waits on; this only says it in the
// owner's words. The client-side queue rule (lib/orderQueues' needsPerson / personWaits) is not used on this page any
// more -- it stays only where other pages still read it (项目中心, the data tree, the flow spread).

import { blockerKind } from '../components/DashboardPage'
import { childrenOf, parentOf, type Parent } from './orderLineage'
import { blockersOfOrder } from './orderWaiting'
import type { OwnerCardAction, OwnerCardView } from './ownerCard'

// ── what the server says on a row ─────────────────────────────────────────

/** The S0-A verdict as `order_list` carries it on each row (`attention`). Every field is optional on the wire; read
 *  through `attentionOf`, which fills the defaults and says `partial` when the server could not judge. */
export interface OrderAttention {
  /** '' | 'owner' | 'employee' | 'customer' */
  waiting_on: string
  waiting_employee_id: string
  /** The server's one word for what it waits for (待接单 / 等你审核 / 等你验收 / 要用工具 / 钱不够了 / 没收到回执 / 已停 /
   *  等客户确认 / 等你确认收货 / 轮到某人 / 等你回答); '' when it waits on nobody. */
  waiting_kind: string
  waiting_since: string
  snoozed: boolean
  abnormal: boolean
  /** 出错了 / 已停 / 返工出错 / 没收到回执 / 钱不够了 / 预算已关, or ''. */
  abnormal_reason: string
  due_days: number | null
  overdue: boolean
  ended: boolean
  /** '' while live; closed / rejected / cancelled once ended. */
  ended_as: string
  delivered: boolean
  /** The pauses or budgets of the project could not be read: waiting and abnormal are unknown, not false. */
  partial: boolean
}

export interface CurrentNode { node_id: string; node_name: string }

/** The row as this page reads it: the fields every order row has, plus what S0 added. Structural, so a test can hand
 *  in a plain object and the page can hand in a WorkOrderSummary. */
export type InboxOrderLike = {
  order_id: string
  status: string
  workflow_id?: string
  customer_name?: string
  brand_name?: string
  updated_at?: string
  created_at?: string
  launched_task_id?: string
  current_nodes?: CurrentNode[] | null
  ended_as?: string
  attention?: Partial<OrderAttention> | null
  acceptance?: { status?: string; round_no?: number; workflow_version?: string; criteria?: string[] } | null
  handling?: { due_at?: string; assignee_employee_id?: string } | null
  fields?: Record<string, unknown> | null
  /** How many orders this one sent out are still running, as the server counts them (a summary order). */
  running_child_orders?: number
}

/** The server's words for `waiting_kind` and `abnormal_reason` (order_attention.py); matched by value, never shown
 *  from here as anything else. */
export const KIND = Object.freeze({
  intake: '待接单', review: '等你审核', answer: '等你回答', tool: '要用工具', allowance: '钱不够了', noReceipt: '没收到回执',
  stopped: '已停', acceptance: '等你验收', customer: '等客户确认', close: '等你确认收货', employee: '轮到某人',
})
export const REASON = Object.freeze({ failed: '出错了', stopped: '已停', rework: '返工出错', noReceipt: '没收到回执', allowance: '钱不够了', budgetClosed: '预算已关' })

const ENDED_STATUSES = new Set(['closed', 'rejected'])

/** The verdict with its defaults. A row without one (an older server, or a list not judged) is read from the row's
 *  own status alone and marked partial: ended by status, waiting on nobody, never abnormal. */
export function attentionOf(order: InboxOrderLike): OrderAttention {
  const given = order.attention && typeof order.attention === 'object' ? order.attention : null
  const endedByStatus = ENDED_STATUSES.has(order.status)
  const endedAs = String(given?.ended_as ?? order.ended_as ?? (endedByStatus ? order.status : '')).trim()
  const days = given?.due_days
  return {
    waiting_on: String(given?.waiting_on ?? '').trim(),
    waiting_employee_id: String(given?.waiting_employee_id ?? '').trim(),
    waiting_kind: String(given?.waiting_kind ?? '').trim(),
    waiting_since: String(given?.waiting_since ?? '').trim(),
    snoozed: given?.snoozed === true,
    abnormal: given?.abnormal === true,
    abnormal_reason: String(given?.abnormal_reason ?? '').trim(),
    due_days: typeof days === 'number' && Number.isFinite(days) ? days : null,
    overdue: given?.overdue === true,
    ended: given?.ended === true || endedByStatus,
    ended_as: endedAs,
    delivered: given?.delivered === true || (given === null && order.status === 'closed'),
    partial: given === null || given.partial === true,
  }
}

// ── groups and filters ────────────────────────────────────────────────────

export type InboxGroup = 'mine' | 'live' | 'done' | 'cancelled'
export const INBOX_GROUPS: readonly InboxGroup[] = ['mine', 'live', 'done', 'cancelled']

/** Which of the inbox's groups an order sits in: from the server's verdict only. */
export function groupOf(order: InboxOrderLike): InboxGroup {
  const attention = attentionOf(order)
  if (attention.ended) return attention.ended_as === 'cancelled' ? 'cancelled' : 'done'
  return attention.waiting_on === 'owner' ? 'mine' : 'live'
}

/** The group's name, what it holds, how many and by what it is sorted -- the head of each group says all three. */
export const GROUP_WORDS: Record<InboxGroup, { name: readonly [string, string]; holds: readonly [string, string]; sortedBy: readonly [string, string] }> = {
  mine: { name: ['等你处理', 'Waiting on you'], holds: ['等你决定的单', 'orders that need your decision'], sortedBy: ['逾期在前，其次等得最久', 'overdue first, then longest waiting'] },
  live: { name: ['进行中', 'In progress'], holds: ['在做、等别人或等客户的单', 'being done, or waiting on someone else'], sortedBy: ['截止最近在前', 'nearest due date first'] },
  done: { name: ['已完成', 'Done'], holds: ['已结案或已打回的单', 'closed or refused orders'], sortedBy: ['最近结案在前', 'most recently ended first'] },
  cancelled: { name: ['已取消', 'Cancelled'], holds: ['取消整单的单', 'orders cancelled whole'], sortedBy: ['最近取消在前', 'most recently cancelled first'] },
}

// `live` and `delivered` (S3-2B): 项目中心's 在途 and 已交付 as the server counts them (order_attention.count_standing),
// so each number opens exactly the list it counted. 在途 = the whole 进行中 group (live and not waiting on the owner,
// the customer's turn included); 已交付 = closed and not cancelled -- narrower than 已完成, which holds the refused
// (打回) orders too.
export const INBOX_STATES = ['all', 'mine', 'risk', 'live', 'others', 'customer', 'done', 'delivered', 'cancelled'] as const
export type InboxState = typeof INBOX_STATES[number]
export const INBOX_STATE_LABELS: Record<InboxState, readonly [string, string]> = {
  all: ['全部', 'All'],
  mine: ['等你处理', 'Waiting on you'],
  risk: ['逾期或异常', 'Overdue or in trouble'],
  live: ['在途', 'In flight'],
  others: ['在做 · 别人负责', 'In progress · someone else'],
  customer: ['等客户', 'Waiting on the customer'],
  done: ['已完成', 'Done'],
  delivered: ['已交付', 'Delivered'],
  cancelled: ['已取消', 'Cancelled'],
}

/** The inbox state another page asks for by the old queue names (项目中心's counts, 总控's 等人处理): the nearest group. */
export function stateFromQueue(queue: string | undefined | null, state?: string | null): InboxState {
  // A state named directly (项目中心's four numbers, S3-2B) wins over the old queue name beside it.
  if (state && (INBOX_STATES as readonly string[]).includes(state)) return state as InboxState
  switch (queue) {
    case 'needs_person': return 'mine'
    case 'in_progress': return 'others'
    case 'ended': return 'done'
    default: return 'all'
  }
}

export function matchesState(order: InboxOrderLike, state: InboxState): boolean {
  const attention = attentionOf(order)
  const group = groupOf(order)
  switch (state) {
    case 'all': return true
    case 'mine': return group === 'mine'
    case 'risk': return !attention.ended && (attention.abnormal || attention.overdue)
    case 'live': return group === 'live'
    case 'others': return group === 'live' && attention.waiting_on !== 'customer'
    case 'customer': return !attention.ended && attention.waiting_on === 'customer'
    case 'done': return group === 'done'
    // The server's own verdict (closed, not cancelled): a refused order is 已完成 but never 已交付.
    case 'delivered': return attention.delivered && attention.ended_as !== 'cancelled'
    case 'cancelled': return group === 'cancelled'
  }
}

/** The rows the filter row leaves: a state, exactly one flow (not a search word: one flow's id can be a prefix of
 *  another's), and every typed word found in the order's number, customer, brand or flow. */
export function listedOrders<T extends InboxOrderLike>(orders: readonly T[], filter: { state?: InboxState; flow?: string; query?: string }): T[] {
  const words = (filter.query ?? '').toLowerCase().split(/\s+/).filter(Boolean)
  const state = filter.state ?? 'all'
  const flow = (filter.flow ?? '').trim()
  return orders.filter(order => {
    if (!matchesState(order, state)) return false
    if (flow && (order.workflow_id ?? '') !== flow) return false
    if (words.length === 0) return true
    const text = [order.order_id, order.customer_name, order.brand_name, order.workflow_id].filter(Boolean).join('\n').toLowerCase()
    return words.every(word => text.includes(word))
  })
}

const LAST = '￿'
const byId = (a: InboxOrderLike, b: InboxOrderLike) => (a.order_id < b.order_id ? -1 : a.order_id > b.order_id ? 1 : 0)
const stamp = (value: string | undefined) => String(value ?? '').trim()

/** 等你处理: overdue first, then the one that has waited longest (a wait with no time last); 进行中: the nearest due
 *  date first (no due date last), then the most recently touched; 已完成 / 已取消: the most recently ended first. */
export function sortGroup<T extends InboxOrderLike>(group: InboxGroup, rows: readonly T[]): T[] {
  const sorted = [...rows]
  if (group === 'mine') {
    sorted.sort((a, b) => {
      const x = attentionOf(a), y = attentionOf(b)
      return Number(y.overdue) - Number(x.overdue)
        || (x.waiting_since || stamp(a.updated_at) || LAST).localeCompare(y.waiting_since || stamp(b.updated_at) || LAST)
        || byId(a, b)
    })
  } else if (group === 'live') {
    sorted.sort((a, b) => {
      const x = attentionOf(a).due_days, y = attentionOf(b).due_days
      return (x ?? Number.POSITIVE_INFINITY) - (y ?? Number.POSITIVE_INFINITY)
        || stamp(b.updated_at).localeCompare(stamp(a.updated_at))
        || byId(a, b)
    })
  } else {
    sorted.sort((a, b) => stamp(b.updated_at).localeCompare(stamp(a.updated_at)) || byId(a, b))
  }
  return sorted
}

export type InboxSections<T> = Record<InboxGroup, T[]>

/** The listed rows in their groups, each group sorted its own way. */
export function inboxSections<T extends InboxOrderLike>(rows: readonly T[]): InboxSections<T> {
  const sections: InboxSections<T> = { mine: [], live: [], done: [], cancelled: [] }
  for (const row of rows) sections[groupOf(row)].push(row)
  for (const group of INBOX_GROUPS) sections[group] = sortGroup(group, sections[group])
  return sections
}

/** The order to show when none is chosen, or the chosen one is no longer listed: the first of the first group that
 *  has any (等你处理 before 进行中 before 已完成 before 已取消). */
export function firstListed<T extends InboxOrderLike>(sections: InboxSections<T>): T | null {
  for (const group of INBOX_GROUPS) if (sections[group].length) return sections[group][0]
  return null
}

// ── words ─────────────────────────────────────────────────────────────────

export type Tone = '' | 'red' | 'amber' | 'accent'

const STATUS_STEP: Record<string, readonly [string, string]> = {
  collecting: ['收集需求', 'collecting the brief'],
  pending_review: ['待接单', 'waiting to be taken'],
  launching: ['正在发起', 'starting'],
  launched: ['执行中', 'running'],
  producing: ['执行中', 'running'],
  delivering: ['交付中', 'delivering'],
  closed: ['已结案', 'closed'],
  rejected: ['已打回', 'refused'],
}

/** The step the order stands at, in words: the live steps' names (S0-B) joined, else a word for its status. */
export function stepText(order: InboxOrderLike, zh: boolean): string {
  const nodes = (order.current_nodes ?? []).map(node => String(node.node_name || node.node_id || '').trim()).filter(Boolean)
  if (nodes.length) return nodes.join(' / ')
  if (order.acceptance?.status === 'rejected' && !ENDED_STATUSES.has(order.status)) return zh ? '返工中' : 'reworking'
  const known = STATUS_STEP[order.status]
  return known ? known[zh ? 0 : 1] : (zh ? '进行中' : 'in progress')
}

/** How the order is called on a row: the customer and brand, else the flow's name -- never its number. */
export function rowTitle(order: InboxOrderLike, flowName: string, zh: boolean): string {
  return [order.customer_name, order.brand_name].map(value => String(value ?? '').trim()).filter(Boolean).join(' · ')
    || flowName.trim() || (zh ? '工单' : 'Order')
}

/** The time slot: the due date as days, from the verdict. Red only when overdue; nothing for an ended order or one
 *  with no due date. */
export function dueWords(attention: OrderAttention, zh: boolean): { text: string; tone: Tone } {
  if (attention.ended || attention.due_days === null) return { text: '', tone: '' }
  const days = attention.due_days
  if (days < 0) return { text: zh ? `逾期 ${-days} 天` : `${-days}d overdue`, tone: 'red' }
  if (days === 0) return { text: zh ? '今天到期' : 'due today', tone: '' }
  return { text: zh ? `${days} 天后` : `in ${days}d`, tone: '' }
}

const ENDED_WORDS: Record<string, readonly [string, string]> = {
  closed: ['已结案', 'closed'],
  cancelled: ['已取消', 'cancelled'],
  rejected: ['已打回', 'refused'],
}

/** The one status word of a row, and its tone: red for 出错了 and the other abnormal reasons, amber for waiting on
 *  someone else (the customer), and NO colour for the server's 等你… words or an ended order's word (the design: red
 *  only for overdue or abnormal, amber only for waiting on someone else, nothing else coloured). A step assigned to an
 *  employee carries no word: its line says 轮到 <name> instead. */
export function statusWord(attention: OrderAttention, zh: boolean): { text: string; tone: Tone } {
  if (attention.ended) {
    const word = ENDED_WORDS[attention.ended_as] ?? ENDED_WORDS.closed
    return { text: word[zh ? 0 : 1], tone: '' }
  }
  if (attention.abnormal && attention.abnormal_reason) return { text: attention.abnormal_reason, tone: 'red' }
  if (attention.waiting_on === 'owner' && attention.waiting_kind) return { text: attention.waiting_kind, tone: '' }
  if (attention.waiting_on === 'customer') return { text: zh ? '等客户' : 'customer', tone: 'amber' }
  return { text: '', tone: '' }
}

export interface RowView {
  group: InboxGroup
  title: string
  sub: string
  time: string
  timeTone: Tone
  word: string
  wordTone: Tone
  /** The verdict's reminder is held (snoozed): drawn quieter, still in its group. */
  held: boolean
  /** The server could not judge this row: it sits in 进行中 with no word, and the page says so once. */
  partial: boolean
}

/** The four fixed slots of a row: name · flow and step · due · one status word. */
export function rowView(order: InboxOrderLike, flowName: string, zh: boolean, nameOf: (employeeId: string) => string = id => id): RowView {
  const attention = attentionOf(order)
  const step = stepText(order, zh)
  const hasCustomer = Boolean(String(order.customer_name ?? '').trim() || String(order.brand_name ?? '').trim())
  let sub = hasCustomer && flowName.trim() ? `${flowName.trim()} · ${step}` : step
  if (!attention.ended && attention.waiting_on === 'employee') {
    const who = attention.waiting_employee_id ? nameOf(attention.waiting_employee_id) : (zh ? '别人' : 'someone')
    sub = zh ? `轮到 ${who} · ${step}` : `${who}'s turn · ${step}`
  }
  const due = dueWords(attention, zh)
  const word = statusWord(attention, zh)
  return {
    group: groupOf(order),
    title: rowTitle(order, flowName, zh),
    sub,
    time: due.text,
    timeTone: due.tone,
    word: word.text,
    wordTone: word.tone,
    held: attention.snoozed,
    partial: attention.partial && !attention.ended,
  }
}

// ── the 「现在」 block ──────────────────────────────────────────────────────

export type NowKind = 'intake' | 'review' | 'acceptance' | 'close' | 'card' | 'no_receipt' | 'customer' | 'employee' | 'failed' | 'running' | 'collecting' | 'ended'

export interface NowView {
  kind: NowKind
  title: string
  line: string
  /** The one button's word; '' when there is nothing to press (an employee's turn, an ended order, a card that draws
   *  its own answers). */
  button: string
  buttonTone: 'primary' | 'outline' | 'none'
  /** What pressing it does, under the button; '' when there is nothing worth saying. */
  consequence: string
  /** Red for an abnormal or overdue order; '' otherwise. */
  tone: Tone
}

export interface NowFacts {
  /** The name of the step it stands at (stepText). */
  step: string
  /** The employee a human step is assigned to, by name; '' when nobody. */
  employee?: string
  /** Whether a pause of the order is on the console for the owner to answer (its OwnerCard is drawn in the block). */
  hasCard?: boolean
}

const w = (zh: boolean, cn: string, en: string) => (zh ? cn : en)

/** One sentence where the order stands and ONE primary button, by state -- the owner's rulings : 待接单
 * 我来接; 等你审核 看材料并处理; 等你验收 看交付物并处理; 等你确认收货 确认收货并结案; 等客户 看交接与反馈; 出错了 看出了什么问题;
 * 正在做 看进度; the tool / allowance / receipt / stopped cards draw themselves; an employee's turn and an ended order
 * have no button. Never 同意 / 通过. */
export function nowView(order: InboxOrderLike, facts: NowFacts, zh: boolean): NowView {
  const attention = attentionOf(order)
  const step = facts.step.trim() || stepText(order, zh)
  const tone: Tone = !attention.ended && (attention.abnormal || attention.overdue) ? 'red' : ''
  const none = (kind: NowKind, title: string, line: string): NowView => ({ kind, title, line, button: '', buttonTone: 'none', consequence: '', tone })
  if (attention.ended) {
    if (attention.ended_as === 'cancelled') return none('ended', w(zh, '已取消', 'Cancelled'), w(zh, '整单已取消；已经发出去的动作没有撤回。', 'The whole order was cancelled; actions already sent out were not taken back.'))
    if (attention.ended_as === 'rejected') return none('ended', w(zh, '已打回', 'Refused'), w(zh, '接单时没有接；什么都没跑。', 'It was not taken at intake; nothing ran.'))
    return none('ended', w(zh, '已验收结案', 'Accepted and closed'), w(zh, '交付文件和记录都保留；追加需求请建一张新单。', 'Its files and records are kept; further work is a new order.'))
  }
  if (attention.waiting_on === 'owner') {
    switch (attention.waiting_kind) {
      case KIND.intake:
        return { kind: 'intake', tone, title: w(zh, '还没有人接这张单', 'Nobody has taken this order yet'), line: w(zh, '先核对需求，再决定要不要接。', 'Check the brief first, then decide whether to take it.'),
          button: w(zh, '我来接', 'I will take it'), buttonTone: 'primary', consequence: w(zh, '接了之后才会开始占用项目预算、调用模型。', 'Only once taken does it start spending the project allowance and calling models.') }
      case KIND.review:
        return { kind: 'review', tone, title: w(zh, '等你审核', 'Waiting for your review'), line: w(zh, `「${step}」的材料已固定。看完材料再决定放行还是打回修改。`, `The materials of "${step}" are fixed. Read them, then release or send back.`),
          button: w(zh, '看材料并处理', 'Read the materials and decide'), buttonTone: 'primary', consequence: w(zh, '放行后，后面的步骤马上开始做，不再有 AI 复审；打回要写明哪里需要修改。', 'Releasing starts the next steps at once with no AI re-check; sending back asks you to say what to change.') }
      case KIND.acceptance:
        return { kind: 'acceptance', tone, title: w(zh, '等你验收', 'Waiting for your acceptance'), line: w(zh, '交付物已提交。看完再决定接受还是打回重做。', 'The deliverables were submitted. Read them, then accept or send them back for rework.'),
          button: w(zh, '看交付物并处理', 'Read the deliverables and decide'), buttonTone: 'primary', consequence: w(zh, '接受后这张单就可以结案；打回重做会开新一轮。', 'Accepting lets the order close; sending back starts a new round.') }
      case KIND.close:
        return { kind: 'close', tone, title: w(zh, '等你确认收货', 'Waiting for your receipt'), line: w(zh, '这一轮交付已经接受，只差你确认收货并结案。', 'This round was accepted; only your receipt and the close are left.'),
          button: w(zh, '确认收货并结案', 'Confirm receipt and close'), buttonTone: 'primary', consequence: w(zh, '结案后不能撤回；交付文件保留。', 'A close cannot be undone; the files are kept.') }
      case KIND.noReceipt:
        return { kind: facts.hasCard ? 'card' : 'no_receipt', tone, title: w(zh, '没收到回执', 'No receipt came back'), line: w(zh, '有一次付费调用不知道到底发出去没有。先核对，再说它发出去了还是没有；这一单在那之前不再花钱。', 'A paid call may or may not have gone through. Check first, then say which; nothing more is spent until you do.'),
          button: '', buttonTone: 'none', consequence: '' }
      default:
        return { kind: 'card', tone, title: attention.waiting_kind || w(zh, '等你处理', 'Waiting on you'), line: w(zh, '这张单停在一张等你回答的卡上。', 'The order stopped on a card that waits for your answer.'),
          button: '', buttonTone: 'none', consequence: '' }
    }
  }
  if (attention.waiting_on === 'customer') {
    return { kind: 'customer', tone, title: w(zh, '等客户确认', 'Waiting for the customer'), line: w(zh, '已交给客户确认。内部通过不等于客户验收，只能等，或到交接记录里核对。', 'Handed to the customer to confirm. An internal pass is not the customer\'s acceptance: wait, or check the handover record.'),
      button: w(zh, '看交接与反馈', 'See the handover and feedback'), buttonTone: 'outline', consequence: w(zh, '只看，不代客户确认。', 'Read only; it does not confirm on the customer\'s behalf.') }
  }
  if (attention.waiting_on === 'employee') {
    const who = (facts.employee ?? '').trim() || w(zh, '别人', 'someone else')
    return none('employee', w(zh, `等${who}审核`, `Waiting for ${who}'s review`), w(zh, `「${step}」分给了 ${who}，材料已固定；做完会交到下一步。要自己审在「更多」里。`, `"${step}" is assigned to ${who} and its materials are fixed; it moves on once done. To review it yourself, see 更多.`))
  }
  if (attention.abnormal) {
    return { kind: 'failed', tone, title: w(zh, '出错停下了', 'Stopped on an error'), line: w(zh, `「${step}」没有做完（${attention.abnormal_reason || REASON.failed}），没有自动重试。`, `"${step}" did not finish (${attention.abnormal_reason || 'error'}); nothing was retried on its own.`),
      button: w(zh, '看出了什么问题', 'See what went wrong'), buttonTone: 'primary', consequence: w(zh, '只看；要重做这一步在里面点。', 'Read only; redoing the step is done inside.') }
  }
  if (order.status === 'collecting') return none('collecting', w(zh, '收集需求中', 'Collecting the brief'), w(zh, '需求还没收齐，收齐后进入接单。', 'The brief is not complete yet; once it is, the order can be taken.'))
  return { kind: 'running', tone, title: w(zh, `正在做「${step}」`, `Working on "${step}"`), line: w(zh, '做完会先自检再交给下一步。', 'Once done it checks itself, then hands over to the next step.'),
    button: w(zh, '看进度', 'See progress'), buttonTone: 'outline', consequence: w(zh, '只看，不改变任何状态。', 'Read only; nothing changes.') }
}

// ── the pauses behind an order ────────────────────────────────────────────

type PauseLike = {
  checkpoint_id?: string
  task_id?: string
  session_id?: string
  waiting_task_id?: string
  created_at?: string
  kind?: string
  gate?: { node_id?: string; node_name?: string; version?: number; assignee?: string } | null
  order_id?: string
}

function pausesOf<T extends PauseLike>(order: InboxOrderLike, blockers: readonly T[] | null | undefined): T[] {
  // The same match every page makes (the run's task, the order's own id) -- and the order the server names on the
  // card (S1-A), which reaches a pause anchored on a work item the row cannot name.
  const mine = new Set(blockersOfOrder(order, blockers).map(item => item.checkpoint_id ?? ''))
  const named = (blockers ?? []).filter(item => String(item.order_id ?? '') === order.order_id)
  const seen = new Set<string>()
  const found: T[] = []
  for (const item of [...(blockers ?? []).filter(item => mine.has(item.checkpoint_id ?? '')), ...named]) {
    const id = String(item.checkpoint_id ?? '')
    if (seen.has(id)) continue
    seen.add(id)
    found.push(item)
  }
  return found.sort((a, b) => String(a.created_at ?? '').localeCompare(String(b.created_at ?? '')))
}

/** The lesson card (留点经验, kind delivery) open on this order, if any: never a primary action, never counted as waiting. */
export function lessonCardOf<T extends PauseLike>(order: InboxOrderLike, blockers: readonly T[] | null | undefined): T | null {
  return pausesOf(order, blockers).find(item => blockerKind(item) === 'delivery') ?? null
}

/** The business gate (a person's step) open on this order, if any: what 看材料并处理 and 我来审 open. */
export function gateCardOf<T extends PauseLike>(order: InboxOrderLike, blockers: readonly T[] | null | undefined): T | null {
  return pausesOf(order, blockers).find(item => blockerKind(item) === 'gate') ?? null
}

/** The card the owner answers in the 「现在」 block: the oldest pause that is neither a gate (the materials view), a
 *  lesson card nor a peer wait. */
export function openCardOf<T extends PauseLike>(order: InboxOrderLike, blockers: readonly T[] | null | undefined): T | null {
  return pausesOf(order, blockers).find(item => !['gate', 'delivery', 'wait'].includes(blockerKind(item))) ?? null
}

/** The lesson checkpoint's two answers as the coordinator takes them (owner_card.py LESSON_LEARN / LESSON_SKIP;
 *  layer0 validates approve / feedback / ignore on company_delivery_feedback). The checkpoint itself carries no
 *  options, so a pause's `options` never name them: they are served under the owner card's 更多 (S1-A). */
export const LESSON_LEARN = 'approve'
export const LESSON_SKIP = 'ignore'

/** 学一轮（会花钱）/ 不用了: the server's own words for the two answers when it worded the pause (its card's 更多 holds
 *  both; primary is null -- never a primary action), else the console's labels on the same two ids. Matched by id,
 *  never by an option's kind: the only 'deny'-looking id here is `ignore`, and blockers.py would not mark it so. */
export function lessonAnswers(card: Pick<OwnerCardView, 'primary' | 'more'> | null | undefined, zh: boolean): { learn: OwnerCardAction; skip: OwnerCardAction } {
  const offered: OwnerCardAction[] = card ? [...(card.primary ? [card.primary] : []), ...card.more] : []
  const find = (id: string) => offered.find(action => action.id === id)
  return {
    learn: find(LESSON_LEARN) ?? { id: LESSON_LEARN, label: w(zh, '学一轮（会花钱）', 'Learn a round (costs money)'), kind: 'approve' },
    skip: find(LESSON_SKIP) ?? { id: LESSON_SKIP, label: w(zh, '不用了', 'No thanks'), kind: 'deny' },
  }
}

// ── the orders around this one ────────────────────────────────────────────

export interface Lineage<T> {
  /** The order that sent this one out (the invoke step's record on the child, lib/orderLineage), and its row when
   *  it is among the listed orders -- null when it is not (older than the list's cut, or scoped out). */
  parent: (Parent & { row: T | null }) | null
  /** The listed orders this one sent out, in the order they arrived. */
  children: T[]
  /** Child orders the row says are running that are not among the listed ones. */
  unlistedChildren: number
}

/** 主工单 / 子工单 of an order, over the rows the page already holds (the old drawer's parentOf / childrenOf). */
export function lineageOf<T extends InboxOrderLike>(order: InboxOrderLike, rows: readonly T[] | null | undefined): Lineage<T> {
  const link = parentOf(order)
  const children = childrenOf(order, rows)
  const running = typeof order.running_child_orders === 'number' && Number.isFinite(order.running_child_orders) ? Math.max(0, order.running_child_orders) : 0
  const listedLive = children.filter(child => !attentionOf(child).ended).length
  return {
    parent: link ? { ...link, row: (rows ?? []).find(item => item.order_id === link.orderId) ?? null } : null,
    children,
    unlistedChildren: Math.max(0, running - listedLive),
  }
}

// ── the record and the context ────────────────────────────────────────────

/** The newest `n` entries first, and how many older ones are folded away. */
export function latestEntries<T extends { at: string }>(entries: readonly T[], n = 5): { shown: T[]; rest: number } {
  const sorted = [...entries].sort((a, b) => String(b.at ?? '').localeCompare(String(a.at ?? '')))
  return { shown: sorted.slice(0, Math.max(0, n)), rest: Math.max(0, sorted.length - n) }
}

export interface IntakeLine { label: string; value: string }

/** 开始材料: what the order was placed with, as the flow's intake fields name it (the order's own hidden fields, the
 *  ones an invoke step writes, are not the brief). At most `limit` lines, each cut short. */
export function intakeSummary(order: InboxOrderLike, fields: ReadonlyArray<{ id: string; label: string }> | undefined, limit = 4): IntakeLine[] {
  const labels = new Map((fields ?? []).map(field => [field.id, field.label]))
  const lines: IntakeLine[] = []
  for (const [key, raw] of Object.entries(order.fields ?? {})) {
    if (key.startsWith('_')) continue
    const value = typeof raw === 'string' ? raw.trim() : typeof raw === 'number' || typeof raw === 'boolean' ? String(raw) : ''
    if (!value) continue
    lines.push({ label: labels.get(key) ?? key, value: value.length > 80 ? `${value.slice(0, 79)}…` : value })
    if (lines.length >= limit) break
  }
  return lines
}

/** 预期交付: the order's own acceptance criteria when it has them, else what the flow's last step promises. */
export function expectedDelivery(order: InboxOrderLike, flowNodes: ReadonlyArray<{ deliverables?: string[] }> | undefined): string[] {
  const criteria = (order.acceptance?.criteria ?? []).map(item => String(item).trim()).filter(Boolean)
  if (criteria.length) return criteria
  const last = flowNodes && flowNodes.length ? flowNodes[flowNodes.length - 1] : undefined
  return (last?.deliverables ?? []).map(item => String(item).trim()).filter(Boolean)
}

// ── the receipt's three states, on a button ───────────────────────────────

/** What a button says for its tracked request: busy = disabled, in the tracker's words; timeout / unknown = enabled
 *  again, the tracker's sentence under it; idle / settled = its own word. */
export function buttonReceipt(entry: { state: string; words: string } | undefined, label: string): { label: string; disabled: boolean; note: string } {
  if (!entry) return { label, disabled: false, note: '' }
  if (entry.state === 'busy') return { label: entry.words || label, disabled: true, note: '' }
  if (entry.state === 'timeout' || entry.state === 'unknown') return { label, disabled: false, note: entry.words }
  return { label, disabled: false, note: '' }
}

/** The words that must never be on the page as a button (the owner's rule: no one-click 同意 / 通过). */
export const FORBIDDEN_BUTTON_WORDS: readonly string[] = ['同意', '通过']
