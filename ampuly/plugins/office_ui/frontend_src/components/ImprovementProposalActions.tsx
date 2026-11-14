import { useState } from 'react'

import { DUAL_RUN_METRICS } from '../lib/componentDetail'
import {
  DEFAULT_SAMPLE, benefitLines, improvementRefusal, proposalNext, sampleSizeOf,
  type ImprovementProposalRow,
} from '../lib/improvementProposal'

// 4.7-A11: what the owner does with one improvement proposal. The approval is judged on the last N finished real
// orders before and after (完成时长、打回次数、模型花费), read by the console -- nothing typed. A run-session dual run
// is offered as a trial beforehand; it is measured from the run records and does not replace the real-order check.

export interface ImprovementProposalActionHandlers {
  connected: boolean
  /** The proposal being acted on, '' when idle. */
  busy: string
  /** The last refusal on one proposal; it stays on that card until something on it succeeds. */
  refusal?: { proposalId: string; message: string } | null
  onEvaluate: (row: ImprovementProposalRow, sampleSize: number) => void
  onApprove: (row: ImprovementProposalRow, sampleSize: number) => void
  onOpenCandidate?: (row: ImprovementProposalRow) => void
  onBindStandard: (row: ImprovementProposalRow, standard: { hypothesis: string; metrics: Array<{ name: string; min_delta: number }>; min_samples: number }) => void
  onBindSessions: (row: ImprovementProposalRow, variant: 'baseline' | 'candidate', sessionIds: string[]) => void
  onRecordSessions: (row: ImprovementProposalRow, baselineSessionIds: string[], candidateSessionIds: string[], note: string) => void
}

const lines = (text: string) => text.split('\n').map(item => item.trim()).filter(Boolean)

function SessionTrial({ zh, row, disabled, props }: { zh: boolean; row: ImprovementProposalRow; disabled: boolean; props: ImprovementProposalActionHandlers }) {
  const [hypothesis, setHypothesis] = useState('')
  const [chosen, setChosen] = useState<Record<string, string>>({ completion_rate: '0.1' })
  const [samples, setSamples] = useState('3')
  const [baseline, setBaseline] = useState('')
  const [trial, setTrial] = useState('')
  const metrics = Object.entries(chosen).map(([name, value]) => ({ name, min_delta: Number(value) }))
  const count = Number(samples)
  const standardReady = hypothesis.trim().length > 0 && metrics.length > 0 && metrics.every(item => item.min_delta > 0 && item.min_delta <= 1) && Number.isInteger(count) && count >= 1 && count <= 1000
  return (
    <details className="cl-candidate-diff" data-testid="improvement-session-trial">
      <summary>{zh ? '先用运行会话双跑试一试（不算收益证据）' : 'Try a run-session dual run first (not benefit evidence)'}</summary>
      {!row.evaluation_spec_fingerprint ? (
        <div className="cl-candidate-form" data-testid="improvement-session-standard">
          <input value={hypothesis} placeholder={zh ? '新版本应该比旧版本好在哪' : 'How the new version should be better'} onChange={event => setHypothesis(event.target.value)} />
          {DUAL_RUN_METRICS.map(([name, zhLabel, enLabel]) => (
            <label key={name}>
              <input type="checkbox" checked={name in chosen} onChange={event => setChosen(current => {
                const copy = { ...current }
                if (event.target.checked) copy[name] = copy[name] ?? '0.1'; else delete copy[name]
                return copy
              })} />
              {zh ? zhLabel : enLabel}
              {name in chosen && <input type="number" min={0.01} max={1} step={0.01} value={chosen[name]} aria-label={zh ? '要求的提升' : 'Required improvement'} onChange={event => setChosen(current => ({ ...current, [name]: event.target.value }))} />}
            </label>
          ))}
          <label>{zh ? '每组会话数' : 'Sessions per group'}<input type="number" min={1} max={1000} step={1} value={samples} onChange={event => setSamples(event.target.value)} /></label>
          <button type="button" className="workflows-cancel" disabled={disabled || !standardReady} onClick={() => props.onBindStandard(row, { hypothesis: hypothesis.trim(), metrics, min_samples: count })}>
            {zh ? '定下双跑标准（定了不能改）' : 'Fix the standard (final)'}
          </button>
        </div>
      ) : (
        <div className="cl-candidate-form" data-testid="improvement-session-runs">
          <p className="cl-line">{zh
            ? '旧版、新版各一组运行会话，每组开跑之前先登记；两组都跑完后由系统从运行记录算指标并比对，不接受手填的数字。'
            : 'One group of run sessions per version, each registered before it runs; the system derives the measures from the run records.'}</p>
          <textarea rows={2} value={baseline} placeholder={zh ? '旧版那组的会话编号（每行一个）' : 'Old-version session ids (one per line)'} onChange={event => setBaseline(event.target.value)} />
          <button type="button" className="workflows-cancel" disabled={disabled || lines(baseline).length === 0} onClick={() => props.onBindSessions(row, 'baseline', lines(baseline))}>{zh ? '登记旧版那组（开跑前）' : 'Register the old group'}</button>
          <textarea rows={2} value={trial} placeholder={zh ? '新版那组的会话编号（每行一个）' : 'New-version session ids (one per line)'} onChange={event => setTrial(event.target.value)} />
          <button type="button" className="workflows-cancel" disabled={disabled || lines(trial).length === 0} onClick={() => props.onBindSessions(row, 'candidate', lines(trial))}>{zh ? '登记新版那组（开跑前）' : 'Register the new group'}</button>
          <button type="button" className="workflows-cancel" disabled={disabled || lines(baseline).length === 0 || lines(trial).length === 0} onClick={() => props.onRecordSessions(row, lines(baseline), lines(trial), '')}>
            {zh ? '两组都跑完了：比对并记录' : 'Both finished: compare and record'}
          </button>
        </div>
      )}
    </details>
  )
}

export function ImprovementProposalActions({ zh = true, row, props }: { zh?: boolean; row: ImprovementProposalRow; props: ImprovementProposalActionHandlers }) {
  const [samples, setSamples] = useState(String(DEFAULT_SAMPLE))
  const { next, note } = proposalNext(row)
  const busy = props.busy === row.proposal_id
  const disabled = busy || !props.connected
  const size = sampleSizeOf(samples)
  const benefit = row.benefit ?? null
  const refusal = props.refusal && props.refusal.proposalId === row.proposal_id ? improvementRefusal(props.refusal.message) : ''
  return (
    <div className="cl-improvement-actions" data-testid="improvement-proposal-actions" aria-busy={busy || undefined}>
      {benefit && (
        <div className="cl-line" data-testid="improvement-benefit" data-verdict={benefit.verdict}>
          <strong>{zh ? `真实订单对比（旧 ${benefit.baseline_count} 单 / 新 ${benefit.candidate_count} 单）` : `Real orders (old ${benefit.baseline_count} / new ${benefit.candidate_count})`}</strong>
          <p className="cl-line">{benefit.reason}</p>
          {benefitLines(benefit).length > 0 && <ul>{benefitLines(benefit).map(line => <li key={line}>{line}</li>)}</ul>}
        </div>
      )}
      {row.decision_reason && row.status !== 'materialized' && !benefit && <p className="cl-line">{row.decision_reason}</p>}
      {row.status === 'materialized' && (
        <div className="cl-candidate-form">
          <label>{zh ? '看最近几单（旧、新各）' : 'Latest orders per side'}
            <input type="number" min={1} max={50} step={1} value={samples} aria-label={zh ? '样本单数' : 'Orders per side'} onChange={event => setSamples(event.target.value)} />
          </label>
          <button type="button" className="workflows-cancel" data-testid="improvement-evaluate" disabled={disabled || size === null} onClick={() => size !== null && props.onEvaluate(row, size)}>
            {zh ? '按真实订单评估收益' : 'Judge on real orders'}
          </button>
          <button type="button" className="workflows-invoke" data-testid="improvement-approve" disabled={disabled || size === null || next === 'none'} onClick={() => size !== null && props.onApprove(row, size)}>
            {zh ? '批准' : 'Approve'}
          </button>
          {props.onOpenCandidate && row.candidate_id && (
            <button type="button" className="workflows-cancel" data-testid="improvement-open-candidate" onClick={() => props.onOpenCandidate?.(row)}>{zh ? '去编辑候选' : 'Edit the candidate'}</button>
          )}
        </div>
      )}
      {note && <p className="cl-line" data-testid="improvement-proposal-note">{note[zh ? 0 : 1]}</p>}
      {row.status === 'materialized' && <SessionTrial zh={zh} row={row} disabled={disabled} props={props} />}
      {refusal && <p className="cl-line cl-detail-error" role="alert" data-testid="improvement-proposal-refusal">{zh ? '没有做成：' : 'Refused: '}{refusal}</p>}
    </div>
  )
}
