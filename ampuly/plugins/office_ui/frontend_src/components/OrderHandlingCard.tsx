// 经办: who looks after an order, by when, for how much (docs/BACKEND-GAPS-PLAN-2026-09-20, B1/B2). In the order
// drawer. Reads `order.handling`; with `onSave` (the owner's console) it can change it -- each save is one signed
// event on the order's trail, which is why a handover asks for a word of why. The assignee grants nothing: who may
// review or accept is decided where it always was.

import { useEffect, useState } from 'react'

import { HANDLING_CURRENCIES, dueState, formFrom, handlingPatch, moneyText, type OrderHandling } from '../lib/orderHandling'

export function OrderHandlingCard({ zh, handling, ended, employees, onSave, busy = false }: {
  zh: boolean
  handling: OrderHandling | null | undefined
  /** An ended order is never overdue. */
  ended: boolean
  employees: ReadonlyArray<{ employee_id: string; name: string }>
  /** Absent = read only (not the owner's console). */
  onSave?: (patch: Record<string, unknown>) => void
  busy?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState(() => formFrom(handling))
  // A saved change comes back as a new `handling`: show it, and leave the form.
  useEffect(() => { setForm(formFrom(handling)); setEditing(false) }, [handling?.updated_at]) // eslint-disable-line react-hooks/exhaustive-deps
  const nameOf = (id: string) => employees.find(person => person.employee_id === id)?.name || id
  const due = dueState(handling?.due_at, new Date(), zh, ended)
  const quoted = moneyText(handling?.amount_quoted, handling?.currency)
  const received = moneyText(handling?.amount_received, handling?.currency)
  const patch = handlingPatch(handling, form)
  const none = zh ? '未设置' : 'not set'

  return (
    <section className="order-handling" data-testid="order-handling" aria-label={zh ? '经办' : 'Handling'}>
      <header>
        <strong>{zh ? '经办' : 'Handling'}</strong>
        {onSave && !editing && <button type="button" className="shell-btn" data-testid="order-handling-edit" onClick={() => setEditing(true)}>{zh ? '修改 / 转派' : 'Change / hand over'}</button>}
      </header>
      {!editing ? (
        <dl>
          <div><dt>{zh ? '负责人' : 'Assignee'}</dt><dd>{handling?.assignee_employee_id ? nameOf(handling.assignee_employee_id) : none}</dd></div>
          <div data-due={due.kind}><dt>{zh ? '截止' : 'Due'}</dt><dd>{due.text || none}</dd></div>
          <div><dt>{zh ? '报价' : 'Quoted'}</dt><dd>{quoted || none}</dd></div>
          <div><dt>{zh ? '已收款' : 'Received'}</dt><dd>{received || none}</dd></div>
        </dl>
      ) : (
        <form className="order-handling-form" onSubmit={event => { event.preventDefault(); if (patch && onSave && !busy) onSave(patch) }}>
          <label>{zh ? '负责人' : 'Assignee'}
            <select value={form.assignee} onChange={event => setForm({ ...form, assignee: event.target.value })}>
              <option value="">{zh ? '（不指定）' : '(nobody)'}</option>
              {/* Someone assigned earlier who is no longer on the roster stays selectable, by id. */}
              {form.assignee && !employees.some(person => person.employee_id === form.assignee) && <option value={form.assignee}>{form.assignee}</option>}
              {employees.map(person => <option key={person.employee_id} value={person.employee_id}>{person.name}</option>)}
            </select>
          </label>
          <label>{zh ? '截止日期' : 'Due date'}
            <input type="date" value={form.due} onChange={event => setForm({ ...form, due: event.target.value })} />
          </label>
          <label>{zh ? '报价' : 'Quoted'}
            <input type="number" min="0" step="0.01" inputMode="decimal" value={form.quoted} onChange={event => setForm({ ...form, quoted: event.target.value })} />
          </label>
          <label>{zh ? '已收款' : 'Received'}
            <input type="number" min="0" step="0.01" inputMode="decimal" value={form.received} onChange={event => setForm({ ...form, received: event.target.value })} />
          </label>
          <label>{zh ? '币种' : 'Currency'}
            <select value={form.currency} onChange={event => setForm({ ...form, currency: event.target.value })}>
              {HANDLING_CURRENCIES.map(code => <option key={code} value={code}>{code}</option>)}
            </select>
          </label>
          <label className="order-handling-note">{zh ? '说明（写进工单记录，转派时请写原因）' : 'Note (kept on the order’s trail; say why when handing over)'}
            <input type="text" maxLength={400} value={form.note} onChange={event => setForm({ ...form, note: event.target.value })} />
          </label>
          <div className="order-handling-actions">
            <button type="submit" className="shell-btn shell-btn--primary" data-testid="order-handling-save" disabled={!patch || busy}>{busy ? (zh ? '保存中…' : 'Saving…') : (zh ? '保存' : 'Save')}</button>
            <button type="button" className="shell-btn" onClick={() => { setForm(formFrom(handling)); setEditing(false) }}>{zh ? '取消' : 'Cancel'}</button>
          </div>
          <p>{zh ? '负责人只是经办人，不改变谁能审单、谁能验收。金额是记下来的事实，不是财务系统。' : 'The assignee looks after the order; it does not change who may review or accept. Amounts are recorded facts, not an accounting system.'}</p>
        </form>
      )}
    </section>
  )
}
