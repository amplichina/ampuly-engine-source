// 人员的联系方式 (4.7-A07). The contact book (ws contact_list / contact_bind: one person, the inboxes they are known by)
// holds the company's own people too, filed under one reserved customer id so they never mix with a customer's
// contacts. Pure: reading the ack, what a person has bound, the words the builder shows beside a person, and the basic
// format check the bind form runs before it asks the owner to confirm. The server checks again and has the last word.

import type { OrgEmployee } from '../types/visual'

/** The customer id the contact book files company members under (a customer's own contacts sit under theirs). */
export const STAFF_CONTACT_CUSTOMER = '_staff'

/** The channels a person can be bound on from 人员与职责, in the order the form offers them. */
export const BIND_CHANNELS = ['feishu', 'email'] as const
export type BindChannel = typeof BIND_CHANNELS[number]

export interface ContactIdentity { channel: string; identity: string }
export interface BookContact { contact_id: string; customer_id: string; display_name: string; identities: ContactIdentity[] }

const text = (value: unknown) => (typeof value === 'string' ? value : '')

/** The contacts an ack carries, or null when it carries none (the list is then not known, not empty). */
export function contactsFrom(raw: unknown): BookContact[] | null {
  if (!Array.isArray(raw)) return null
  const out: BookContact[] = []
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const item = entry as Record<string, unknown>
    const contactId = text(item.contact_id)
    if (!contactId) continue
    const identities = Array.isArray(item.identities)
      ? item.identities.flatMap(row => {
        if (!row || typeof row !== 'object') return []
        const channel = text((row as Record<string, unknown>).channel)
        const identity = text((row as Record<string, unknown>).identity)
        return channel && identity ? [{ channel, identity }] : []
      })
      : []
    out.push({ contact_id: contactId, customer_id: text(item.customer_id), display_name: text(item.display_name), identities })
  }
  return out
}

/** The company's people: the members a person's step or a gate can name (AI employees are not reached by a channel). */
export function humanMembers<T extends Pick<OrgEmployee, 'kind'>>(employees: readonly T[]): T[] {
  return employees.filter(employee => employee.kind === 'human')
}

/** What one person has bound in the contact book (only their company-member entry, never a customer's). */
export function identitiesOf(contacts: readonly BookContact[] | null | undefined, employeeId: string): ContactIdentity[] {
  const id = String(employeeId ?? '').trim()
  if (!id || !contacts) return []
  return contacts.find(contact => contact.contact_id === id && contact.customer_id === STAFF_CONTACT_CUSTOMER)?.identities ?? []
}

/** The contact typed in when the person was added to the organisation (read-only here), by what it looks like. */
export function orgContactOf(employee: Pick<OrgEmployee, 'contact'> | undefined): ContactIdentity | null {
  const value = String(employee?.contact ?? '').trim()
  if (!value) return null
  if (/^ou_/.test(value)) return { channel: 'feishu', identity: value }
  if (value.includes('@')) return { channel: 'email', identity: value }
  return { channel: 'other', identity: value }
}

export type ContactStatus = 'none_needed' | 'unknown' | 'feishu' | 'email' | 'other' | 'none'

/**
 * What the builder says beside the person a step or a gate names. The owner is reached through the console and the
 * configured Feishu group, and an empty choice names nobody: neither gets a word. Until the book is read, a person
 * with nothing known yet is 'unknown' -- never "not bound" on a guess.
 */
export function contactStatus(
  personId: string,
  employees: readonly Pick<OrgEmployee, 'employee_id' | 'contact'>[],
  contacts: readonly BookContact[] | null | undefined,
): ContactStatus {
  const id = String(personId ?? '').trim()
  if (!id || id === 'owner') return 'none_needed'
  const channels = new Set(identitiesOf(contacts, id).map(item => item.channel))
  const fromOrg = orgContactOf(employees.find(employee => employee.employee_id === id))
  if (fromOrg) channels.add(fromOrg.channel)
  if (channels.has('feishu')) return 'feishu'
  if (channels.has('email')) return 'email'
  if (channels.size > 0) return 'other'
  return contacts ? 'none' : 'unknown'
}

export function contactStatusText(status: ContactStatus, zh: boolean): string {
  switch (status) {
    case 'feishu': return zh ? '已绑定飞书' : 'Feishu bound'
    case 'email': return zh ? '已绑定邮箱' : 'Email bound'
    case 'other': return zh ? '已登记联系方式' : 'A contact is on file'
    case 'none': return zh ? '还没绑定联系方式' : 'No contact channel bound yet'
    default: return ''
  }
}

export function channelLabel(channel: string, zh: boolean): string {
  if (channel === 'feishu') return zh ? '飞书' : 'Feishu'
  if (channel === 'email') return zh ? '邮箱' : 'Email'
  return zh ? '联系方式' : 'Contact'
}

const FEISHU_OPEN_ID = /^ou_[A-Za-z0-9]{4,125}$/
const EMAIL = /^[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/

/** Why this value cannot be bound on this channel, in the owner's words; '' when it looks right. */
export function identityProblem(channel: BindChannel, value: string, zh: boolean): string {
  const clean = String(value ?? '').trim()
  if (channel === 'feishu') {
    if (!clean) return zh ? '先填这个人的飞书 open_id' : 'Enter the person’s Feishu open_id'
    if (!FEISHU_OPEN_ID.test(clean)) {
      return zh ? '飞书 open_id 以 ou_ 开头，后面是字母和数字（机器人收到这个人的消息时能看到）' : 'A Feishu open_id starts with ou_ followed by letters and digits'
    }
    return ''
  }
  if (!clean) return zh ? '先填这个人的邮箱地址' : 'Enter the person’s email address'
  if (clean.length > 128 || !EMAIL.test(clean)) return zh ? '邮箱地址格式不对，例：name@example.com' : 'That is not an email address, e.g. name@example.com'
  return ''
}

/** What contact_bind is sent for one person: their company-member entry (made on the first bind) and one identity. */
export function bindRequest(person: Pick<OrgEmployee, 'employee_id' | 'name'>, channel: BindChannel, value: string) {
  return {
    contact_id: person.employee_id,
    customer_id: STAFF_CONTACT_CUSTOMER,
    display_name: person.name || person.employee_id,
    channel,
    identity: String(value ?? '').trim(),
  }
}

/** A refused bind in the owner's words; the server's sentence is English and never shown as it is. */
export function bindErrorText(error: unknown, zh: boolean): string {
  const raw = String(error ?? '')
  if (raw === 'owner_required') return zh ? '只有老板能绑定联系方式' : 'Only the owner can bind a contact channel'
  if (/cannot belong to two people/.test(raw)) return zh ? '这个号已经登记在别人名下，一个号只能属于一个人' : 'This one is already bound to someone else'
  if (/already belongs to/.test(raw)) return zh ? '这个成员的编号已被一位客户联系人占用，没法绑定' : 'This member’s id is taken by a customer contact'
  if (/does not look like a provider identity/.test(raw)) return zh ? '格式不对，系统不认这个号' : 'That does not look like an id this channel uses'
  if (/is not a channel/.test(raw)) return zh ? '系统不支持这个渠道' : 'This channel is not supported'
  return zh ? '绑定没有成功，请稍后再试' : 'The binding did not go through; try again later'
}
