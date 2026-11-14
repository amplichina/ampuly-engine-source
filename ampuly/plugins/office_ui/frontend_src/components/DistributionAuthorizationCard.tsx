// 分发授权单 (4.7-A08, 批量自动化 design 3.2 「每单一张」): in the order drawer, the owner's one authorization for
// sending an order's list out -- what (对象), where (渠道), which moves (允许的动作), for how long. The owner drafts,
// approves and revokes it here; an order whose flow sends its list out in batch starts only under an approved one
// that has not expired. Everything shown comes from the server's last reply (distribution_authorization_*): the page
// decides nothing about whether sending may go ahead.

import { useEffect, useState } from 'react'

export interface DistributionAuthorizationRow {
  authorization_id: string
  targets: string[]
  /** D1 (policy): the sites, as hosts (kuaishou.com); `channels_zh` names them (快手). */
  channels: string[]
  channels_zh: string[]
  actions: string[]
  actions_zh: string[]
  valid_days: number
  expires_at: string
  status: string
  /** draft / approved / expired / revoked -- expired is an approved one past its time. */
  state: string
  state_zh: string
  created_by: string
  created_at: string
  approved_by: string
  approved_at: string
  revoked_by: string
  revoked_at: string
  revoke_reason: string
  note: string
}

export interface DistributionAuthorizationReply {
  order_id: string
  /** Which request this answers. */
  verb: DistributionVerb | ''
  ok: boolean
  /** false: this machine's business database has not been brought up to date yet (the owner's command). */
  ready: boolean
  /** The order's flow sends a list out in batch: it starts only under an approved authorization. */
  needed: boolean
  /** The rows the order's own list carries now; a draft without typed targets takes all of them. */
  listItems: string[]
  /** D1: the only channels a draft may choose -- the sites the order's batch steps work on -- by host and by name. */
  channelOptions: DistributionChannelOption[]
  /** D1: what the order's batch steps declared they do (upload / publish / look_back); the form starts from these. */
  stepActions: string[]
  authorizations: DistributionAuthorizationRow[]
  gate: { ok: boolean; why: string }
  error: string
}

export type DistributionVerb = 'list' | 'draft' | 'approve' | 'revoke'

export interface DistributionChannelOption {
  /** The site as the batch step names it (kuaishou.com): what is sent and stored. */
  host: string
  /** What the owner reads (快手); the host itself for a site without a name of its own. */
  name: string
}

const ACTIONS: ReadonlyArray<readonly [code: string, zh: string, en: string]> = [
  ['upload', '上传', 'Upload'],
  ['publish', '发布', 'Publish'],
  ['look_back', '回查', 'Look back'],
]

const strings = (value: unknown): string[] => (Array.isArray(value) ? value.map(item => String(item ?? '')).filter(Boolean) : [])

/** One `distribution_authorization_*` acknowledgement, read into what the card shows. null = not one of them. */
export function distributionReplyFrom(payload: Record<string, unknown>): DistributionAuthorizationReply | null {
  const action = String(payload.action ?? '')
  if (!action.startsWith('distribution_authorization_')) return null
  const verb = action.slice('distribution_authorization_'.length)
  const gate = (payload.gate && typeof payload.gate === 'object' ? payload.gate : {}) as Record<string, unknown>
  const rows = Array.isArray(payload.authorizations) ? payload.authorizations : []
  return {
    order_id: String(payload.order_id ?? ''),
    verb: verb === 'list' || verb === 'draft' || verb === 'approve' || verb === 'revoke' ? verb : '',
    ok: payload.ok === true,
    ready: payload.ready !== false,
    needed: payload.needed === true,
    listItems: strings(payload.list_items),
    channelOptions: (Array.isArray(payload.channel_options) ? payload.channel_options : [])
      .filter(item => item && typeof item === 'object')
      .map(item => {
        const option = item as Record<string, unknown>
        const host = String(option.host ?? '').trim()
        return { host, name: String(option.name_zh ?? '').trim() || host }
      })
      .filter(option => option.host),
    stepActions: strings(payload.step_actions),
    authorizations: rows.filter(row => row && typeof row === 'object').map(raw => {
      const row = raw as Record<string, unknown>
      const text = (key: string) => String(row[key] ?? '')
      return {
        authorization_id: text('authorization_id'), targets: strings(row.targets), channels: strings(row.channels),
        channels_zh: strings(row.channels_zh),
        actions: strings(row.actions), actions_zh: strings(row.actions_zh), valid_days: Number(row.valid_days ?? 0) || 0,
        expires_at: text('expires_at'), status: text('status'), state: text('state'), state_zh: text('state_zh'),
        created_by: text('created_by'), created_at: text('created_at'), approved_by: text('approved_by'),
        approved_at: text('approved_at'), revoked_by: text('revoked_by'), revoked_at: text('revoked_at'),
        revoke_reason: text('revoke_reason'), note: text('note'),
      }
    }),
    gate: { ok: gate.ok === true, why: String(gate.why_zh ?? '') },
    error: payload.ok === true ? '' : String(payload.error ?? ''),
  }
}

/** The draft the form sends: the ticked sites (hosts, D1 -- never a typed word), targets one per line ([] = the
 *  order's list). */
export function draftPayload(form: { channels: readonly string[]; actions: string[]; days: string; targets: string; note: string }) {
  const split = (text: string, by: RegExp) => text.split(by).map(item => item.trim()).filter(Boolean)
  return {
    channels: form.channels.map(item => item.trim()).filter(Boolean),
    actions: form.actions,
    valid_days: Number(form.days) || 0,
    targets: split(form.targets, /\n+/),
    note: form.note.trim(),
  }
}

const when = (iso: string) => iso.replace('T', ' ').slice(0, 16)

/** The row's state for an English console: the server's sentence is Chinese, so it is said here from `state`. */
export function stateEn(row: Pick<DistributionAuthorizationRow, 'state' | 'expires_at'>): string {
  if (row.state === 'draft') return 'Draft, not approved yet'
  if (row.state === 'approved') return row.expires_at ? `Approved, valid until ${when(row.expires_at)}` : 'Approved'
  if (row.state === 'expired') return row.expires_at ? `Expired (${when(row.expires_at)})` : 'Expired'
  if (row.state === 'revoked') return 'Revoked'
  return 'Unknown state'
}

/** D1 (policy): the channels are fixed options -- one checkbox per site the order's batch steps
 * work on, by the name the owner reads (快手) with its host beside it; nothing can be typed in. */
export function DistributionChannelChoices({ zh, options, chosen, onChange }: {
  zh: boolean
  options: readonly DistributionChannelOption[]
  chosen: readonly string[]
  onChange: (next: string[]) => void
}) {
  return (
    <fieldset data-testid="distribution-channels">
      <legend>{zh ? '发到哪些渠道（批量步骤要去的网站）' : 'Channels (the sites the batch steps work on)'}</legend>
      {options.length === 0 && (
        <p role="status">{zh ? '流程里的批量步骤还没写要去的网站，没法选渠道；先在搭建台给这一步写上网站。' : 'The flow’s batch steps name no site yet; add one on the builder first.'}</p>
      )}
      {options.map(option => (
        <label key={option.host}>
          <input type="checkbox" data-testid="distribution-channel" value={option.host} checked={chosen.includes(option.host)}
            onChange={event => onChange(event.target.checked ? [...chosen, option.host] : chosen.filter(item => item !== option.host))} />
          {zh ? option.name : option.host}{zh && option.name !== option.host && <small>{` ${option.host}`}</small>}
        </label>
      ))}
    </fieldset>
  )
}

export function DistributionAuthorizationCard({ zh, orderId, reply, onAsk }: {
  zh: boolean
  orderId: string
  /** The server's last reply for THIS order; null until one has arrived. */
  reply: DistributionAuthorizationReply | null
  /** Sends one request; returns whether it went out. */
  onAsk: (verb: DistributionVerb, payload: Record<string, unknown>) => boolean
}) {
  const [pending, setPending] = useState(false)
  const [confirming, setConfirming] = useState<{ id: string; verb: 'approve' | 'revoke' } | null>(null)
  const [drafting, setDrafting] = useState(false)
  // channels / actions null = not touched yet: every offered site ticked, and the actions the batch steps declared.
  const [form, setForm] = useState<{ channels: string[] | null; actions: string[] | null; days: string; targets: string; note: string }>(
    { channels: null, actions: null, days: '7', targets: '', note: '' })
  useEffect(() => { onAsk('list', { order_id: orderId }) }, [orderId]) // eslint-disable-line react-hooks/exhaustive-deps
  // A reply ends whatever was in flight; a draft that went through closes the form.
  useEffect(() => {
    setPending(false)
    setConfirming(null)
    if (reply?.ok && reply.verb === 'draft') {
      setDrafting(false)
      setForm(current => ({ ...current, targets: '', note: '' }))
    }
  }, [reply])
  const ask = (verb: DistributionVerb, payload: Record<string, unknown>) => {
    if (pending) return
    if (onAsk(verb, { order_id: orderId, ...payload })) setPending(true)
  }

  const head = <header><strong>{zh ? '分发授权单' : 'Distribution authorization'}</strong></header>
  if (!reply) {
    return <section className="order-handling" data-testid="order-distribution" aria-label={zh ? '分发授权单' : 'Distribution authorization'}>
      {head}<p role="status">{zh ? '正在读取分发授权单…' : 'Reading the distribution authorization…'}</p>
    </section>
  }
  if (!reply.ready) {
    return <section className="order-handling" data-testid="order-distribution" data-ready="false">
      {head}<p role="status">{reply.gate.why}</p>
    </section>
  }
  if (!reply.needed && reply.authorizations.length === 0 && !reply.error) {
    return <section className="order-handling" data-testid="order-distribution" data-needed="false">
      {head}<p>{zh ? '这张工单的流程里没有批量发出的步骤，不需要分发授权单。' : 'This order’s flow sends nothing out in batch; it needs no distribution authorization.'}</p>
    </section>
  }
  const offered = reply.channelOptions.map(option => option.host)
  const channels = (form.channels ?? offered).filter(host => offered.includes(host))
  const actions = form.actions ?? (reply.stepActions.length > 0 ? reply.stepActions : ['publish'])
  const draft = draftPayload({ ...form, channels, actions })
  const canDraft = draft.channels.length > 0 && draft.actions.length > 0 && draft.valid_days >= 1 && draft.valid_days <= 14
    && (draft.targets.length > 0 || reply.listItems.length > 0)

  return (
    <section className="order-handling" data-testid="order-distribution" aria-label={zh ? '分发授权单' : 'Distribution authorization'}>
      <header>
        <strong>{zh ? '分发授权单' : 'Distribution authorization'}</strong>
        {!drafting && <button type="button" className="shell-btn" data-testid="distribution-draft-open" onClick={() => setDrafting(true)}>{zh ? '起草一张' : 'Draft one'}</button>}
      </header>
      <p data-testid="distribution-gate" data-gate-ok={reply.gate.ok ? 'true' : 'false'}>
        {reply.gate.ok ? (zh ? '可以批量发出：' : 'May send out: ') : (zh ? '现在不能批量发出：' : 'May not send out now: ')}{reply.gate.why}
      </p>
      {reply.error && <p role="alert" data-testid="distribution-error">{reply.error}</p>}
      {reply.authorizations.length > 0 && (
        <ul className="order-drawer-files" data-testid="distribution-list">
          {reply.authorizations.map(row => {
            const open = row.state === 'draft' || row.state === 'approved' || row.state === 'expired'
            const asking = confirming?.id === row.authorization_id ? confirming.verb : null
            return (
              <li key={row.authorization_id} data-authorization={row.authorization_id} data-state={row.state}>
                <div className="order-drawer-file">
                  <span className="order-drawer-file-name">
                    <strong>{zh ? (row.state_zh || row.state) : stateEn(row)}</strong>
                    <small>{zh
                      ? `渠道：${(row.channels_zh.length ? row.channels_zh : row.channels).join('、')} · 动作：${row.actions_zh.join('、')}`
                      : `Channels: ${row.channels.join(', ')} · Moves: ${row.actions.join(', ')}`}</small>
                    <small>{zh ? `对象 ${row.targets.length} 条：` : `${row.targets.length} items: `}{row.targets.slice(0, 5).join(zh ? '、' : ', ')}{row.targets.length > 5 ? ' …' : ''}</small>
                    <small>{zh
                      ? `${row.created_by || '—'} 起草于 ${when(row.created_at)}${row.approved_at ? `；${row.approved_by || '—'} 批准于 ${when(row.approved_at)}` : `；批准后 ${row.valid_days} 天内有效`}${row.revoked_at ? `；${row.revoked_by || '—'} 撤销于 ${when(row.revoked_at)}${row.revoke_reason ? `（${row.revoke_reason}）` : ''}` : ''}`
                      : `Drafted by ${row.created_by || '—'} at ${when(row.created_at)}${row.approved_at ? `; approved by ${row.approved_by || '—'} at ${when(row.approved_at)}` : `; valid ${row.valid_days} days once approved`}${row.revoked_at ? `; revoked at ${when(row.revoked_at)}` : ''}`}</small>
                  </span>
                </div>
                {open && (
                  <div className="order-handling-actions">
                    {asking ? (
                      <>
                        <button type="button" className="shell-btn shell-btn--primary" disabled={pending} data-testid={`distribution-${asking}-confirm`}
                          onClick={() => ask(asking, { authorization_id: row.authorization_id })}>
                          {asking === 'approve'
                            ? (zh ? `确认批准（从现在起 ${row.valid_days} 天内有效）` : `Approve (valid ${row.valid_days} days from now)`)
                            : (zh ? '确认撤销（撤销后不能恢复）' : 'Revoke (cannot be undone)')}
                        </button>
                        <button type="button" className="shell-btn" onClick={() => setConfirming(null)}>{zh ? '算了' : 'Cancel'}</button>
                      </>
                    ) : (
                      <>
                        {row.state === 'draft' && <button type="button" className="shell-btn shell-btn--primary" disabled={pending} data-testid="distribution-approve"
                          onClick={() => setConfirming({ id: row.authorization_id, verb: 'approve' })}>{zh ? '批准' : 'Approve'}</button>}
                        <button type="button" className="shell-btn" disabled={pending} data-testid="distribution-revoke"
                          onClick={() => setConfirming({ id: row.authorization_id, verb: 'revoke' })}>{zh ? '撤销' : 'Revoke'}</button>
                      </>
                    )}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {drafting && (
        <form className="order-handling-form" data-testid="distribution-draft-form"
          onSubmit={event => { event.preventDefault(); if (canDraft) ask('draft', draft) }}>
          <DistributionChannelChoices zh={zh} options={reply.channelOptions} chosen={channels}
            onChange={next => setForm({ ...form, channels: next })} />
          <label>{zh ? '批准后有效几天（1–14）' : 'Valid days once approved (1–14)'}
            <input type="number" min="1" max="14" step="1" value={form.days} onChange={event => setForm({ ...form, days: event.target.value })} />
          </label>
          <fieldset>
            <legend>{zh ? '允许的动作' : 'Allowed moves'}</legend>
            {ACTIONS.map(([code, zhName, enName]) => (
              <label key={code}>
                <input type="checkbox" checked={actions.includes(code)}
                  onChange={event => setForm({ ...form, actions: event.target.checked ? [...actions, code] : actions.filter(item => item !== code) })} />
                {zh ? zhName : enName}
              </label>
            ))}
          </fieldset>
          <label className="order-handling-note">{reply.listItems.length > 0
            ? (zh ? `要发的对象（不填 = 这张工单清单里的全部 ${reply.listItems.length} 条；只发其中几条就每行写一个）` : `Items (empty = all ${reply.listItems.length} rows of this order’s list; or one per line)`)
            : (zh ? '要发的对象（每行一个）' : 'Items (one per line)')}
            <textarea rows={3} value={form.targets} onChange={event => setForm({ ...form, targets: event.target.value })} />
          </label>
          <label className="order-handling-note">{zh ? '说明（可不填，写进工单记录）' : 'Note (optional, kept on the order’s trail)'}
            <input type="text" maxLength={400} value={form.note} onChange={event => setForm({ ...form, note: event.target.value })} />
          </label>
          <div className="order-handling-actions">
            <button type="submit" className="shell-btn shell-btn--primary" data-testid="distribution-draft-save" disabled={!canDraft || pending}>{pending ? (zh ? '提交中…' : 'Sending…') : (zh ? '起草' : 'Draft')}</button>
            <button type="button" className="shell-btn" onClick={() => setDrafting(false)}>{zh ? '取消' : 'Cancel'}</button>
          </div>
          <p>{zh
            ? '起草不等于授权：批准之后才生效，每张工单同一时间只有一张生效的；批准后内容不能改，要改就撤销再起草一张。'
            : 'A draft authorizes nothing until approved; one live authorization per order; an approved one cannot be edited -- revoke it and draft another.'}</p>
        </form>
      )}
    </section>
  )
}
