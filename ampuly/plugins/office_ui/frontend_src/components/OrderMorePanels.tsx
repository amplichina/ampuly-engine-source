// What sits under 「更多」 on one order of the inbox (frontend redesign step 3, first page): everything the owner could
// do on the old orders table that is not the one thing the 「现在」 block asks for. Each panel is the old row tool lifted
// out of WorkflowsPage's expanded rows with its test ids and class names kept, so the e2e probe and the old tests
// read the same structure: the allowance (grant / ceilings / raise / lift / the held call's answer) with the cost,
// saving and usage figures; the solution blueprint; the delivery plan editor; 取消整单 with its reason and cascade;
// 打回 for an order not taken yet; the whole timeline with its integrity line.
//
// Nothing here sends anything itself: every write goes through the callbacks App hands in, and a write that is out
// says so on its button in the tracker's words.

import { useState } from 'react'
import { useI18n } from '../i18n'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import { timelineEventName, timelineEventTime } from '../lib/orderEventNames'
import { buttonReceipt } from '../lib/ordersInbox'
import { integrityLine } from '../lib/timelineIntegrity'
import { BudgetLimitInputs, budgetLimitsFromDraft, type BudgetDraft } from './BudgetLimitInputs'
import type { OrderMetrics, OrderTimeline, OrderUsage, RunBudget, SolutionBlueprint, WorkOrderSummary } from './WorkflowsPage'

type Translate = ReturnType<typeof useI18n>['t']
type Receipt = { state: string; words: string } | undefined

const percent = (value: number | null | undefined) => (value == null ? '—' : `${Math.round(value * 1000) / 10}%`)

/** Why an allowance waits for an answer, in the owner's words (the server writes the reasons it knows in English). */
export function budgetHoldReason(reason: string, t: Translate): string {
  const said = reason.trim()
  if (/^held by a (console|process) that stopped/i.test(said)) return t('orders.budget.reason.consoleStopped')
  if (/^model outcome uncertain/i.test(said)) return t('orders.budget.reason.modelUncertain')
  if (/^dispatch outcome not established/i.test(said)) return t('orders.budget.reason.dispatchUnknown')
  const heldFor = /^held for more than (\d+) minutes?$/i.exec(said)
  if (heldFor) return t('orders.budget.reason.heldTooLong', { n: heldFor[1] })
  if (/^outcome unknown$/i.test(said)) return t('orders.budget.reason.outcomeUnknown')
  return said
}

const BUDGET_COUNTED_KINDS = new Set(['image', 'model_call', 'retry', 'cost_cny'])

/** How many of what a held call would have spent: 「1 次模型调用」, never 「1 model_call」. */
export function budgetAmountInWords(amount: number, kind: string, t: Translate): string {
  const key = (BUDGET_COUNTED_KINDS.has(kind) ? `orders.budget.count.${kind}` : 'orders.budget.count.other') as Parameters<Translate>[0]
  return t(key, { n: amount })
}

// ── 预算与费用 ──────────────────────────────────────────────────────────────

export interface OrderBudgetPanelProps {
  order: WorkOrderSummary
  connected?: boolean
  /** null / undefined = the allowances were not read yet: nothing is claimed about this order's. */
  budgets?: RunBudget[] | null
  pendingReservations?: Array<{ reservation_id: string; budget_id: string; kind: string; amount: number; note: string }>
  onResolveBudget?: (reservationId: string, happened: boolean) => void
  onCreateBudget?: (orderId: string, limits: Record<string, number>) => boolean
  onRaiseBudget?: (orderId: string, limits: Record<string, number>) => boolean
  onCloseBudget?: (orderId: string, reason: string) => boolean
  /** The order whose allowance write is out: its confirms wait for the receipt. */
  budgetBusyOrderId?: string | null
  orderMetrics?: OrderMetrics[]
  orderUsage?: OrderUsage[]
  orderMetricsTruncated?: boolean
}

export function OrderBudgetPanel({ order, connected = true, budgets, pendingReservations = [], onResolveBudget, onCreateBudget, onRaiseBudget, onCloseBudget, budgetBusyOrderId = null, orderMetrics = [], orderUsage = [], orderMetricsTruncated = false }: OrderBudgetPanelProps) {
  const { t } = useI18n()
  const [grantDraft, setGrantDraft] = useState<BudgetDraft>({})
  const [grantOpen, setGrantOpen] = useState(false)
  const [closing, setClosing] = useState<{ open: boolean; reason: string }>({ open: false, reason: '' })
  const [raising, setRaising] = useState<{ open: boolean; draft: Record<string, string> }>({ open: false, draft: {} })
  const busy = budgetBusyOrderId === order.order_id
  const own = (budgets ?? []).filter(item => item.order_id === order.order_id)
  const ended = ['closed', 'rejected'].includes(order.status)
  const running = Boolean(order.launched_task_id) && !ended
  const busyWord = busy ? t('request.busy') : ''
  const grantForm = (confirmTestId: string, confirmKey: 'orders.budget.grantConfirm' | 'orders.budget.setCeilingConfirm', hintKey: 'orders.budget.grantHint' | 'orders.budget.grantRunningHint' | 'orders.budget.setCeilingHint') => {
    const allowance = budgetLimitsFromDraft(grantDraft)
    const empty = Object.keys(allowance.limits).length === 0
    return (
      <>
        <BudgetLimitInputs draft={grantDraft} onChange={setGrantDraft} />
        <span className="settings-note">{t(hintKey)}</span>
        <button type="button" className="workflows-invoke" data-testid={confirmTestId} disabled={!connected || busy || empty || allowance.invalid.length > 0}
          onClick={() => { if (onCreateBudget?.(order.order_id, allowance.limits)) { setGrantOpen(false); setGrantDraft({}) } }}>
          {busyWord || t(confirmKey)}
        </button>
        <button type="button" className="workflows-cancel" onClick={() => { setGrantOpen(false); setGrantDraft({}) }}>{t('workflows.cancel')}</button>
      </>
    )
  }
  return (
    <div className="inbox-budget" data-testid="order-budget-panel">
      {orderMetrics.filter(metric => metric.order_id === order.order_id).map(metric => (
        <div className="orders-budget" data-testid="order-metrics" key={`metrics-${metric.order_id}`}>
          <span className="orders-budget-kind" data-testid="order-saving">
            {t('orders.metrics.saving')}{' '}
            {metric.saving.state === 'calculated'
              ? `${metric.saving.bound === 'at_most' ? t('orders.metrics.atMost') : ''}¥${(Number(metric.saving.value ?? 0) / 100).toFixed(2)}`
              : `${t('orders.metrics.notCalculated')} · ${t('orders.metrics.missing')} ${metric.saving.missing.join('、')}`}
          </span>
          {metric.saving.rests_on.length > 0 && <span className="settings-note" data-testid="order-saving-rests-on">{t('orders.metrics.restsOn', { names: metric.saving.rests_on.join('、') })}</span>}
          {orderUsage.filter(usage => usage.order_id === order.order_id && usage.total.calls > 0).map(usage => (
            <span className="orders-budget-kind" data-testid="order-usage-by-purpose" key={`usage-${usage.order_id}`}
              title={[...usage.by_purpose.map(row => `${t(`orders.usage.purpose.${row.purpose}` as Parameters<typeof t>[0])}: ${row.calls} × · ${row.tokens_in} + ${row.tokens_out}`), ...usage.limits].join('\n')}>
              {t('orders.usage.line', {
                calls: usage.total.calls, tokens: usage.total.tokens_in + usage.total.tokens_out,
                judgementCalls: usage.judgement.calls, share: percent(usage.judgement.share_of_tokens), outputShare: percent(usage.judgement.output_share_of_judgement),
              })}
            </span>
          ))}
          <span className="orders-budget-kind" data-testid="order-sla">
            {t('orders.metrics.sla')}{' '}
            {metric.sla.state === 'not_calculated'
              ? `${t('orders.metrics.notCalculated')} · ${t('orders.metrics.missing')} ${metric.sla.missing.join('、')}`
              : t(`orders.metrics.sla.${metric.sla.state}` as Parameters<typeof t>[0], { hours: String(metric.sla.remaining_hours ?? metric.sla.elapsed_hours ?? 0) })}
          </span>
        </div>
      ))}
      {orderMetricsTruncated && <p className="orders-truncated" role="status" data-testid="order-metrics-truncated">{t('inbox.metricsTruncated')}</p>}
      {/* A closed allowance is history, not a ceiling: the order is offered a new one. Only once the budgets are known. */}
      {onCreateBudget && budgets != null && !own.some(item => item.state !== 'closed') && !ended && (
        <div className="orders-budget" data-testid="order-budget-grant" data-order-id={order.order_id}>
          <span className="settings-note">{t('orders.budget.unlimited')}</span>
          {!grantOpen
            ? <button type="button" className="workflows-cancel" data-testid="order-budget-open" disabled={!connected} onClick={() => setGrantOpen(true)}>{t('orders.budget.grant')}</button>
            : grantForm('order-budget-confirm', 'orders.budget.grantConfirm', order.launched_task_id ? 'orders.budget.grantRunningHint' : 'orders.budget.grantHint')}
        </div>
      )}
      {own.map(budget => (
        <div className="orders-budget" key={budget.budget_id} data-testid="order-budget" data-order-id={order.order_id}>
          <span className={`capability-pill capability-pill--${budget.state === 'open' ? 'ok' : budget.state === 'closed' ? 'muted' : 'warn'}`}>
            {t(`orders.budget.${budget.state}` as Parameters<typeof t>[0])}
          </span>
          {Object.keys(budget.limits).length === 0 && <span className="settings-note">{t('orders.budget.unlimited')}</span>}
          {/* Every order is held from its launch, without a ceiling unless the owner sets one: ceilings are set on
              that same allowance, in place. */}
          {onCreateBudget && budget.state === 'open' && Object.keys(budget.limits).length === 0 && (!grantOpen
            ? <button type="button" className="workflows-cancel" data-testid="order-budget-set-ceiling-open" disabled={!connected} onClick={() => setGrantOpen(true)}>{t('orders.budget.setCeiling')}</button>
            : <span className="orders-budget-resolve" data-testid="order-budget-set-ceiling">{grantForm('order-budget-set-ceiling-confirm', 'orders.budget.setCeilingConfirm', 'orders.budget.setCeilingHint')}</span>)}
          {Object.entries(budget.limits).map(([kind, limit]) => (
            <span className="orders-budget-kind" key={kind}>
              {t(`orders.budget.kind.${kind}` as Parameters<typeof t>[0])} {budget.consumed[kind] ?? 0}
              {(budget.held[kind] ?? 0) > 0 && ` (+${budget.held[kind]} ${t('orders.budget.held')})`} / {limit}
            </span>
          ))}
          {onRaiseBudget && budget.state === 'open' && Object.keys(budget.limits).some(kind => kind !== 'cost_cny') && (!raising.open ? (
            <button type="button" className="workflows-cancel" data-testid="order-budget-raise-open" disabled={!connected} onClick={() => setRaising({ open: true, draft: {} })}>{t('orders.budget.raise')}</button>
          ) : (() => {
            // Only up, and only ceilings that exist: the server refuses anything else, and so does this form.
            const raised: Record<string, number> = {}
            let invalid = false
            for (const [kind, text] of Object.entries(raising.draft)) {
              if (!text.trim()) continue
              const value = Number(text)
              if (!Number.isInteger(value) || value <= (budget.limits[kind] ?? 0)) invalid = true
              else raised[kind] = value
            }
            return (
              <span className="orders-budget-resolve" data-testid="order-budget-raise">
                {Object.entries(budget.limits).filter(([kind]) => kind !== 'cost_cny').map(([kind, limit]) => (
                  <label key={kind} className="orders-budget-kind">
                    {t(`orders.budget.kind.${kind}` as Parameters<typeof t>[0])} {limit} →{' '}
                    <input className="capability-refresh-btn" inputMode="numeric" style={{ width: '5em' }} data-testid={`order-budget-raise-${kind}`} value={raising.draft[kind] ?? ''}
                      onChange={event => setRaising({ open: true, draft: { ...raising.draft, [kind]: event.target.value } })} />
                  </label>
                ))}
                <button type="button" className="workflows-invoke" data-testid="order-budget-raise-confirm" disabled={!connected || busy || invalid || Object.keys(raised).length === 0}
                  onClick={() => { if (onRaiseBudget(order.order_id, raised)) setRaising({ open: false, draft: {} }) }}>
                  {busyWord || t('orders.budget.raiseConfirm')}
                </button>
                <button type="button" className="workflows-cancel" onClick={() => setRaising({ open: false, draft: {} })}>{t('workflows.cancel')}</button>
                <span className="settings-note">{t('orders.budget.raiseHint')}</span>
              </span>
            )
          })())}
          {/* A running order is never left unheld: "close" lifts its ceilings in place; an order not running closes as before. */}
          {onCloseBudget && budget.state !== 'closed' && !(running && Object.keys(budget.limits).length === 0) && (!closing.open ? (
            <button type="button" className="workflows-cancel" data-testid="order-budget-close-open" disabled={!connected} onClick={() => setClosing({ open: true, reason: '' })}>
              {t(running ? 'orders.budget.lift' : 'orders.budget.close')}
            </button>
          ) : (
            <span className="orders-budget-resolve" data-testid="order-budget-close">
              <input className="capability-refresh-btn" placeholder={t(running ? 'orders.budget.liftWhy' : 'orders.budget.closeWhy')} value={closing.reason} onChange={event => setClosing({ open: true, reason: event.target.value })} />
              <button type="button" className="workflows-cancel" data-testid="order-budget-close-confirm" disabled={!connected || busy || closing.reason.trim().length < 2}
                onClick={() => { if (onCloseBudget(order.order_id, closing.reason.trim())) setClosing({ open: false, reason: '' }) }}>
                {busyWord || t(running ? 'orders.budget.liftConfirm' : 'orders.budget.closeConfirm')}
              </button>
              <button type="button" className="workflows-cancel" onClick={() => setClosing({ open: false, reason: '' })}>{t('workflows.cancel')}</button>
              <span className="settings-note">{t(running ? 'orders.budget.liftHint' : 'orders.budget.closeHint')}</span>
            </span>
          ))}
          {budget.state === 'pending_verification' && (
            <span className="orders-budget-pending">
              <span className="workflows-error">{t('orders.budget.pendingHint')}{budget.state_reason ? ` · ${budgetHoldReason(budget.state_reason, t)}` : ''}</span>
              {onResolveBudget && pendingReservations.filter(item => item.budget_id === budget.budget_id).map(reservation => (
                <span key={reservation.reservation_id} className="orders-budget-resolve">
                  <button type="button" className="workflows-cancel" data-testid="budget-resolve-happened" disabled={!connected} onClick={() => onResolveBudget(reservation.reservation_id, true)}>
                    {t('orders.budget.happened', { what: budgetAmountInWords(reservation.amount, reservation.kind, t) })}
                  </button>
                  <button type="button" className="workflows-cancel" data-testid="budget-resolve-not-happened" disabled={!connected} onClick={() => onResolveBudget(reservation.reservation_id, false)}>
                    {t('orders.budget.notHappened')}
                  </button>
                </span>
              ))}
            </span>
          )}
        </div>
      ))}
      {budgets == null && <p className="settings-note" data-testid="order-budget-unread">{t('inbox.budgetUnread')}</p>}
      {budgets != null && own.length === 0 && (!onCreateBudget || ended) && <p className="settings-note">{t('inbox.budgetNone')}</p>}
    </div>
  )
}

// ── 方案蓝图 ────────────────────────────────────────────────────────────────

export function OrderBlueprintBar({ order, connected = true, blueprints = [], onSaveBlueprint, onApplyBlueprint }: {
  order: WorkOrderSummary
  connected?: boolean
  blueprints?: SolutionBlueprint[]
  onSaveBlueprint?: (orderId: string, name: string) => void
  onApplyBlueprint?: (blueprintId: string, orderId: string) => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [choice, setChoice] = useState('')
  const others = blueprints.filter(item => item.source_order_id !== order.order_id)
  return (
    <div className="orders-blueprint-bar">
      <span className="settings-note">{t('orders.blueprintHint')}</span>
      {onSaveBlueprint && (
        <span className="orders-blueprint-save">
          <input aria-label={t('orders.blueprintSave')} placeholder={t('orders.blueprintNamePlaceholder')} value={name} onChange={event => setName(event.target.value)} />
          <button type="button" className="workflows-cancel" data-testid="blueprint-save" disabled={!connected || !name.trim()} onClick={() => { onSaveBlueprint(order.order_id, name.trim()); setName('') }}>
            {t('orders.blueprintSave')}
          </button>
        </span>
      )}
      {onApplyBlueprint && others.length > 0 && (
        <span className="orders-blueprint-apply">
          <select aria-label={t('orders.blueprintApply')} data-testid="blueprint-choice" value={choice} onChange={event => setChoice(event.target.value)}>
            <option value="">{t('orders.blueprintPick')}</option>
            {others.map(item => <option key={item.blueprint_id} value={item.blueprint_id}>{item.name}{item.source_customer_id ? ` · ${item.source_customer_id}` : ''}</option>)}
          </select>
          <button type="button" className="workflows-cancel" data-testid="blueprint-apply" disabled={!connected || !choice} onClick={() => onApplyBlueprint(choice, order.order_id)}>
            {t('orders.blueprintApply')}
          </button>
        </span>
      )}
    </div>
  )
}

// ── 交付方案 (FDE) ─────────────────────────────────────────────────────────

type FdeDraft = { asIs: string; toBe: string; uat: string; sla: string; rollback: string; roi: string }

function fdeDraftOf(order: WorkOrderSummary): FdeDraft {
  const plane = order.fde_control_plane
  const stringify = (value: unknown) => value && typeof value === 'object' ? JSON.stringify(value, null, 2) : ''
  return { asIs: plane?.as_is ?? '', toBe: plane?.to_be ?? '', uat: stringify(plane?.uat), sla: stringify(plane?.sla), rollback: stringify(plane?.rollback), roi: stringify(plane?.roi) }
}

export function OrderFdeEditor({ order, connected = true, busy = false, onUpdateFdeControlPlane, onClose }: {
  order: WorkOrderSummary
  connected?: boolean
  busy?: boolean
  onUpdateFdeControlPlane: (orderId: string, patch: Record<string, unknown>) => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const [draft, setDraft] = useState<FdeDraft>(() => fdeDraftOf(order))
  const [error, setError] = useState('')
  const submit = () => {
    const patch: Record<string, unknown> = {}
    if (draft.asIs.trim()) patch.as_is = draft.asIs.trim()
    if (draft.toBe.trim()) patch.to_be = draft.toBe.trim()
    for (const key of ['uat', 'sla', 'rollback', 'roi'] as const) {
      const raw = draft[key].trim()
      if (!raw) continue
      try {
        const parsed = JSON.parse(raw)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('object required')
        patch[key] = parsed
      } catch {
        setError(t('inbox.fde.invalidJson', { field: t(`orders.fde.${key}` as Parameters<typeof t>[0]) }))
        return
      }
    }
    if (Object.keys(patch).length === 0) { setError(t('inbox.fde.nothingToSave')); return }
    setError('')
    onUpdateFdeControlPlane(order.order_id, patch)
  }
  return (
    <div className="workflows-builder-form orders-fde-editor" role="form" aria-label={t('orders.fde.edit')}>
      <strong>{t('orders.fde.title')}</strong>
      <textarea aria-label={t('orders.fde.asIs')} rows={2} value={draft.asIs} placeholder={t('orders.fde.asIs')} onChange={event => setDraft(prev => ({ ...prev, asIs: event.target.value }))} />
      <textarea aria-label={t('orders.fde.toBe')} rows={2} value={draft.toBe} placeholder={t('orders.fde.toBe')} onChange={event => setDraft(prev => ({ ...prev, toBe: event.target.value }))} />
      {(['uat', 'sla', 'rollback', 'roi'] as const).map(key => (
        <textarea key={key} aria-label={t(`orders.fde.${key}` as Parameters<typeof t>[0])} rows={2} value={draft[key]} placeholder={t(`orders.fde.${key}` as Parameters<typeof t>[0])} onChange={event => setDraft(prev => ({ ...prev, [key]: event.target.value }))} />
      ))}
      {error && <span className="workflows-error">{error}</span>}
      <div className="workflows-form-actions">
        <button type="button" className="workflows-cancel" disabled={busy} onClick={onClose}>{t('workflows.cancel')}</button>
        <button type="button" className="workflows-invoke" disabled={!connected || busy} onClick={submit}>{busy ? t('orders.fde.saving') : t('orders.fde.save')}</button>
      </div>
    </div>
  )
}

// ── 取消整单 ────────────────────────────────────────────────────────────────

export function OrderCancelForm({ order, connected = true, receipt, onCancelOrder }: {
  order: WorkOrderSummary
  connected?: boolean
  receipt?: Receipt
  /** Returns whether the cancel was really sent; the reason stays in the box when it was not. */
  onCancelOrder: (orderId: string, reason: string, cascadeChildren: boolean) => boolean
}) {
  const { t } = useI18n()
  const [reason, setReason] = useState('')
  const [cascade, setCascade] = useState(true)
  const children = order.running_child_orders ?? 0
  const said = buttonReceipt(receipt, t('inbox.more.cancel'))
  return (
    <div className="inbox-cancel" data-testid="order-cancel">
      <textarea className="orders-review-note" rows={2} aria-label={t('inbox.cancel.reason')} placeholder={t('inbox.cancel.reasonHint')} value={reason} disabled={!connected} onChange={event => setReason(event.target.value)} />
      {children > 0 && (
        <label className="orders-cancel-cascade" data-testid="order-cancel-cascade" title={t('orders.cancel.cascadeHint')}>
          {/* On unless the owner unticks it (policy): cancelling the order that started the others means
              the whole piece of business is off. The server still cascades only when asked. */}
          <input type="checkbox" disabled={!connected} checked={cascade} onChange={event => setCascade(event.target.checked)} />
          <span>{t('orders.cancel.cascade', { count: children })}</span>
        </label>
      )}
      <button type="button" className="workflows-cancel" data-testid="order-cancel-confirm" disabled={!connected || said.disabled || reason.trim().length < 2} title={t('inbox.cancel.consequence')}
        onClick={() => { if (onCancelOrder(order.order_id, reason.trim(), children > 0 && cascade)) { setReason(''); setCascade(true) } }}>
        {said.label}
      </button>
      <small className="inbox-consequence">{t('inbox.cancel.consequence')}</small>
      {said.note && <span className="inbox-receipt-note" role="status">{said.note}</span>}
    </div>
  )
}

// ── 打回 (an order not taken yet) ──────────────────────────────────────────

export function OrderRejectForm({ order, connected = true, receipt, onReviewOrder }: {
  order: WorkOrderSummary
  connected?: boolean
  receipt?: Receipt
  onReviewOrder: (orderId: string, decision: 'approve' | 'reject', note: string) => void
}) {
  const { t } = useI18n()
  const [note, setNote] = useState('')
  const said = buttonReceipt(receipt, t('inbox.more.reject'))
  return (
    <div className="inbox-reject" data-testid="order-reject">
      <textarea className="orders-review-note" rows={2} aria-label={t('inbox.reject.note')} placeholder={t('inbox.reject.note')} value={note} disabled={!connected} onChange={event => setNote(event.target.value)} />
      <button type="button" className="workflows-cancel" data-testid="order-reject-confirm" disabled={!connected || said.disabled || !note.trim()} onClick={() => onReviewOrder(order.order_id, 'reject', note.trim())}>
        {said.label}
      </button>
      <small className="inbox-consequence">{t('inbox.reject.consequence')}</small>
      {said.note && <span className="inbox-receipt-note" role="status">{said.note}</span>}
    </div>
  )
}

// ── 时间线全部 ──────────────────────────────────────────────────────────────

export function OrderTimelinePanel({ order, timeline, error }: { order: WorkOrderSummary; timeline: OrderTimeline | null; error?: string | null }) {
  const { t, locale } = useI18n()
  const zh = locale.startsWith('zh')
  const mine = timeline && timeline.order_id === order.order_id ? timeline : null
  return (
    <div className="orders-timeline-panel" data-order-timeline={order.order_id}>
      <div className="orders-timeline-head">
        <strong>{t('orders.timeline')}</strong>
        {error ? (
          <span className={`orders-timeline-error orders-timeline-error--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
            {t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}: {error}
          </span>
        ) : mine ? (
          <span className="orders-timeline-counts">
            {t('orders.timelineCounts', { order: mine.counts.order, gate: mine.counts.gate, runtime: mine.counts.runtime, effect: mine.counts.effect ?? 0, browser: mine.counts.browser ?? 0 })}
            {' · '}{mine.business_chain_verified ? t('orders.auditVerified') : t('orders.auditInvalid')}
            {' · '}{t('orders.timelineRuntimeUnchained')}
            {(() => {
              const line = integrityLine(mine.integrity)
              return line ? <span data-testid="timeline-integrity">{' · '}{t(line.key as Parameters<typeof t>[0], line.params)}</span> : null
            })()}
          </span>
        ) : (
          <span className="orders-timeline-counts">{t('orders.timelineLoading')}</span>
        )}
      </div>
      {mine && !error && (mine.entries.length === 0 ? (
        <div className="capability-muted">{t('orders.timelineEmpty')}</div>
      ) : (
        <ol className="orders-timeline-list">
          {mine.entries.map((entry, index) => (
            <li key={`${entry.at}-${entry.source}-${index}`} className={`orders-timeline-item orders-timeline-item--${entry.source}`} data-timeline-source={entry.source}>
              <span className="orders-timeline-at" title={timelineEventTime(entry.at, true)}>{timelineEventTime(entry.at)}</span>
              <span className={`capability-pill capability-pill--${entry.source === 'order' || entry.source === 'gate' ? 'ok' : entry.source === 'effect' ? 'warn' : 'muted'}`}>{t(`orders.timelineSource.${entry.source}` as Parameters<typeof t>[0])}</span>
              <span className="orders-timeline-kind" data-kind={entry.kind}>{timelineEventName(entry.kind, zh)}</span>
              <span className="orders-timeline-actor">{entry.actor}</span>
              <span className="orders-timeline-detail">{entry.detail}</span>
            </li>
          ))}
        </ol>
      ))}
    </div>
  )
}
