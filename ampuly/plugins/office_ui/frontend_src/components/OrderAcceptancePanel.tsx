// The acceptance of one order, as a panel of the inbox (frontend redesign step 3, first page): the frozen criteria and
// their evidence, the submission, the decision at the END of the materials (接受 / 打回重做 / 带例外接受 -- the owner's
// words for the third kind of approval; never 通过), the rework, the runtime recovery, the frozen files, the byte
// comparison, the exports, the history, and 确认收货并结案 for a delivering order. Lifted out of the orders table
// (WorkflowsPage's expanded row) with its class names and test ids kept, so the e2e probe reads the same structure.
//
// It decides nothing itself: every write goes through the callbacks App hands in (each on the request tracker), and
// the receipt of the decision that is out is said under the buttons (lib/ordersInbox buttonReceipt).

import { useState } from 'react'
import { useI18n } from '../i18n'
import { buttonReceipt } from '../lib/ordersInbox'
import type { WorkOrderSummary } from './WorkflowsPage'

type EvidenceDraft = { runId: string; artifactId: string; note: string }
const EMPTY_DRAFT: EvidenceDraft = { runId: '', artifactId: '', note: '' }

export interface OrderAcceptancePanelProps {
  order: WorkOrderSummary
  connected?: boolean
  /** A write of this order's acceptance is out (App's acceptanceBusyOrderId, narrowed by the tracker). */
  busy?: boolean
  /** The tracker's entry for this order's decision, for the words under the decision buttons. */
  receipt?: { state: string; words: string }
  onAcceptanceEvidence: (orderId: string, criterion: string, evidence: Record<string, unknown>) => void
  onAcceptanceSubmit: (orderId: string) => void
  onAcceptanceDecide: (orderId: string, decision: 'accepted' | 'rejected' | 'accepted_with_exceptions', note: string, exceptions: string[], options?: { closeOrder?: boolean }) => void
  onAcceptanceRework: (orderId: string, targetNode: string, requestId?: string) => void
  onAcceptanceRoundExport?: (orderId: string, roundNo: number) => void
  onPortfolioExport?: (orderId: string) => void
  /** Owner only, and only a clean acceptance: record the customer's explicit redistribution grant. */
  onAuthorizeRedistribution?: (orderId: string) => void
  onOpenArtifact?: (path: string) => void
}

export function OrderAcceptancePanel({ order, connected = true, busy = false, receipt, onAcceptanceEvidence, onAcceptanceSubmit, onAcceptanceDecide, onAcceptanceRework, onAcceptanceRoundExport, onPortfolioExport, onAuthorizeRedistribution, onOpenArtifact }: OrderAcceptancePanelProps) {
  const { t } = useI18n()
  const [evidenceDrafts, setEvidenceDrafts] = useState<Record<string, EvidenceDraft>>({})
  const [note, setNote] = useState('')
  const [exceptions, setExceptions] = useState('')
  const [reworkTarget, setReworkTarget] = useState('')
  const acceptance = order.acceptance
  if (!acceptance) return null
  const acceptanceStatus = acceptance.status ?? ''
  const evidenceFor = (criterion: string) => (acceptance.evidence ?? []).filter(item => String(item.criterion ?? '') === criterion)
  const draftFor = (criterion: string) => evidenceDrafts[criterion] ?? EMPTY_DRAFT
  const setDraft = (criterion: string, patch: Partial<EvidenceDraft>) => setEvidenceDrafts(prev => ({ ...prev, [criterion]: { ...draftFor(criterion), ...patch } }))
  const said = buttonReceipt(receipt, '')
  const passedAlready = acceptanceStatus === 'accepted' || acceptanceStatus === 'accepted_with_exceptions'
  const statusLabel = (status: string) => t(`orders.acceptance.${status}` as Parameters<typeof t>[0])
  const comparison = (item: NonNullable<typeof acceptance.artifact_comparison>) => t('orders.acceptance.byteComparison', {
    earlier: item.earlier_round ?? '?', later: item.later_round ?? '?',
    added: item.added?.length ?? 0, removed: item.removed?.length ?? 0, changed: item.changed?.length ?? 0, unchanged: item.unchanged?.length ?? 0,
  })

  return (
    <div className="orders-acceptance-panel inbox-acceptance" aria-busy={busy} data-testid="order-acceptance-panel" data-acceptance-status={acceptanceStatus}>
      <div className="orders-acceptance-head">
        <div>
          <strong>{t('orders.acceptance')}</strong>
          <span className={`orders-status orders-status--${acceptanceStatus}`}>{statusLabel(acceptanceStatus)}</span>
          <span className="orders-acceptance-round">{t('orders.acceptance.round', { round: acceptance.round_no ?? 1 })}</span>
        </div>
      </div>
      <div className="orders-acceptance-criteria">
        {acceptance.criteria.map(criterion => {
          const evidence = evidenceFor(criterion)
          const draft = draftFor(criterion)
          const canRecord = !!draft.runId.trim() && !!draft.artifactId.trim()
          return (
            <div key={criterion} className="orders-acceptance-criterion">
              <div className="orders-acceptance-criterion-title">
                <span className={evidence.length > 0 ? 'is-met' : ''}>{evidence.length > 0 ? '✓' : '○'}</span>
                <span>{criterion}</span>
              </div>
              {evidence.length > 0 ? (
                <div className="orders-acceptance-evidence-list">
                  {evidence.map(item => {
                    const artifactPath = [item.artifact_path, item.path].find(value => typeof value === 'string' && value.trim()) as string | undefined
                    const evidenceLabel = `${String(item.artifact_id ?? item.artifact_hash ?? item.event_id ?? '')} · ${String(item.run_id ?? '')}`
                    return (
                      <span key={String(item.evidence_id ?? `${criterion}-${item.run_id}`)} className="orders-acceptance-evidence">
                        {evidenceLabel}
                        {artifactPath && onOpenArtifact && (
                          <button type="button" className="orders-acceptance-evidence-open" onClick={() => onOpenArtifact(artifactPath)}>{t('artifacts.preview')}</button>
                        )}
                      </span>
                    )
                  })}
                </div>
              ) : (acceptanceStatus === 'criteria_defined' || acceptanceStatus === 'evidence_collecting') ? (
                <div className="orders-acceptance-evidence-form">
                  <input value={draft.runId} placeholder={t('orders.acceptance.runId')} onChange={event => setDraft(criterion, { runId: event.target.value })} />
                  <input value={draft.artifactId} placeholder={t('orders.acceptance.artifactId')} onChange={event => setDraft(criterion, { artifactId: event.target.value })} />
                  <input value={draft.note} placeholder={t('orders.acceptance.note')} onChange={event => setDraft(criterion, { note: event.target.value })} />
                  <button type="button" className="workflows-cancel" disabled={!canRecord || busy || !connected}
                    onClick={() => onAcceptanceEvidence(order.order_id, criterion, { run_id: draft.runId.trim(), artifact_path: draft.artifactId.trim(), ...(draft.note.trim() ? { note: draft.note.trim() } : {}) })}>
                    {t('orders.acceptance.recordEvidence')}
                  </button>
                </div>
              ) : null}
            </div>
          )
        })}
      </div>
      <div className="orders-acceptance-actions">
        {(acceptanceStatus === 'criteria_defined' || acceptanceStatus === 'evidence_collecting') && (
          <button type="button" className="workflows-invoke" disabled={!connected || busy || acceptance.criteria.some(criterion => evidenceFor(criterion).length === 0)}
            onClick={() => onAcceptanceSubmit(order.order_id)}>
            {t('orders.acceptance.submit')}
          </button>
        )}
        {acceptanceStatus === 'submitted' && (
          // The decision sits at the end of the materials, in the owner's words for an acceptance: 接受 / 打回重做.
          <>
            <textarea rows={2} value={note} disabled={!connected || busy} placeholder={t('orders.acceptance.decisionNote')} onChange={event => setNote(event.target.value)} />
            <button type="button" className="workflows-invoke" data-testid="acceptance-accept" disabled={!connected || busy}
              onClick={() => onAcceptanceDecide(order.order_id, 'accepted', note, [])}>
              {busy && said.disabled ? said.label : t('inbox.acceptance.accept')}
            </button>
            <button type="button" className="workflows-cancel" data-testid="acceptance-send-back" disabled={!connected || busy || !note.trim()}
              onClick={() => onAcceptanceDecide(order.order_id, 'rejected', note, [])}>
              {t('inbox.acceptance.sendBack')}
            </button>
            <button type="button" className="workflows-cancel" disabled={!connected || busy || !exceptions.trim()}
              onClick={() => onAcceptanceDecide(order.order_id, 'accepted_with_exceptions', note, exceptions.split(/[,，\n]/).map(item => item.trim()).filter(Boolean))}>
              {t('inbox.acceptance.acceptWithExceptions')}
            </button>
            <input className="orders-acceptance-exceptions" value={exceptions} disabled={!connected || busy} placeholder={t('orders.acceptance.exceptions')} onChange={event => setExceptions(event.target.value)} />
          </>
        )}
        {order.status === 'delivering' && ['criteria_defined', 'evidence_collecting', 'submitted', 'accepted', 'accepted_with_exceptions'].includes(acceptanceStatus) && (
          // 确认收货并结案 (B5): the owner's one click for a delivering order, asked once more first. Once the acceptance
          // passed on the buttons above this only closes, with the decision the record holds.
          <button type="button" className="workflows-invoke" data-testid="acceptance-receive-and-close" disabled={!connected || busy}
            title={t(passedAlready ? 'orders.acceptance.closeAcceptedHint' : 'orders.acceptance.receiveAndCloseHint')}
            onClick={() => {
              if (!window.confirm(t(passedAlready ? 'orders.acceptance.closeAcceptedConfirm' : 'orders.acceptance.receiveAndCloseConfirm'))) return
              onAcceptanceDecide(order.order_id, acceptanceStatus === 'accepted_with_exceptions' ? 'accepted_with_exceptions' : 'accepted', note, [], { closeOrder: true })
            }}>
            {busy && said.disabled ? said.label : t('orders.acceptance.receiveAndClose')}
          </button>
        )}
        {said.note && <span className="inbox-receipt-note" role="status" data-testid="acceptance-receipt-note">{said.note}</span>}
        {acceptanceStatus === 'rejected' && (
          <div className="orders-acceptance-rework">
            <p>{acceptance.decision_note || acceptance.rework_feedback || t('orders.acceptance.reworkHint')}</p>
            <input value={reworkTarget} disabled={!connected || busy} placeholder={t('orders.acceptance.reworkTarget')} onChange={event => setReworkTarget(event.target.value)} />
            <button type="button" className="workflows-invoke" disabled={!connected || busy || !reworkTarget.trim()} onClick={() => onAcceptanceRework(order.order_id, reworkTarget.trim())}>
              {t('orders.acceptance.startRework')}
            </button>
          </div>
        )}
        {acceptance.runtime_rework?.state === 'pending' && (
          <div className="orders-acceptance-rework" data-testid="acceptance-runtime-recovery">
            <p>{t('orders.acceptance.runtimePending', { node: acceptance.runtime_rework.target_node, count: acceptance.runtime_rework.attempt_count })}</p>
            {acceptance.runtime_rework.last_error && <p className="workflows-error">{acceptance.runtime_rework.last_error}</p>}
            <button type="button" className="workflows-invoke" disabled={!connected || busy}
              onClick={() => onAcceptanceRework(order.order_id, acceptance.runtime_rework?.target_node ?? '', acceptance.runtime_rework?.request_id)}>
              {t('orders.acceptance.resumeRework')}
            </button>
          </div>
        )}
        {onAuthorizeRedistribution && acceptance.status === 'accepted' && (
          <div className="orders-acceptance-history-files">
            <button type="button" className="orders-acceptance-evidence-open" data-testid="acceptance-redistribution-authorize" disabled={!connected} onClick={() => onAuthorizeRedistribution(order.order_id)}>
              {t('orders.acceptance.authorizeRedistribution')}
            </button>
          </div>
        )}
        {acceptance.artifact_snapshot?.verified && (
          <div className="orders-acceptance-history-files" data-testid="acceptance-final-files">
            <span>{t('orders.acceptance.finalSnapshotVerified', { count: acceptance.artifact_snapshot.files?.length ?? 0 })}</span>
            <button type="button" className="orders-acceptance-evidence-open" disabled={!connected || !onAcceptanceRoundExport} onClick={() => onAcceptanceRoundExport?.(order.order_id, Number(acceptance.round_no ?? 1))}>
              {t('orders.acceptance.exportRound')}
            </button>
            {passedAlready && (
              <button type="button" className="orders-acceptance-evidence-open" data-testid="acceptance-portfolio-export" disabled={!connected || !onPortfolioExport} onClick={() => onPortfolioExport?.(order.order_id)}>
                {t('orders.acceptance.exportPortfolio')}
              </button>
            )}
            {acceptance.artifact_snapshot.files?.map(file => (
              <button key={file.archive_path} type="button" className="orders-acceptance-evidence-open" disabled={!connected || !onOpenArtifact} onClick={() => onOpenArtifact?.(file.archive_path)} title={file.sha256}>
                {file.source_path}
              </button>
            ))}
          </div>
        )}
        {acceptance.artifact_comparison && !acceptance.artifact_comparison.error && (
          <div className="settings-note" data-testid="acceptance-final-comparison">{comparison(acceptance.artifact_comparison)}</div>
        )}
        {(acceptance.history?.length ?? 0) > 0 && (
          <details className="orders-acceptance-history">
            <summary>{t('orders.acceptance.history', { count: acceptance.history?.length ?? 0 })}</summary>
            <ol>
              {acceptance.history?.map((item, index) => (
                <li key={`${String(item.round_no ?? index + 1)}-${String(item.evidence_hash ?? '')}`}>
                  {t('orders.acceptance.round', { round: Number(item.round_no ?? index + 1) })}
                  {' · '}{statusLabel(String(item.status ?? ''))}
                  {' · '}{Array.isArray(item.evidence) ? item.evidence.length : 0} {t('orders.acceptance.evidenceCount')}
                  {String(item.decision_note ?? '').trim() && (
                    <div className="orders-acceptance-history-note" data-testid="acceptance-history-note">{t('orders.acceptance.decisionNote')}：{String(item.decision_note)}</div>
                  )}
                  {item.artifact_snapshot?.verified && (
                    <div className="orders-acceptance-history-files" data-testid="acceptance-history-files">
                      <span>{t('orders.acceptance.snapshotVerified', { count: item.artifact_snapshot.files?.length ?? 0 })}</span>
                      <button type="button" className="orders-acceptance-evidence-open" disabled={!connected || !onAcceptanceRoundExport} onClick={() => onAcceptanceRoundExport?.(order.order_id, Number(item.round_no ?? index + 1))}>
                        {t('orders.acceptance.exportRound')}
                      </button>
                      {item.artifact_snapshot.files?.map(file => (
                        <button key={file.archive_path} type="button" className="orders-acceptance-evidence-open" disabled={!connected || !onOpenArtifact} onClick={() => onOpenArtifact?.(file.archive_path)} title={file.sha256}>
                          {file.source_path}
                        </button>
                      ))}
                    </div>
                  )}
                  {item.artifact_comparison && !item.artifact_comparison.error && (
                    <div className="settings-note" data-testid="acceptance-round-comparison">{comparison(item.artifact_comparison)}</div>
                  )}
                  {item.artifact_comparison?.error && <div className="workflows-error">{item.artifact_comparison.error}</div>}
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </div>
  )
}
