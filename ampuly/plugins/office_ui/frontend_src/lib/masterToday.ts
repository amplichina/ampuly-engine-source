// 总控's counts (frontend redesign S3-3B, policy; cut to the title line by the policy). Pure
// helpers over the server's `owner_attention_list` reply -- the ONE cross-project judgement of what waits on the owner
// (S3-3A) -- never a rule of the console's own. lib/attention (which added a due-based fourth reason and so counted
// differently from 业务工单's 等你处理) stays only for the rail's dot.
//
// What the page draws from here: the counts 「等你 N 件 · 在做 N 单 · 出错或逾期 N」 in 总控's title line -- their numbers
// come from `counts` ONLY: with no counts nothing is counted, and 0 is never coloured. Each number opens 业务工单 on the
// group with the same filter.
//
// The rows of what waits on the owner and the card beside them (the 「今天」 block) left 总控 Previously, (owner: those
// orders are handled in 业务工单 and on the Feishu cards), and with them the row helpers that lived here. A deep link's
// card is opened in 业务工单 by the order checkpoint_locate names (App.tsx), not selected here.
//
// The reply, as ws_handler.py sends it (order_attention.collect_owner_attention; tests/test_order_attention.py pins it):
//   { counts: { waiting_on_me, in_progress, abnormal_or_overdue }, total, limit, partial_projects, items: [...] }
// -- the counts are made over everything that waits, whatever the limit; `items` is no longer read.

export interface TodayCounts {
  waitingOnMe: number
  inProgress: number
  /** The server's `abnormal_or_overdue`: the inbox's 逾期或异常 group, which is what the title line's 出错 opens. */
  errors: number
}

export type TodayTone = '' | 'red' | 'amber'

/** The inbox group each number opens -- the same filter that counted it (lib/ordersInbox INBOX_STATES). */
export type TodayGroup = 'mine' | 'live' | 'risk'

export interface SentencePart {
  group: TodayGroup
  n: number
  text: string
  tone: TodayTone
}

type Rec = Record<string, unknown>

function record(value: unknown): Rec | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Rec) : null
}

function text(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

function count(value: unknown): number | null {
  const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN
  return Number.isFinite(n) && Math.trunc(n) >= 0 ? Math.trunc(n) : null
}

// ── the numbers ────────────────────────────────────────────────────────────

/** The three counts, or null when the reply carries none (an older server, or nothing back yet): then nothing is
 *  counted. `waiting_on_me` is the field the design names; without it there are no numbers at all. */
export function todayCountsFrom(payload: unknown): TodayCounts | null {
  const counts = record(record(payload)?.counts)
  if (!counts) return null
  const waiting = count(counts.waiting_on_me ?? counts.waitingOnMe)
  if (waiting === null) return null
  return {
    waitingOnMe: waiting,
    inProgress: count(counts.in_progress ?? counts.inProgress ?? counts.working) ?? 0,
    // The server's third number is abnormal_or_overdue (order_attention.count_standing); the older names stay readable.
    errors: count(counts.abnormal_or_overdue ?? counts.errors ?? counts.error ?? counts.failed ?? counts.abnormal) ?? 0,
  }
}

/** 「等你 N 件 · 在做 N 单 · 出错或逾期 N」: each part is a link into the inbox group with the same filter. Only 出错 is
 *  coloured, and only when it is not 0. */
export function statusSentence(counts: TodayCounts, zh: boolean): SentencePart[] {
  return [
    { group: 'mine', n: counts.waitingOnMe, text: zh ? `等你 ${counts.waitingOnMe} 件` : `${counts.waitingOnMe} waiting on you`, tone: '' },
    { group: 'live', n: counts.inProgress, text: zh ? `在做 ${counts.inProgress} 单` : `${counts.inProgress} in progress`, tone: '' },
    // The number is the server's abnormal_or_overdue (the inbox's 逾期或异常 group this part opens): the word says both.
    { group: 'risk', n: counts.errors, text: zh ? `出错或逾期 ${counts.errors}` : `${counts.errors} in trouble or overdue`, tone: counts.errors > 0 ? 'red' : '' },
  ]
}

/** How many projects the server could not read (`partial_projects`): their waiting is unknown, not zero, and the
 *  title line says so beside the numbers. */
export function todayPartial(payload: unknown): number {
  const listed = record(payload)?.partial_projects
  return Array.isArray(listed) ? listed.filter(entry => text(entry)).length : 0
}

/** How many rows the console asks for: one, the least the server sends (order_attention.clamp_attention_limit). The
 *  title line reads only the counts, which the server makes over everything that waits whatever the limit; the twenty
 *  rows it used to ask for were for the 今天 block, which left 总控 on 2026-09-29. */
export const TODAY_FETCH_LIMIT = 1

/** The server refused the read because it does not know the action (an older server): not an error to show, the page
 *  simply counts nothing. The same for checkpoint_locate: a deep link's card then lands on 业务工单 · 等你处理. */
export function todayUnsupported(payload: unknown): boolean {
  const root = record(payload)
  return !!root && root.ok === false && text(root.error) === 'unknown_message_type'
}
