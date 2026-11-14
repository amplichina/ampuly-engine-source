import { useI18n } from '../i18n'
import { BudgetLimitInputs, budgetLimitsFromDraft, type BudgetDraft } from './BudgetLimitInputs'
import type { WorkflowIntakeField } from './WorkflowsPage'

// The one form an order is placed with. A flow's package card on 搭建台 and 业务工单's ＋ 新建工单 both show it, so what
// is asked, what is required and the allowance confirmed before anything runs cannot differ between the two.
export function OrderLaunchForm({ fields, values, onField, budget, onBudget, sending, error, connected, onCancel, onSubmit }: {
  fields: readonly WorkflowIntakeField[]
  values: Record<string, string>
  onField: (fieldId: string, value: string) => void
  budget: BudgetDraft
  onBudget: (next: BudgetDraft) => void
  sending: boolean
  error: string
  connected: boolean
  onCancel: () => void
  onSubmit: (limits: Record<string, number>) => void
}) {
  const { t } = useI18n()
  const missing = fields.filter(field => field.required && !(values[field.id] ?? '').trim()).map(field => field.label)
  const allowance = budgetLimitsFromDraft(budget)
  return (
    <form
      className="workflows-form"
      data-testid="order-launch-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (missing.length > 0 || sending || !connected || allowance.invalid.length > 0) return
        onSubmit(allowance.limits)
      }}
    >
      {fields.map(field => (
        <label key={field.id} className="workflows-field">
          <span>{field.label}{field.required ? ' *' : ''}</span>
          {field.multiline ? (
            <textarea rows={3} value={values[field.id] ?? ''} onChange={(e) => onField(field.id, e.target.value)} />
          ) : (
            <input type="text" value={values[field.id] ?? ''} onChange={(e) => onField(field.id, e.target.value)} />
          )}
        </label>
      ))}
      <fieldset className="workflows-budget" data-testid="launch-budget">
        <legend>{t('orders.budget.launchTitle')}</legend>
        <p className="settings-note">{t('orders.budget.launchHint')}</p>
        <BudgetLimitInputs draft={budget} disabled={sending} onChange={onBudget} />
      </fieldset>
      {error && <div className="workflows-error">{error}</div>}
      <div className="workflows-form-actions">
        <button type="button" className="workflows-cancel" onClick={onCancel} disabled={sending}>
          {t('workflows.cancel')}
        </button>
        <button type="submit" className="workflows-invoke" disabled={!connected || missing.length > 0 || sending || allowance.invalid.length > 0}>
          {sending ? t('workflows.sending') : t('workflows.confirmOrder')}
        </button>
      </div>
      {missing.length > 0 && (
        <div className="workflows-hint">{t('workflows.missing', { fields: missing.join('、') })}</div>
      )}
    </form>
  )
}
