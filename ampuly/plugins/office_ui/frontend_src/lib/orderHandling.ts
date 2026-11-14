// Who handles an order, by when, for how much -- the reading side of `order_handling_update` (B1/B2 of
// docs/BACKEND-GAPS-PLAN-2026-09-20). Pure. The facts are the order's own (`order.handling`, null until someone sets
// one); nothing here invents a value: no assignee means no assignee, no due date means no due date.

export type OrderHandling = {
  assignee_employee_id?: string
  due_at?: string
  amount_quoted?: number
  amount_received?: number
  currency?: string
  updated_at?: string
  updated_by?: string
}

export const HANDLING_CURRENCIES = ['CNY', 'USD', 'EUR', 'HKD', 'JPY'] as const

export type DueKind = 'none' | 'overdue' | 'today' | 'soon' | 'later'
export type DueState = { kind: DueKind; days: number; text: string }

const DAY = 86_400_000

/** How far off the due date is, by calendar day in the viewer's own time zone. `soon` = within three days. An order
 *  that has ended is never overdue: the caller passes `ended`. */
export function dueState(dueAt: string | undefined | null, now: Date, zh: boolean, ended = false): DueState {
  const raw = (dueAt ?? '').trim()
  if (!raw) return { kind: 'none', days: 0, text: '' }
  const [y, m, d] = raw.slice(0, 10).split('-').map(Number)
  if (!y || !m || !d) return { kind: 'none', days: 0, text: raw }
  const due = new Date(y, m - 1, d).getTime()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const days = Math.round((due - today) / DAY)
  const date = `${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`
  if (ended) return { kind: 'later', days, text: date }
  if (days < 0) return { kind: 'overdue', days, text: zh ? `逾期 ${-days} 天（${date}）` : `${-days}d overdue (${date})` }
  if (days === 0) return { kind: 'today', days, text: zh ? `今天到期（${date}）` : `due today (${date})` }
  if (days <= 3) return { kind: 'soon', days, text: zh ? `${days} 天后（${date}）` : `in ${days}d (${date})` }
  return { kind: 'later', days, text: zh ? `${days} 天后（${date}）` : `in ${days}d (${date})` }
}

const SIGNS: Record<string, string> = { CNY: '¥', USD: '$', EUR: '€', HKD: 'HK$', JPY: 'JP¥' }

/** An amount as recorded, with its currency; '' when none was recorded (0 is a recorded amount). */
export function moneyText(amount: number | undefined | null, currency: string | undefined | null): string {
  if (typeof amount !== 'number' || !Number.isFinite(amount)) return ''
  const code = (currency ?? '').trim().toUpperCase() || 'CNY'
  return `${SIGNS[code] ?? `${code} `}${amount.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`
}

export type HandlingForm = { assignee: string; due: string; quoted: string; received: string; currency: string; note: string }

export function formFrom(handling: OrderHandling | null | undefined): HandlingForm {
  return {
    assignee: handling?.assignee_employee_id ?? '',
    due: (handling?.due_at ?? '').slice(0, 10),
    quoted: typeof handling?.amount_quoted === 'number' ? String(handling.amount_quoted) : '',
    received: typeof handling?.amount_received === 'number' ? String(handling.amount_received) : '',
    currency: handling?.currency ?? 'CNY',
    note: '',
  }
}

/** Only what changed goes to the server ('' clears a value); null when nothing did, or an amount is not a number.
 *  The server refuses an unchanged patch, so the page does not send one. */
export function handlingPatch(current: OrderHandling | null | undefined, form: HandlingForm): Record<string, unknown> | null {
  const before = formFrom(current)
  const patch: Record<string, unknown> = {}
  if (form.assignee.trim() !== before.assignee) patch.assignee_employee_id = form.assignee.trim()
  if (form.due.trim() !== before.due) patch.due_at = form.due.trim()
  for (const [key, field] of [['quoted', 'amount_quoted'], ['received', 'amount_received']] as const) {
    const text = form[key].trim()
    if (text === before[key]) continue
    if (text === '') { patch[field] = ''; continue }
    const value = Number(text)
    if (!Number.isFinite(value) || value < 0) return null
    if (String(value) !== before[key]) patch[field] = value
  }
  const hasAmount = form.quoted.trim() !== '' || form.received.trim() !== ''
  if (hasAmount && form.currency !== (current?.currency ?? '')) patch.currency = form.currency
  if (Object.keys(patch).length === 0) return null
  if (form.note.trim()) patch.note = form.note.trim()
  return patch
}

/** Across the orders given: what was quoted and received, per currency (amounts in different currencies are never
 *  added together). Only orders that recorded an amount count. */
export function moneyTotals(orders: ReadonlyArray<{ handling?: OrderHandling | null }> | null | undefined): Array<{ currency: string; quoted: number; received: number; orders: number }> {
  const byCurrency = new Map<string, { currency: string; quoted: number; received: number; orders: number }>()
  for (const order of orders ?? []) {
    const h = order.handling
    if (!h || (typeof h.amount_quoted !== 'number' && typeof h.amount_received !== 'number')) continue
    const currency = (h.currency ?? '').trim().toUpperCase() || 'CNY'
    const row = byCurrency.get(currency) ?? { currency, quoted: 0, received: 0, orders: 0 }
    row.quoted += h.amount_quoted ?? 0
    row.received += h.amount_received ?? 0
    row.orders += 1
    byCurrency.set(currency, row)
  }
  return [...byCurrency.values()].sort((a, b) => (a.currency < b.currency ? -1 : 1))
}
