import { useI18n } from '../i18n'

export interface CustomerPreviewView {
  order_id: string
  customer_id: string
  status: string
  brand_name: string
  fields: Record<string, string>
  fields_rule: 'declared' | 'fence'
  stages: Array<{ at: string; stage: string }>
  stages_hidden: number
  acceptance: { status: string; criteria: string[]; round_no?: number; decided_at: string; note: string }
  can_accept: boolean
  deliverables: Array<{ name: string; at: string }>
}

export function CustomerPreviewPanel({ view, loading, error, onClose, onRetry }: {
  view: CustomerPreviewView | null; loading: boolean; error: string | null
  onClose: () => void; onRetry: () => void
}) {
  const { t } = useI18n()
  const shown = !loading && !error ? view : null
  const status = (value: string) => {
    const key = `orders.status.${value}` as Parameters<typeof t>[0]
    const label = t(key)
    return label === key ? value : label
  }
  return <section className="orders-panel customer-preview-panel" aria-label={t('customerPreview.title')}>
    <div className="orders-heading-row"><h3>{t('customerPreview.title')}</h3>
      <button className="workflows-cancel" onClick={onClose}>{t('customerPreview.close')}</button>
      <button className="workflows-cancel" onClick={onRetry}>{t('customerPreview.retry')}</button>
    </div>
    <p className="capability-muted">{t('customerPreview.readonly')}</p>
    {loading && <p role="status">{t('customerPreview.loading')}</p>}
    {error && <p role="alert">{t(error === 'unknown' ? 'customerPreview.unknown' : 'customerPreview.failed')}</p>}
    {shown && <>
      <h4>{shown.brand_name}</h4><p>{status(shown.status)}</p>
      {!shown.customer_id && <p role="note">{t('customerPreview.unbound')}</p>}
      {shown.fields_rule === 'fence' && <p role="note">{t('customerPreview.fence')}</p>}
      <dl>{Object.entries(shown.fields).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
      <h4>{t('customerPreview.stages')}</h4>
      <ol>{shown.stages.map((stage, index) => <li key={index}><time>{stage.at}</time> · {status(stage.stage)}</li>)}</ol>
      {shown.stages_hidden > 0 && <p className="capability-muted">{t('customerPreview.stagesHidden', { count: shown.stages_hidden })}</p>}
      <h4>{t('customerPreview.acceptance')}</h4><p>{t(`orders.acceptance.${shown.acceptance.status}` as Parameters<typeof t>[0])} · {t('orders.acceptance.round', { round: shown.acceptance.round_no ?? 1 })}</p>
      <ul>{shown.acceptance.criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
      {shown.acceptance.decided_at && <time>{shown.acceptance.decided_at}</time>}
      {shown.acceptance.note && <p>{shown.acceptance.note}</p>}
      <h4>{t('customerPreview.deliverables')}</h4>
      {shown.deliverables.length === 0 && <p className="capability-muted">{t('customerPreview.deliverablesEmpty')}</p>}
      <ul>{shown.deliverables.map((item, index) => <li key={index}>{item.name}</li>)}</ul>
    </>}
  </section>
}
