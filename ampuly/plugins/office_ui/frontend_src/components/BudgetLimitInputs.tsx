import { useI18n } from '../i18n'

/** The ceilings an order can be held to today, all counted. A money ceiling is deliberately not offered: model calls
 *  carry no price yet, and the model gate stops every request of a budget that has one -- the order would not run. */
export const BUDGET_KINDS = ['image', 'model_call', 'retry'] as const
export type BudgetKind = typeof BUDGET_KINDS[number]
export type BudgetDraft = Partial<Record<BudgetKind, string>>

/**
 * What the owner typed, as the limits the server stores. A blank box is "no ceiling for this kind" and is left out
 * -- it is never sent as zero, which would forbid the very first call. Anything that is not a non-negative whole
 * number is reported instead of being rounded into something the owner did not write.
 */
export function budgetLimitsFromDraft(draft: BudgetDraft | null | undefined): { limits: Record<string, number>; invalid: BudgetKind[] } {
  const limits: Record<string, number> = {}
  const invalid: BudgetKind[] = []
  for (const kind of BUDGET_KINDS) {
    const text = String(draft?.[kind] ?? '').trim()
    if (!text) continue
    const value = Number(text)
    if (!Number.isFinite(value) || value < 0 || !Number.isInteger(value)) invalid.push(kind)
    else limits[kind] = value
  }
  return { limits, invalid }
}

/** Four optional ceilings. Presentation only: the caller owns the draft and decides when it is sent. */
export function BudgetLimitInputs({ draft, onChange, disabled = false }: {
  draft: BudgetDraft
  onChange: (next: BudgetDraft) => void
  disabled?: boolean
}) {
  const { t } = useI18n()
  const { invalid } = budgetLimitsFromDraft(draft)
  return <div className="orders-budget-inputs" data-testid="budget-limit-inputs">
    {BUDGET_KINDS.map(kind => (
      <label key={kind} className="orders-budget-input">
        <span>{t(`orders.budget.kind.${kind}` as Parameters<typeof t>[0])}</span>
        <input
          type="text"
          inputMode="numeric"
          value={draft[kind] ?? ''}
          placeholder={t('orders.budget.noCeiling')}
          aria-invalid={invalid.includes(kind)}
          disabled={disabled}
          onChange={event => onChange({ ...draft, [kind]: event.target.value })}
        />
      </label>
    ))}
    {invalid.length > 0 && <div className="workflows-error" role="alert">{t('orders.budget.invalid')}</div>}
  </div>
}
