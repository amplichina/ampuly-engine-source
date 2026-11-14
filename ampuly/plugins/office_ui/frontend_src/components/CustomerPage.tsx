import { useState } from 'react'
import { refusalInWords, useI18n } from '../i18n'
import { customerOrderState, type CustomerOrder } from '../lib/customerPage'
import { classifyPageError } from '../lib/pageError'
import { keyOf, stateOf, type Tracked } from '../lib/requestTracker'
import type { AuthAccount } from './LoginScreen'

interface CustomerPageProps {
  account: AuthAccount
  /** The customer's own orders (customer_orders_view); null until the first answer. */
  orders: CustomerOrder[] | null
  /** Why the list did not come (a code the page classifies), '' when it did. */
  error: string
  connected: boolean
  /** The request tracker's word on every write: 确认收货 is busy, timed out or unknown by it (lib/requestTracker). */
  tracked: Tracked
  /** By order: what the last 确认收货 came back with, in words. */
  acceptErrors: Readonly<Record<string, string>>
  onRetry: () => void
  onAccept: (orderId: string, note: string) => void
  onLogout: () => void
}

// 客户页「我的单」 (S3-6B, policy): the one page a customer account sees. Per order: what it is called, its
// status word, one sentence of where it stands, the deliverables (count and names), the acceptance criteria and the
// round, and -- only when it is the customer's turn -- 确认收货, which records the acceptance and nothing more: the page
// says the owner closes the order. No product mark, no ids, no other customer's anything (the server lists only the
// account's own orders). Every write goes through the request tracker, so the button says busy / no receipt / not sent.
export function CustomerPage({ account, orders, error, connected, tracked, acceptErrors, onRetry, onAccept, onLogout }: CustomerPageProps) {
  const { t, locale, translateMaybe } = useI18n()
  const [notes, setNotes] = useState<Record<string, string>>({})
  const errorKind = error ? classifyPageError(error) : null
  return (
    <div className="person-shell customer-shell">
      <header className="person-topbar">
        <span className="person-who">{account.display_name || account.login}</span>
        <button type="button" className="person-secondary" onClick={onLogout}>{t('employee.logout')}</button>
      </header>
      <main className="person-main">
        <h2 className="person-title">{t('myOrders.title')}</h2>
        {!connected && <p className="person-note" role="status">{t('myOrders.offline')}</p>}
        {errorKind ? (
          <div className={`person-state person-state--${errorKind}`} role="alert" data-error-kind={errorKind}>
            <span><strong>{t(`pageError.${errorKind}`)}</strong><br />{error}</span>
            <button type="button" className="person-secondary" onClick={onRetry} disabled={!connected}>{t('myOrders.retry')}</button>
          </div>
        ) : orders === null ? (
          <p className="person-empty">{t('myOrders.loading')}</p>
        ) : orders.length === 0 ? (
          <p className="person-empty">{t('myOrders.empty')}</p>
        ) : (
          <ul className="person-list customer-orders">
            {orders.map(order => {
              const state = customerOrderState(order)
              const acceptKey = keyOf('customer_accept', order.orderId)
              const acceptState = stateOf(tracked, acceptKey)
              const busy = acceptState === 'busy'
              const trackerWords = tracked[acceptKey]?.words ?? ''
              const refused = acceptErrors[order.orderId] ?? ''
              const note = notes[order.orderId] ?? ''
              return (
                <li key={order.orderId} className="customer-order" data-state={state}>
                  <div className="person-row-head">
                    <strong className="person-row__name">{order.name || t('myOrders.untitled')}</strong>
                    <span className="person-status" data-tone={state === 'waiting_you' ? 'waiting' : undefined}>{translateMaybe('orders.status', order.status)}</span>
                  </div>
                  <p className="customer-stage">{order.stage || t(`myOrders.state.${state}`)}</p>
                  <details className="person-fold" open={state === 'waiting_you'}>
                    <summary>{t('myOrders.deliverables', { n: order.deliverables.length })}</summary>
                    {order.deliverables.length === 0
                      ? <p className="person-muted">{t('myOrders.noDeliverables')}</p>
                      : <ul className="customer-deliverables">{order.deliverables.map((item, index) => <li key={index}>{item.name}{item.at && <time> · {item.at}</time>}</li>)}</ul>}
                  </details>
                  <div className="customer-criteria">
                    <span className="person-muted">{t('myOrders.criteria')} · {t('myOrders.round', { n: order.round })}</span>
                    {order.criteria.length === 0
                      ? <p className="person-muted">{t('myOrders.noCriteria')}</p>
                      : <ul>{order.criteria.map((item, index) => <li key={index}>{item}</li>)}</ul>}
                  </div>
                  {state === 'waiting_you' ? (
                    <div className="customer-accept">
                      <textarea
                        rows={2}
                        placeholder={t('myOrders.acceptNoteHint')}
                        value={note}
                        disabled={!connected || busy}
                        onChange={event => setNotes(prev => ({ ...prev, [order.orderId]: event.target.value }))}
                      />
                      <div className="person-actions">
                        <button
                          type="button"
                          className="person-primary"
                          disabled={!connected || busy}
                          onClick={() => onAccept(order.orderId, note.trim())}
                        >
                          {busy ? t('request.busy') : t('myOrders.accept')}
                        </button>
                        <span className="person-consequence">{t('myOrders.acceptConsequence')}</span>
                      </div>
                      {!busy && trackerWords && <p className="person-error" role="status">{trackerWords}</p>}
                      {refused && <p className="person-error" role="alert">{refusalInWords(locale, 'myOrders.refused', refused)}</p>}
                    </div>
                  ) : (
                    <p className="customer-state-line">{t(`myOrders.state.${state}`)}{order.note && state !== 'in_progress' ? ` · ${order.note}` : ''}</p>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </main>
    </div>
  )
}
