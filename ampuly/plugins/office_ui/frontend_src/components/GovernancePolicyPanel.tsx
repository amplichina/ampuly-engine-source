import { useState } from 'react'
import { useI18n } from '../i18n'

/** One recorded policy decision: who changed which field, from what to what, and why. */
export interface GovernancePolicyChange {
  field: string
  old_value?: unknown
  new_value?: unknown
  actor?: string
  at?: string
  reason?: string
}

/** `governance_policy_list` as the server sent it: the policy, and which of its fields the runtime actually reads. */
export interface GovernancePolicyView {
  /** The company profile whose policy is in effect; empty when this console has no organization running yet. */
  profile?: string
  policy: Record<string, unknown>
  writable: string[]
  stored_only: string[]
  stored_only_note: string
  changes: GovernancePolicyChange[]
}

export interface GovernancePolicyPanelProps {
  /** null while the first answer is still on its way. */
  view: GovernancePolicyView | null
  connected: boolean
  error?: string | null
  onRefresh?: () => void
  /** Absent for anyone who may not change policy; the panel then only reads. */
  onSet?: (field: string, value: boolean, reason: string) => void
}

/** The server refuses a change whose reason is shorter than this; so does the form. */
export const POLICY_REASON_MIN = 4

/** `review.enable_work_item_gates` read out of the nested policy; undefined when any step is missing. */
export function policyValue(policy: Record<string, unknown> | null | undefined, field: string): unknown {
  let cursor: unknown = policy
  for (const part of field.split('.')) {
    if (!cursor || typeof cursor !== 'object') return undefined
    cursor = (cursor as Record<string, unknown>)[part]
  }
  return cursor
}

function shown(value: unknown, on: string, off: string): string {
  if (value === true) return on
  if (value === false) return off
  if (value === undefined || value === null || value === '') return '—'
  return String(value)
}

/** Governance policy as the runtime reads it. The fields that change behaviour get a switch that demands a reason;
 *  the fields that are only stored say so, instead of looking like governance and governing nothing. */
export function GovernancePolicyPanel({ view, connected, error, onRefresh, onSet }: GovernancePolicyPanelProps) {
  const { t, translateMaybe } = useI18n()
  const [editing, setEditing] = useState<{ field: string; reason: string }>({ field: '', reason: '' })
  // A field this build has no words for is shown by its own name rather than hidden.
  const label = (field: string) => translateMaybe('capability.policy', field)
  const on = t('capability.enabled')
  const off = t('capability.disabled')

  return <article className="capability-panel" data-testid="governance-policy-panel">
    <div className="capability-panel-title">
      <h2>{t('capability.governance')}</h2>
      {onRefresh && <button type="button" className="capability-refresh-btn" onClick={onRefresh} disabled={!connected}>{t('capability.policy.refresh')}</button>}
    </div>
    {error && <div className="capability-state capability-state--error" role="alert">{error}</div>}
    {view === null ? <div className="capability-muted">{t('capability.policy.loading')}</div>
      : Object.keys(view.policy).length === 0 ? (
        // No organization is running, so no policy is in effect: say that, rather than a row of dashes with a switch
        // that has nothing to switch.
        <div className="capability-muted" data-testid="governance-no-policy">{t('capability.policy.none')}</div>
      ) : <>
      <div className="capability-list">
        {view.writable.map(field => {
          const current = policyValue(view.policy, field)
          const open = editing.field === field
          return <div className="capability-list-item" key={field} data-testid="governance-writable" data-field={field}>
            <div>
              <strong>{label(field)}</strong>
              <small className="capability-detail">{t('capability.policy.effective')}</small>
              {open && <span className="capability-reason-form" data-testid="governance-reason">
                <input
                  className="capability-refresh-btn"
                  placeholder={t('capability.policy.reasonPlaceholder')}
                  value={editing.reason}
                  onChange={event => setEditing({ field, reason: event.target.value })}
                />
                <button
                  type="button"
                  className="workflows-cancel"
                  data-testid="governance-confirm"
                  disabled={!connected || editing.reason.trim().length < POLICY_REASON_MIN}
                  onClick={() => { onSet?.(field, current !== true, editing.reason.trim()); setEditing({ field: '', reason: '' }) }}
                >
                  {t(current === true ? 'capability.policy.confirmOff' : 'capability.policy.confirmOn')}
                </button>
                <button type="button" className="capability-refresh-btn" onClick={() => setEditing({ field: '', reason: '' })}>{t('common.cancel')}</button>
              </span>}
            </div>
            <span>
              <span className={`capability-pill capability-pill--${current === true ? 'ok' : 'warn'}`}>{shown(current, on, off)}</span>
              {onSet && typeof current === 'boolean' && !open && <button
                type="button"
                className="capability-refresh-btn"
                data-testid="governance-change"
                disabled={!connected}
                onClick={() => setEditing({ field, reason: '' })}
              >
                {t('capability.policy.change')}
              </button>}
            </span>
          </div>
        })}
      </div>
      {view.stored_only.length > 0 && <>
        <div className="capability-muted" data-testid="governance-stored-only-note">{t('capability.policy.storedOnly')}</div>
        <div className="capability-policy-grid">
          {view.stored_only.map(field => <span key={field} style={{ display: 'contents' }} data-testid="governance-stored-only">
            <span>{label(field)}</span><b>{shown(policyValue(view.policy, field), on, off)}</b>
          </span>)}
        </div>
      </>}
      <div className="capability-detail"><strong>{t('capability.policy.changes')}</strong></div>
      {view.changes.length === 0 ? <div className="capability-muted">{t('capability.policy.noChanges')}</div> : <div className="capability-list">
        {view.changes.slice(0, 8).map((change, index) => <div className="capability-list-item" key={`${change.field}:${change.at ?? index}`} data-testid="governance-change-record">
          <div>
            <strong>{label(change.field)}</strong>
            <small className="capability-detail">{shown(change.old_value, on, off)} → {shown(change.new_value, on, off)} · {change.actor || '—'} · {(change.at ?? '').replace('T', ' ').slice(0, 16) || '—'}</small>
            <small className="capability-detail">{change.reason || '—'}</small>
          </div>
        </div>)}
      </div>}
    </>}
  </article>
}
