// 人员与职责 · 联系方式 (4.7-A07): one row per member of the company -- the channels they are bound on, and, for the
// owner, a way to bind a Feishu open_id or an email. A bind is asked twice (the value, then 确认绑定) because an
// inbox bound to the wrong person routes their messages to somebody else. The server refuses anything but the owner.
import { useEffect, useState } from 'react'

import type { OrgEmployee } from '../types/visual'
import {
  BIND_CHANNELS,
  type BindChannel,
  type BookContact,
  bindErrorText,
  channelLabel,
  contactStatus,
  contactStatusText,
  humanMembers,
  identitiesOf,
  identityProblem,
  orgContactOf,
} from '../lib/contactBook'

/**
 * Beside the person a step or a gate names on the builder (负责 / 签字人): whether they can be reached, and when they
 * cannot, the way to 人员与职责 to bind them. Nothing for the owner, nobody, or while the book is still being read.
 */
export function ContactHint({ personId, employees, contacts, zh, onOpenPeople }: {
  personId: string
  employees: readonly OrgEmployee[]
  contacts: readonly BookContact[] | null
  zh: boolean
  onOpenPeople?: () => void
}) {
  const status = contactStatus(personId, employees, contacts)
  const words = contactStatusText(status, zh)
  if (!words) return null
  return (
    <small className="wf-node-edit-roster-note" data-testid="node-contact-status" data-status={status}>
      {words}
      {status === 'none' && onOpenPeople && (
        <> <button type="button" className="wf-skill-link" data-testid="node-contact-bind" onClick={onOpenPeople}>{zh ? '去绑定 →' : 'Bind one →'}</button></>
      )}
    </small>
  )
}

export interface ContactBindState {
  /** The member a bind was just sent for ('' = none in flight). */
  busyEmployeeId: string
  /** The last refused bind, for the member it was about. */
  error: { employeeId: string; error: string } | null
}

export function PeopleContacts({ employees, contacts, zh, connected = true, bind = { busyEmployeeId: '', error: null }, onBind }: {
  employees: readonly OrgEmployee[]
  /** The contact book as last read; null = not read yet. */
  contacts: readonly BookContact[] | null
  zh: boolean
  connected?: boolean
  bind?: ContactBindState
  /** Owner only: absent, the rows are shown without the bind controls. */
  onBind?: (person: OrgEmployee, channel: BindChannel, identity: string) => void
}) {
  const [openFor, setOpenFor] = useState('')
  const [channel, setChannel] = useState<BindChannel>('feishu')
  const [value, setValue] = useState('')
  const [problem, setProblem] = useState('')
  const [confirming, setConfirming] = useState(false)
  const people = humanMembers(employees)

  const close = () => { setOpenFor(''); setValue(''); setProblem(''); setConfirming(false) }
  // The bind went through when the book read back holds it: the form closes on what the server says, not on the click.
  useEffect(() => {
    if (!openFor || !confirming) return
    const clean = value.trim()
    if (identitiesOf(contacts, openFor).some(item => item.channel === channel && item.identity === clean)) close()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new read of the book can close the form
  }, [contacts])

  return (
    <section className="dashboard-card dashboard-contacts" data-testid="people-contacts">
      <h3 className="dashboard-card-title">{zh ? '联系方式' : 'Contact channels'}</h3>
      <p className="dashboard-projects-note">{zh
        ? '登记每位成员的飞书 open_id 或邮箱，人工步骤和审核要找到具体的人时用。添加成员时填的联系方式也列在这里（只读）。只有老板能绑定。'
        : 'Each member’s Feishu open_id or email, for when a person’s step or a gate has to reach them. What was typed when the member was added is listed too (read-only). Only the owner binds.'}</p>
      {contacts === null && <div className="dashboard-empty" data-testid="people-contacts-loading">{zh ? '正在读取已绑定的联系方式…' : 'Reading the bound channels…'}</div>}
      {people.length === 0 ? (
        <div className="dashboard-empty">{zh ? '组织里还没有人类成员。先在「协作人员与组织」里添加成员。' : 'The organisation has no people yet. Add them under People & organisation first.'}</div>
      ) : (
        <ul className="dashboard-staff-list">
          {people.map(person => {
            const bound = identitiesOf(contacts, person.employee_id)
            const fromOrg = orgContactOf(person)
            const open = openFor === person.employee_id
            const busy = bind.busyEmployeeId === person.employee_id
            const refused = bind.error && bind.error.employeeId === person.employee_id ? bindErrorText(bind.error.error, zh) : ''
            return (
              <li key={person.employee_id} className="dashboard-staff-row dashboard-contact-row" data-testid="people-contact-row" data-employee={person.employee_id}>
                <span className="dashboard-staff-name">{person.name || person.employee_id}</span>
                <span className="dashboard-staff-meta" data-testid="people-contact-channels">
                  {bound.length === 0 && !fromOrg
                    ? (contacts === null ? '' : (zh ? '还没绑定联系方式' : 'No channel bound yet'))
                    : [
                      ...bound.map(item => `${channelLabel(item.channel, zh)} ${item.identity}`),
                      ...(fromOrg ? [`${channelLabel(fromOrg.channel, zh)} ${fromOrg.identity}${zh ? '（添加成员时填的）' : ' (typed in when added)'}`] : []),
                    ].join(zh ? '；' : '; ')}
                </span>
                {onBind && !open && (
                  <button type="button" className="dashboard-inline-btn" data-testid="people-contact-open" disabled={!connected || contacts === null}
                    onClick={() => { close(); setOpenFor(person.employee_id); setChannel('feishu') }}>{zh ? '绑定' : 'Bind'}</button>
                )}
                {onBind && open && (
                  <div className="dashboard-contact-form" data-testid="people-contact-form">
                    {!confirming ? (
                      <>
                        <select aria-label={zh ? '渠道' : 'Channel'} value={channel} onChange={event => { setChannel(event.target.value as BindChannel); setProblem('') }}>
                          {BIND_CHANNELS.map(item => <option key={item} value={item}>{item === 'feishu' ? (zh ? '飞书 open_id' : 'Feishu open_id') : (zh ? '邮箱' : 'Email')}</option>)}
                        </select>
                        <input data-testid="people-contact-value" value={value} maxLength={128}
                          placeholder={channel === 'feishu' ? 'ou_…' : 'name@example.com'}
                          onChange={event => { setValue(event.target.value); setProblem('') }} />
                        <button type="button" className="dashboard-inline-btn" data-testid="people-contact-next"
                          onClick={() => { const found = identityProblem(channel, value, zh); setProblem(found); if (!found) setConfirming(true) }}>{zh ? '下一步' : 'Next'}</button>
                        <button type="button" className="dashboard-inline-btn" onClick={close}>{zh ? '取消' : 'Cancel'}</button>
                      </>
                    ) : (
                      <>
                        <span data-testid="people-contact-confirm-text">{zh
                          ? `确认把${channelLabel(channel, zh)}「${value.trim()}」绑定给「${person.name || person.employee_id}」？绑定后这个号只认这一个人。`
                          : `Bind ${channelLabel(channel, zh)} "${value.trim()}" to ${person.name || person.employee_id}? It will belong to this person only.`}</span>
                        <button type="button" className="dashboard-inline-btn" data-testid="people-contact-confirm" disabled={!connected || busy}
                          onClick={() => onBind(person, channel, value.trim())}>{busy ? (zh ? '绑定中…' : 'Binding…') : (zh ? '确认绑定' : 'Confirm')}</button>
                        <button type="button" className="dashboard-inline-btn" disabled={busy} onClick={() => setConfirming(false)}>{zh ? '返回修改' : 'Back'}</button>
                      </>
                    )}
                    {problem && <small role="alert" data-testid="people-contact-problem">{problem}</small>}
                  </div>
                )}
                {refused && <small role="alert" data-testid="people-contact-refused">{refused}</small>}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
