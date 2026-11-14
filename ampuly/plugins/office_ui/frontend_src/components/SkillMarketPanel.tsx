import { useEffect, useId, useRef, useState } from 'react'

import { useI18n } from '../i18n'
import { trialFlowTargets } from '../lib/componentCatalog'
import {
  admissionBlockers,
  formatCount,
  hasListingMismatch,
  listingMismatchWords,
  marketRefusalWords,
  needsLicenseConfirmation,
  parseSuccessCriteria,
  trialActions,
  trialForListing,
  trialKey,
  type MarketCategory,
  type MarketRefusal,
  type MarketSkill,
  type TrialSkill,
} from '../lib/skillMarket'
import { FlowPicker } from './FlowPicker'
import type { WorkflowSummary } from './WorkflowsPage'

export interface MarketBrowseState {
  skills: MarketSkill[]
  total: number
  keyword: string
  category: string
  sort: 'stars' | 'downloads'
  loading: boolean
  /** The server's message (never shown as it is) and its reason code: the page says the reason in words. */
  error: string | null
  reason?: string
}

export type TrialBusy = 'downloading' | 'marking' | 'admitting' | 'discarding' | 'restoring'

export interface SkillMarketPanelProps {
  connected: boolean
  categories: MarketCategory[]
  browse: MarketBrowseState
  trials: TrialSkill[]
  /** trialKey or slug -> what is in flight for it */
  busy: Record<string, TrialBusy>
  /** The last trial refusal, kept as its reason and what the server said: worded when shown, in the page's language now. */
  trialError?: MarketRefusal | null
  fileView?: { key: string; path: string; text: string; truncated: boolean; binary: boolean } | null
  initialView?: 'market' | 'trials'
  /** Open the trial area on this trial's card (录入内部库 from a market card or the builder shelf); seq changes per request. */
  focus?: { key: string; seq: number } | null
  /** The focus was shown: the next visit to 组件库 opens where the owner left it, not on that card again. */
  onFocusHandled?: () => void
  /** The workflows 调用测试 can take a trial to; absent = no 调用测试, only downloading into the trial area. */
  workflows?: WorkflowSummary[] | null
  onBrowse: (query: { keyword: string; category: string; sort: 'stars' | 'downloads' }) => void
  onDownload: (slug: string) => void
  onMark: (trial: TrialSkill, outcome: 'passed' | 'failed', note: string) => void
  /**
   * listingMismatchAcknowledged: the owner confirmed admitting a SkillHub package that differs from SkillHub's own list;
   * listingMismatchKey: the fingerprint of the differences that confirmation showed (the trial's listing_mismatch_key).
   */
  onAdmit: (trial: TrialSkill, note: string, licenseConfirmed: boolean, successCriteria: string[], listingMismatchAcknowledged?: boolean, listingMismatchKey?: string) => void
  /**
   * The server refused an admission for want of that confirmation (the page's trial list was older than the trial), or
   * because the confirmation was given to other differences than the trial's now (changed): open the confirmation on
   * this trial's card, with its current differences; seq changes per refusal.
   */
  mismatchAsk?: { key: string; seq: number; changed?: boolean } | null
  /** The confirmation was answered (confirmed or put off): the ask is spent. */
  onMismatchAskHandled?: () => void
  onViewFile: (trial: TrialSkill, path: string) => void
  onRefreshTrials: () => void
  /** 调用测试: open this workflow on the builder with the trial skill on the shelf. */
  onUse?: (trial: TrialSkill, workflowId: string) => void
  onDiscard?: (trial: TrialSkill) => void
  onRestore?: (trial: TrialSkill) => void
}

type Translate = ReturnType<typeof useI18n>['t']

/** 弃用 asks once more before it gives a trial up. */
function DiscardButton({ disabled, busy, onDiscard, t }: { disabled: boolean; busy: boolean; onDiscard: () => void; t: Translate }) {
  const [confirming, setConfirming] = useState(false)
  return (
    <button type="button" className="workflows-cancel" data-testid="trial-discard" disabled={disabled}
      onClick={() => { if (confirming) { setConfirming(false); onDiscard() } else setConfirming(true) }}
      onBlur={() => setConfirming(false)}>
      {busy ? t('market.saving') : confirming ? t('market.discardConfirm') : t('market.discard')}
    </button>
  )
}

function MarketCard({ skill, category, trial, busy, connected, workflows, onDownload, onUse, onDiscard, onRestore, onOpenAdmission, t, locale }: {
  skill: MarketSkill; category: string; trial?: TrialSkill; busy?: string; connected: boolean; workflows?: WorkflowSummary[] | null
  onDownload: (slug: string) => void; onUse?: SkillMarketPanelProps['onUse']; onDiscard?: SkillMarketPanelProps['onDiscard']; onRestore?: SkillMarketPanelProps['onRestore']
  onOpenAdmission: (trial: TrialSkill) => void; t: Translate; locale: string
}) {
  const [picking, setPicking] = useState(false)
  // 调用测试 on a skill not downloaded yet downloads it first, then opens the picker once the trial is there.
  const [useAfterDownload, setUseAfterDownload] = useState(false)
  useEffect(() => {
    if (useAfterDownload && trial) { setUseAfterDownload(false); setPicking(true) }
    if (useAfterDownload && !trial && !busy) setUseAfterDownload(false)
  }, [useAfterDownload, trial, busy])
  const canUse = Boolean(onUse && workflows)
  const actions = trialActions(trial)
  return (
    <article className="cl-card cl-card--market" data-testid="market-skill" data-slug={skill.slug}>
      <div className="cl-card-top">
        <strong>{skill.name}</strong>
        <span className="cl-market-stars" title={t('market.stars')}>★ {formatCount(skill.stars, locale)}</span>
      </div>
      <small className="cl-meta">{skill.slug}{skill.version ? ` · v${skill.version}` : ''}{category ? ` · ${category}` : ''} · {t('market.downloads', { count: formatCount(skill.downloads, locale) })}</small>
      {skill.summary && <p className="cl-summary">{skill.summary}</p>}
      <div className="cl-chips">
        {skill.verified && <span className="cl-chip cl-chip--ok">{t('market.verified')}</span>}
        {skill.requires_api_key && <span className="cl-chip cl-chip--warn">{t('market.needsKey')}</span>}
        {skill.paid && <span className="cl-chip cl-chip--warn">{t('market.paid')}</span>}
        {skill.upstream === 'clawhub' && <span className="cl-chip">{t('market.fromClawHub')}</span>}
        {trial && <span className={`cl-chip ${trial.status === 'test_passed' || trial.status === 'admitted' ? 'cl-chip--ok' : trial.status === 'test_failed' ? 'cl-chip--warn' : ''}`}>{t(`market.trialStatus.${trial.status}` as Parameters<Translate>[0])} · v{trial.version}</span>}
      </div>
      <div className="cl-actions">
        {!trial && (skill.paid || !canUse) && (
          <button type="button" className="capability-refresh-btn" disabled={!connected || skill.paid || Boolean(busy)} data-testid="market-download" onClick={() => onDownload(skill.slug)}>
            {busy === 'downloading' ? t('market.downloading') : skill.paid ? t('market.paidNoDownload') : t('market.download')}
          </button>
        )}
        {actions.includes('use') && canUse && !skill.paid && (
          <button type="button" className="capability-refresh-btn" data-testid="market-use" disabled={!connected || Boolean(busy)} aria-expanded={picking}
            onClick={() => { if (trial) setPicking(current => !current); else { setUseAfterDownload(true); onDownload(skill.slug) } }}>
            {busy === 'downloading' ? t('market.downloading') : t('market.use')}
          </button>
        )}
        {trial && actions.includes('discard') && onDiscard && (
          <DiscardButton disabled={!connected || Boolean(busy)} busy={busy === 'discarding'} onDiscard={() => onDiscard(trial)} t={t} />
        )}
        {trial && actions.includes('admit') && (
          <button type="button" className="workflows-invoke" data-testid="market-admit-open" disabled={!connected || Boolean(busy)} onClick={() => onOpenAdmission(trial)}>
            {t('market.admitOpen')}
          </button>
        )}
        {trial && actions.includes('restore') && onRestore && (
          <button type="button" className="capability-refresh-btn" data-testid="trial-restore" disabled={!connected || Boolean(busy)} onClick={() => onRestore(trial)}>
            {busy === 'restoring' ? t('market.saving') : t('market.restore')}
          </button>
        )}
        {trial && actions.includes('redownload') && !skill.paid && (
          // A discarded trial whose files changed cannot be restored; a fresh verified download replaces it.
          <button type="button" className="workflows-cancel" data-testid="market-redownload" disabled={!connected || Boolean(busy)} onClick={() => onDownload(skill.slug)}>
            {busy === 'downloading' ? t('market.downloading') : t('market.redownload')}
          </button>
        )}
      </div>
      {picking && trial && onUse && workflows && (
        <FlowPicker title={t('market.usePick', { name: trial.name })} note={t('market.useNote')} targets={trialFlowTargets(trial.slug, workflows)} t={t}
          onClose={() => setPicking(false)} onPick={workflowId => { setPicking(false); onUse(trial, workflowId) }} />
      )}
    </article>
  )
}

/** Test notes and the pass / fail buttons: the trial card's, and the builder shelf's for a skill brought there to test. */
export function TrialTestControls({ trial, busy, connected, onMark, t, compact = false }: {
  trial: TrialSkill; busy?: string; connected: boolean; onMark: SkillMarketPanelProps['onMark']; t: Translate; compact?: boolean
}) {
  const [note, setNote] = useState('')
  const noteOk = note.trim().length >= 8
  return (
    <div className={`cl-trial-actions${compact ? ' cl-trial-actions--compact' : ''}`} data-testid="trial-test-controls">
      <textarea rows={2} value={note} placeholder={t('market.notePlaceholder')} aria-label={t('market.notePlaceholder')} onChange={event => setNote(event.target.value)} />
      <div className="cl-actions">
        <button type="button" className="workflows-cancel" disabled={!connected || !noteOk || Boolean(busy)} data-testid="trial-fail" onClick={() => { onMark(trial, 'failed', note.trim()); setNote('') }}>
          {t('market.markFailed')}
        </button>
        <button type="button" className="capability-refresh-btn" disabled={!connected || !noteOk || Boolean(busy)} data-testid="trial-pass" onClick={() => { onMark(trial, 'passed', note.trim()); setNote('') }}>
          {busy === 'marking' ? t('market.saving') : t('market.markPassed')}
        </button>
      </div>
    </div>
  )
}

/**
 * 调用测试 on the builder shelf: where the skill on trial stands, its test notes and verdict, and -- once a test has
 * passed -- 弃用 or 录入内部库 (which opens the trial card, where success criteria and the license are confirmed).
 */
export function TrialShelf({ trial, busy, connected, onMark, onDiscard, onRestore, onOpenAdmission }: {
  trial: TrialSkill | null; busy?: string; connected: boolean
  onMark: SkillMarketPanelProps['onMark']; onDiscard: (trial: TrialSkill) => void; onRestore: (trial: TrialSkill) => void; onOpenAdmission: (trial: TrialSkill) => void
}) {
  const { t } = useI18n()
  if (!trial) return <p className="wf-palette-hint" data-testid="trial-shelf">{t('market.noTrials')}</p>
  const actions = trialActions(trial)
  return (
    <div className="wf-trial-shelf" data-testid="trial-shelf" data-status={trial.status}>
      <span className={`cl-chip ${trial.status === 'test_passed' || trial.status === 'admitted' ? 'cl-chip--ok' : trial.status === 'test_failed' ? 'cl-chip--warn' : ''}`}>
        {t(`market.trialStatus.${trial.status}` as Parameters<Translate>[0])} · v{trial.version}
      </span>
      {trial.tests.length > 0 && (
        <small className="cl-line">{t(`market.testOutcome.${trial.tests[trial.tests.length - 1].outcome}` as Parameters<Translate>[0])}：{trial.tests[trial.tests.length - 1].note}</small>
      )}
      {actions.includes('use') && <TrialTestControls trial={trial} busy={busy} connected={connected} onMark={onMark} t={t} compact />}
      {actions.includes('admit') && <small className="cl-line">{t('market.shelf.passed')}</small>}
      <div className="cl-actions">
        {actions.includes('discard') && <DiscardButton disabled={!connected || Boolean(busy)} busy={busy === 'discarding'} onDiscard={() => onDiscard(trial)} t={t} />}
        {actions.includes('admit') && (
          <button type="button" className="workflows-invoke" data-testid="shelf-admit-open" disabled={!connected || Boolean(busy)} onClick={() => onOpenAdmission(trial)}>
            {t('market.admitOpen')}
          </button>
        )}
        {actions.includes('restore') && (
          <button type="button" className="capability-refresh-btn" data-testid="trial-restore" disabled={!connected || Boolean(busy)} onClick={() => onRestore(trial)}>
            {busy === 'restoring' ? t('market.saving') : t('market.restore')}
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * The trial card's warning that SkillHub's file list and its package differ (the package is what counts); nothing when
 * they agreed. Plain paragraphs in the alert: the same size and colour as the secrets alert beside it.
 */
export function ListingMismatchNote({ trial, t }: { trial: Pick<TrialSkill, 'listing_mismatch'>; t: Translate }) {
  const words = listingMismatchWords(t, trial.listing_mismatch)
  if (!words.files && !words.version) return null
  return (
    <div className="cl-alert" role="note" data-testid="trial-listing-mismatch">
      {words.files && <p>{words.files}</p>}
      {words.version && <p>{words.version}</p>}
    </div>
  )
}

function AdmissionForm({ trial, busy, connected, onAdmit, askSeq, askChanged = false, onAskHandled, t }: {
  trial: TrialSkill; busy?: string; connected: boolean; onAdmit: SkillMarketPanelProps['onAdmit']; askSeq: number; askChanged?: boolean
  onAskHandled?: () => void; t: Translate
}) {
  const [note, setNote] = useState('')
  const [criteriaText, setCriteriaText] = useState('')
  const [licenseConfirmed, setLicenseConfirmed] = useState(false)
  // 录入内部库 on a package that differs from SkillHub's own list asks first, naming the differences (policy);
  // a refusal for want of that yes, or for a yes to other differences (askSeq), opens the same question.
  const [confirmingMismatch, setConfirmingMismatch] = useState(askSeq > 0)
  useEffect(() => { if (askSeq > 0) setConfirmingMismatch(true) }, [askSeq])
  const titleId = useId()
  const blockers = admissionBlockers(trial)
  const needsLicense = needsLicenseConfirmation(trial)
  const { criteria, problem } = parseSuccessCriteria(criteriaText)
  const noteOk = note.trim().length >= 8
  const blocked = !connected || !noteOk || Boolean(problem) || blockers.length > 0 || (needsLicense && !licenseConfirmed) || Boolean(busy)
  const mismatch = listingMismatchWords(t, trial.listing_mismatch)
  // The note and criteria stay after sending: a refusal (this one, or any other) is answered without typing them again.
  // The yes goes with the key of the differences this card shows, so it is a yes to those and no others.
  const send = (acknowledged: boolean) => {
    setConfirmingMismatch(false); onAskHandled?.()
    onAdmit(trial, note.trim(), licenseConfirmed, criteria, acknowledged, acknowledged ? trial.listing_mismatch_key ?? '' : '')
  }
  return (
    <div className="cl-trial-actions" data-testid="trial-admission">
      <textarea rows={2} value={note} placeholder={t('market.notePlaceholder')} aria-label={t('market.notePlaceholder')} onChange={event => setNote(event.target.value)} />
      <textarea rows={3} value={criteriaText} placeholder={t('market.criteria')} aria-label={t('market.criteria')} data-testid="trial-criteria" onChange={event => setCriteriaText(event.target.value)} />
      {criteriaText.trim() && problem && <p className="cl-line cl-note">{t(`market.criteriaProblem.${problem}` as Parameters<Translate>[0])}</p>}
      {needsLicense && (
        <label className="cl-license-confirm">
          <input type="checkbox" checked={licenseConfirmed} onChange={event => setLicenseConfirmed(event.target.checked)} />
          {t('market.licenseConfirm')}
        </label>
      )}
      {confirmingMismatch ? (
        <div className="cl-alert" role="alertdialog" aria-labelledby={titleId} data-testid="trial-mismatch-confirm">
          <p className="cl-alert-title" id={titleId}>{t('market.mismatch.confirmTitle')}</p>
          {askChanged && <p data-testid="trial-mismatch-changed">{t('market.mismatch.changed')}</p>}
          {mismatch.files && <p>{mismatch.files}</p>}
          {mismatch.version && <p>{mismatch.version}</p>}
          <p>{t('market.mismatch.confirm')}</p>
          <div className="cl-actions">
            <button type="button" className="workflows-cancel" data-testid="trial-mismatch-cancel" onClick={() => { setConfirmingMismatch(false); onAskHandled?.() }}>
              {t('market.mismatch.cancel')}
            </button>
            <button type="button" className="workflows-invoke" data-testid="trial-mismatch-admit" disabled={blocked} onClick={() => send(true)}>
              {busy === 'admitting' ? t('market.saving') : t('market.mismatch.confirmButton')}
            </button>
          </div>
        </div>
      ) : (
        <div className="cl-actions">
          <button
            type="button"
            className="workflows-invoke"
            data-testid="trial-admit"
            disabled={blocked}
            onClick={() => { if (hasListingMismatch(trial.listing_mismatch)) setConfirmingMismatch(true); else send(false) }}
          >
            {busy === 'admitting' ? t('market.saving') : t('market.admit')}
          </button>
        </div>
      )}
      {blockers.length > 0 && <p className="cl-line cl-note">{blockers.map(blocker => t(`market.blocker.${blocker}` as Parameters<Translate>[0])).join('；')}</p>}
    </div>
  )
}

function TrialCard({ trial, busy, connected, fileView, focused, workflows, onMark, onAdmit, onViewFile, onUse, onDiscard, onRestore, askSeq, askChanged, onAskHandled, t }: {
  trial: TrialSkill; busy?: string; connected: boolean; fileView?: SkillMarketPanelProps['fileView']; focused: boolean; workflows?: WorkflowSummary[] | null
  onMark: SkillMarketPanelProps['onMark']; onAdmit: SkillMarketPanelProps['onAdmit']; onViewFile: SkillMarketPanelProps['onViewFile']
  onUse?: SkillMarketPanelProps['onUse']; onDiscard?: SkillMarketPanelProps['onDiscard']; onRestore?: SkillMarketPanelProps['onRestore']
  askSeq: number; askChanged?: boolean; onAskHandled?: () => void; t: Translate
}) {
  const key = trialKey(trial)
  const viewing = fileView && fileView.key === key ? fileView : null
  const actions = trialActions(trial)
  const [picking, setPicking] = useState(false)
  const ref = useRef<HTMLElement>(null)
  useEffect(() => { if (focused) ref.current?.scrollIntoView({ block: 'center' }) }, [focused])
  return (
    <article ref={ref} className={`cl-card cl-card--trial${focused ? ' is-focused' : ''}`} data-risk={trial.governance.risk_level} data-testid="trial-skill" data-key={key} data-status={trial.status}>
      <div className="cl-card-top">
        <strong>{trial.name}</strong>
        <span className={`cl-chip ${trial.status === 'test_passed' || trial.status === 'admitted' ? 'cl-chip--ok' : trial.status === 'test_failed' ? 'cl-chip--warn' : ''}`}>
          {t(`market.trialStatus.${trial.status}` as Parameters<Translate>[0])}
        </span>
      </div>
      <small className="cl-meta">{trial.source === 'catalog' ? `${t('market.fromCatalog')} · ` : ''}{trial.slug} · v{trial.version} · {t('market.downloadedAt', { at: trial.downloaded_at.slice(0, 16).replace('T', ' ') })}</small>
      <div className="cl-chips">
        <span className={`cl-risk cl-risk--${trial.governance.risk_level}`}>{t(`components.risk.${trial.governance.risk_level}` as Parameters<Translate>[0])}</span>
        {trial.governance.reasons.map(reason => <span className="cl-chip cl-chip--reason" key={reason}>{t(`components.skillReason.${reason}` as Parameters<Translate>[0])}</span>)}
        {trial.governance.admission_blockers.map(blocker => <span className="cl-chip cl-chip--warn" key={blocker}>{t(`components.blocker.${blocker}` as Parameters<Translate>[0])}</span>)}
      </div>
      {trial.secret_findings.length > 0 && (
        <p className="cl-alert" role="alert">{t('market.secrets', { list: trial.secret_findings.slice(0, 3).join('；') })}</p>
      )}
      <ListingMismatchNote trial={trial} t={t} />
      {trial.security_reports.length > 0 && (
        <p className="cl-line">
          {t('market.reports')}{' '}
          {trial.security_reports.map(report => report.url
            ? <a key={report.vendor} href={report.url} target="_blank" rel="noopener noreferrer">{report.vendor}{report.status_text ? `（${report.status_text}）` : ''}</a>
            : <span key={report.vendor}>{report.vendor}{report.status_text ? `（${report.status_text}）` : ''}</span>)}
          <span className="cl-note">{t('market.reportsNote')}</span>
        </p>
      )}
      <details className="cl-files">
        <summary>{t('market.files', { count: trial.files.length })}</summary>
        <ul>
          {trial.files.map(path => (
            <li key={path}><button type="button" className="cl-file-link" disabled={!connected} onClick={() => onViewFile(trial, path)}>{path}</button></li>
          ))}
        </ul>
        {viewing && (
          <div className="cl-file-view" data-testid="trial-file-view">
            <strong>{viewing.path}</strong>
            {viewing.binary ? <p className="cl-line">{t('market.binary')}</p> : <pre>{viewing.text}</pre>}
            {viewing.truncated && <p className="cl-line">{t('market.truncated')}</p>}
          </div>
        )}
      </details>
      {trial.tests.length > 0 && (
        <ul className="cl-tests">
          {trial.tests.map((test, index) => (
            <li key={`${test.at}-${index}`}>{t(`market.testOutcome.${test.outcome}` as Parameters<Translate>[0])} · {test.by} · {test.at.slice(0, 16).replace('T', ' ')}：{test.note}</li>
          ))}
        </ul>
      )}
      {actions.includes('use') && <TrialTestControls trial={trial} busy={busy} connected={connected} onMark={onMark} t={t} />}
      {actions.includes('admit') && <AdmissionForm trial={trial} busy={busy} connected={connected} onAdmit={onAdmit} askSeq={askSeq} askChanged={askChanged} onAskHandled={onAskHandled} t={t} />}
      <div className="cl-actions">
        {actions.includes('use') && onUse && workflows && (
          <button type="button" className="capability-refresh-btn" data-testid="trial-use" disabled={!connected || Boolean(busy)} aria-expanded={picking} onClick={() => setPicking(current => !current)}>
            {t('market.use')}
          </button>
        )}
        {actions.includes('discard') && onDiscard && (
          <DiscardButton disabled={!connected || Boolean(busy)} busy={busy === 'discarding'} onDiscard={() => onDiscard(trial)} t={t} />
        )}
        {actions.includes('restore') && onRestore && (
          <button type="button" className="capability-refresh-btn" data-testid="trial-restore" disabled={!connected || Boolean(busy)} onClick={() => onRestore(trial)}>
            {busy === 'restoring' ? t('market.saving') : t('market.restore')}
          </button>
        )}
      </div>
      {picking && onUse && workflows && (
        <FlowPicker title={t('market.usePick', { name: trial.name })} note={t('market.useNote')} targets={trialFlowTargets(trial.slug, workflows)} t={t}
          onClose={() => setPicking(false)} onPick={workflowId => { setPicking(false); onUse(trial, workflowId) }} />
      )}
    </article>
  )
}

export function SkillMarketPanel(props: SkillMarketPanelProps) {
  const { t, locale } = useI18n()
  const { connected, categories, browse, trials, busy, trialError, fileView, workflows, focus, onFocusHandled, onBrowse, onDownload, onMark, onAdmit, onViewFile, onRefreshTrials, onUse, onDiscard, onRestore, mismatchAsk, onMismatchAskHandled } = props
  const [view, setView] = useState<'market' | 'trials'>(props.initialView ?? (focus ? 'trials' : 'market'))
  const [focusKey, setFocusKey] = useState(focus?.key ?? '')
  const [keyword, setKeyword] = useState(browse.keyword)
  // 录入内部库 elsewhere (a market card, the builder shelf) opens the trial area on that trial's card.
  useEffect(() => {
    if (focus?.key) { setView('trials'); setFocusKey(focus.key); onFocusHandled?.() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.seq, focus?.key])
  const categoryName = new Map(categories.map(item => [item.key, locale.startsWith('zh') ? item.name : (item.name_en || item.name)]))
  const submit = (next: Partial<{ keyword: string; category: string; sort: 'stars' | 'downloads' }>) =>
    onBrowse({ keyword: next.keyword ?? keyword.trim(), category: next.category ?? browse.category, sort: next.sort ?? browse.sort })
  const openAdmission = (trial: TrialSkill) => { setView('trials'); setFocusKey(trialKey(trial)) }

  return (
    <div className="cl-market" data-testid="skill-market">
      <div className="cl-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={view === 'market'} className={view === 'market' ? 'active' : ''} onClick={() => setView('market')}>
          {t('market.tab.market')}
        </button>
        <button type="button" role="tab" aria-selected={view === 'trials'} className={view === 'trials' ? 'active' : ''} onClick={() => { setView('trials'); onRefreshTrials() }}>
          {t('market.tab.trials')} <b>{trials.length}</b>
        </button>
      </div>

      {view === 'market' && (
        <>
          <form className="cl-filters" onSubmit={event => { event.preventDefault(); submit({}) }}>
            <input type="search" value={keyword} aria-label={t('market.search')} placeholder={t('market.search')} onChange={event => setKeyword(event.target.value)} />
            <select aria-label={t('components.category')} value={browse.category} onChange={event => submit({ category: event.target.value })}>
              <option value="">{t('components.allCategories')}</option>
              {categories.map(item => <option key={item.key} value={item.key}>{categoryName.get(item.key)}</option>)}
            </select>
            <select aria-label={t('market.sort')} value={browse.sort} onChange={event => submit({ sort: event.target.value as 'stars' | 'downloads' })}>
              <option value="stars">{t('market.sortStars')}</option>
              <option value="downloads">{t('market.sortDownloads')}</option>
            </select>
            <button type="submit" className="capability-refresh-btn" disabled={!connected || browse.loading}>{browse.loading ? t('components.loading') : t('market.searchButton')}</button>
            <span className="cl-count" data-testid="market-count">
              {browse.keyword || browse.category ? t('market.found', { shown: browse.skills.length, total: browse.total }) : t('market.top', { shown: browse.skills.length })}
            </span>
          </form>
          <p className="cl-section-intro">{t('market.intro')}</p>
          {browse.error && <p className="cl-alert" role="alert" data-testid="market-error">{marketRefusalWords(t, browse.reason, browse.error)}</p>}
          {!browse.error && browse.skills.length === 0 && <p className="cl-empty">{browse.loading ? t('components.loading') : t('components.noMatch')}</p>}
          {browse.skills.length > 0 && (
            <div className="cl-grid">
              {browse.skills.map(skill => (
                <MarketCard key={skill.slug} skill={skill} category={categoryName.get(skill.category) ?? skill.category} trial={trialForListing(trials, skill.slug, skill.version)}
                  busy={busy[skill.slug] ?? (trialForListing(trials, skill.slug, skill.version) ? busy[trialKey(trialForListing(trials, skill.slug, skill.version)!)] : undefined)}
                  connected={connected} workflows={workflows} onDownload={onDownload} onUse={onUse} onDiscard={onDiscard} onRestore={onRestore}
                  onOpenAdmission={openAdmission} t={t} locale={locale} />
              ))}
            </div>
          )}
        </>
      )}

      {view === 'trials' && (
        <>
          <p className="cl-section-intro">{t('market.trialsIntro')}</p>
          {/* Worded here, not when it arrived: a refusal on screen follows the owner switching language. */}
          {trialError && (
            <p className="cl-alert" role="alert" data-testid="trial-error">
              {marketRefusalWords(t, trialError.reason, trialError.said, { preferSaid: trialError.preferSaid })}
            </p>
          )}
          {trials.length === 0 ? <p className="cl-empty">{t('market.noTrials')}</p> : (
            <div className="cl-grid cl-grid--wide">
              {trials.map(trial => (
                <TrialCard key={trialKey(trial)} trial={trial} busy={busy[trialKey(trial)]} connected={connected} fileView={fileView} focused={focusKey === trialKey(trial)}
                  workflows={workflows} onMark={onMark} onAdmit={onAdmit} onViewFile={onViewFile} onUse={onUse} onDiscard={onDiscard} onRestore={onRestore}
                  askSeq={mismatchAsk?.key === trialKey(trial) ? mismatchAsk.seq : 0} askChanged={mismatchAsk?.key === trialKey(trial) && mismatchAsk.changed === true}
                  onAskHandled={onMismatchAskHandled} t={t} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
