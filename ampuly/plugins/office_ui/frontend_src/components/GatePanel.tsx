import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useI18n } from '../i18n'
import { classifyPageError } from '../lib/pageError'
import { hasVisualDeliverable } from '../lib/revisionForm'

export interface GateFile {
  path: string
  kind: 'image' | 'text' | 'binary'
  data_url?: string
  content?: string
  size?: number
}

export interface GateDetail {
  checkpoint_id: string
  checkpoint_type: string
  task_id: string
  session_id: string
  order_id: string
  order_title: string
  node_id: string
  node_name: string
  node_type: string
  assignee: string
  version: number
  standard: string[]
  deliverables: string[]
  missing: string[]
  precheck: string[]
  revision_path: string
  prompt: string
  files: GateFile[]
  events: { kind: string; actor: string; at?: string }[]
  can_answer: boolean
}

export interface GateDecision {
  checkpoint_id: string
  decision: 'approve' | 'reject'
  exception_note?: string
  sheet?: {
    failed_standards: string[]
    chosen: string
    keep: string
    change: string
    add_remove: string
    directions: string[]
    verbal: string
  }
}

interface GatePanelProps {
  detail: GateDetail
  busy?: boolean
  connected?: boolean
  /** Why the last decision did not land.  A toast disappears; a refused
   *  approval has to stay on the panel the person is looking at. */
  error?: string | null
  /** Open straight on the reject sheet (the console's deep link from a 打回 reply, and tests). */
  initialMode?: 'review' | 'reject'
  onDecide: (decision: GateDecision) => void
  onClose: () => void
}

/** The console's half of a human gate: the same standard, deliverables and
 *  revision sheet as the Feishu card, so a person can close the loop here
 *  without opening a chat window. */
export function GatePanel({ detail, busy, connected = true, error, initialMode = 'review', onDecide, onClose }: GatePanelProps) {
  const { t } = useI18n()
  const [checked, setChecked] = useState<boolean[]>(() => detail.standard.map(() => false))
  const [exception, setException] = useState('')
  const [mode, setMode] = useState<'review' | 'reject'>(initialMode)
  const [failed, setFailed] = useState<boolean[]>(() => detail.standard.map(() => false))
  const [chosen, setChosen] = useState('')
  const [whereWrong, setWhereWrong] = useState('')
  const [keep, setKeep] = useState('')
  const [change, setChange] = useState('')
  const [addRemove, setAddRemove] = useState('')
  const [directions, setDirections] = useState('')
  const [openFile, setOpenFile] = useState<string>(() => detail.files[0]?.path ?? '')

  useEffect(() => {
    setChecked(detail.standard.map(() => false))
    setFailed(detail.standard.map(() => false))
    setMode('review')
    setOpenFile(detail.files[0]?.path ?? '')
  }, [detail.checkpoint_id, detail.version, detail.standard, detail.files])

  const allChecked = useMemo(() => checked.every(Boolean), [checked])
  const canApprove = connected && detail.can_answer && detail.missing.length === 0 && (allChecked || exception.trim().length > 0)
  const canReject = connected && detail.can_answer && (change.trim().length > 0 || whereWrong.trim().length > 0 || keep.trim().length > 0)
  const current = detail.files.find(f => f.path === openFile) ?? null
  // The reject sheet asks the plain three; a node that hands over pictures also gets the two design questions
  // (lib/revisionForm.ts -- the same rule as the Feishu card).
  const visual = detail.files.some(f => f.kind === 'image')
    || hasVisualDeliverable([...detail.files.map(f => f.path), ...detail.deliverables, ...detail.missing])

  return (
    <div className="gate-panel">
      <div className="gate-panel-head">
        <div>
          <div className="gate-panel-title">
            {detail.order_id || detail.order_title} · {detail.node_name} · {t('gate.version', { n: detail.version })}
          </div>
          <div className="gate-panel-sub">
            {t('gate.assignee', { who: detail.assignee || 'owner' })}
            {!detail.can_answer && <span className="gate-panel-warn"> · {t('gate.notYours')}</span>}
          </div>
        </div>
        <button className="gate-panel-close" onClick={onClose} type="button">✕</button>
      </div>

      <div className="gate-panel-body">
        {!connected && <div className="gate-panel-warn" role="status">{t('master.offline')}</div>}
        {error && (
          <div className={`gate-panel-error gate-panel-error--${classifyPageError(error)}`} role="alert" data-testid="gate-error">
            <strong>{t(`gate.error.${classifyPageError(error)}` as Parameters<typeof t>[0])}</strong>
            <span className="gate-panel-error-detail">{error}</span>
          </div>
        )}
        <div className="gate-panel-left">
          <div className="gate-panel-section-title">{t('gate.standard')}</div>
          {detail.standard.length === 0 && <div className="gate-panel-muted">{t('gate.noStandard')}</div>}
          <ul className="gate-panel-checklist">
            {detail.standard.map((item, i) => (
              <li key={i}>
                <label>
                  <input type="checkbox" disabled={!connected || busy} checked={checked[i] ?? false} onChange={e => setChecked(prev => prev.map((v, j) => (j === i ? e.target.checked : v)))} />
                  <span>{item}</span>
                </label>
              </li>
            ))}
          </ul>
          <div className={`gate-panel-precheck ${detail.precheck.length ? 'bad' : 'ok'}`}>
            {detail.precheck.length ? '⚠ ' + detail.precheck.join('；') : '✓ ' + t('gate.precheckOk', { n: detail.files.length })}
          </div>
          {detail.prompt && <div className="gate-panel-prompt">{detail.prompt}</div>}

          {mode === 'review' ? (
            <div className="gate-panel-actions">
              {!allChecked && (
                <input
                  className="gate-panel-input"
                  placeholder={t('gate.exceptionHint')}
                  value={exception}
                  disabled={!connected || busy}
                  onChange={e => setException(e.target.value)}
                />
              )}
              <button className="gate-btn gate-btn-approve" disabled={!canApprove || busy} onClick={() => onDecide({ checkpoint_id: detail.checkpoint_id, decision: 'approve', exception_note: allChecked ? '' : exception.trim() })}>
                {t('gate.approve')}
              </button>
              <button className="gate-btn gate-btn-reject" disabled={!connected || !detail.can_answer || busy} onClick={() => setMode('reject')}>
                {t('gate.reject')}
              </button>
            </div>
          ) : (
            <div className="gate-panel-sheet">
              <div className="gate-panel-section-title">{t('gate.sheet')}</div>
              <div className="gate-panel-field">
                <span>{t('gate.sheet.failed')}</span>
                {detail.standard.map((item, i) => (
                  <label key={i} className="gate-panel-inline">
                    <input type="checkbox" disabled={!connected || busy} checked={failed[i] ?? false} onChange={e => setFailed(prev => prev.map((v, j) => (j === i ? e.target.checked : v)))} />
                    <span>{item}</span>
                  </label>
                ))}
              </div>
              {detail.files.length > 1 && (
                <label className="gate-panel-field">
                  <span>{t('gate.sheet.chosen')}</span>
                  <select value={chosen} disabled={!connected || busy} onChange={e => setChosen(e.target.value)}>
                    <option value="">—</option>
                    {detail.files.map(f => <option key={f.path} value={f.path}>{f.path}</option>)}
                  </select>
                </label>
              )}
              <label className="gate-panel-field"><span>{t('gate.sheet.whereWrong')}</span><input className="gate-panel-input" disabled={!connected || busy} value={whereWrong} onChange={e => setWhereWrong(e.target.value)} placeholder={t('gate.sheet.whereWrongHint')} /></label>
              {visual && <label className="gate-panel-field"><span>{t('gate.sheet.keep')}</span><input className="gate-panel-input" disabled={!connected || busy} value={keep} onChange={e => setKeep(e.target.value)} /></label>}
              <label className="gate-panel-field"><span>{t('gate.sheet.change')}</span><textarea className="gate-panel-input" disabled={!connected || busy} rows={3} value={change} onChange={e => setChange(e.target.value)} placeholder={t(visual ? 'gate.sheet.changeHintVisual' : 'gate.sheet.changeHint')} /></label>
              {visual && <label className="gate-panel-field"><span>{t('gate.sheet.addRemove')}</span><input className="gate-panel-input" disabled={!connected || busy} value={addRemove} onChange={e => setAddRemove(e.target.value)} /></label>}
              <label className="gate-panel-field"><span>{t('gate.sheet.directions')}</span><input className="gate-panel-input" disabled={!connected || busy} value={directions} onChange={e => setDirections(e.target.value)} placeholder={t('gate.sheet.directionsHint')} /></label>
              <div className="gate-panel-actions">
                <button className="gate-btn" type="button" disabled={!connected || busy} onClick={() => setMode('review')}>{t('gate.back')}</button>
                <button
                  className="gate-btn gate-btn-reject"
                  disabled={!canReject || busy}
                  onClick={() => onDecide({
                    checkpoint_id: detail.checkpoint_id,
                    decision: 'reject',
                    // The plain questions land in the sheet's existing columns: 哪里不对 beside the ticked standard
                    // (不符合哪条标准), 要改成什么 as 修改什么, 补充说明 as 可选方向 -- the file keeps its shape.
                    sheet: {
                      failed_standards: [...detail.standard.filter((_, i) => failed[i]), ...(whereWrong.trim() ? [whereWrong.trim()] : [])],
                      chosen,
                      keep: keep.trim(),
                      change: change.trim(),
                      add_remove: addRemove.trim(),
                      directions: directions.split(/[；;\n]/).map(s => s.trim()).filter(Boolean).slice(0, 3),
                      verbal: '',
                    },
                  })}
                >
                  {t('gate.rejectSend')}
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="gate-panel-right">
          <div className="gate-panel-section-title">{t('gate.deliverables')}</div>
          <div className="gate-panel-files">
            {detail.files.map(f => (
              <button key={f.path} type="button" className={`gate-panel-file ${f.path === openFile ? 'active' : ''}`} onClick={() => setOpenFile(f.path)} disabled={!connected}>
                {f.kind === 'image' ? '🖼' : f.kind === 'text' ? '📄' : '📦'} {f.path}
              </button>
            ))}
            {detail.missing.map(m => <span key={m} className="gate-panel-file missing">✗ {m} · {t('gate.missing')}</span>)}
          </div>
          <div className="gate-panel-preview">
            {current?.kind === 'image' && current.data_url && <img src={current.data_url} alt={current.path} />}
            {current?.kind === 'text' && (
              <div className="gate-panel-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{current.content ?? ''}</ReactMarkdown></div>
            )}
            {current?.kind === 'binary' && <div className="gate-panel-muted">{t('gate.binary')}</div>}
            {!current && <div className="gate-panel-muted">{t('gate.noFiles')}</div>}
          </div>
        </div>
      </div>
    </div>
  )
}
