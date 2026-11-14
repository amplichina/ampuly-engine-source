import { useState, type ReactNode } from 'react'

import {
  DUAL_RUN_METRICS, canActivateRows, candidateLadder, candidateNext, componentRefusal, nextMinor, skillInstructions,
  type ComponentCandidate, type ComponentDetail, type ComponentEditFields, type ComponentVersions,
} from '../lib/componentDetail'

// 组件 · 编辑 / 测试 / 发布 (4.7 A2-4) for one internal-library skill: the detail's 能力 / 契约 / 测试 / 发布 pages. An edit
// becomes an inert candidate beside the library; a verdict and a review are records; staging, activation and
// rollback are the existing skill-improvement calls and still need a frozen dual-run evaluation. 查看 never gets here:
// it stays the read-only panel.
// 4.7-A03: each candidate shows the whole chain in order -- 测试 → 双跑评估 → 批准 → 暂存 → 启用 → 回滚 -- and offers only
// the step that is open now; a failed test or dual run offers 修订 instead. Why approval is not open, and the last
// refusal, stay on the card in words.

export type WorkbenchTab = 'ability' | 'contract' | 'test' | 'publish'
export const WORKBENCH_TABS: ReadonlyArray<readonly [WorkbenchTab, string, string]> = [
  ['ability', '能力', 'Ability'], ['contract', '契约', 'Contract'], ['test', '测试', 'Test'], ['publish', '发布', 'Publish'],
]

export interface ComponentWorkbenchProps {
  initialTab: WorkbenchTab
  versions: ComponentVersions | null
  loading: boolean
  error: string | null
  /** '' when idle; 'edit' while an edit is being made into a candidate; a candidate id while one is acted on. */
  busy: string
  connected: boolean
  onEdit: (fields: { version: string; description: string; instructions: string; success_criteria: string[]; test_plan: string[] }, expectedFingerprint: string) => void
  onTest: (candidateId: string, outcome: 'passed' | 'failed', note: string) => void
  onReview: (candidateId: string, decision: 'approved' | 'rejected', note: string, expectedFingerprint: string) => void
  onStage: (candidateId: string) => void
  onActivate: (candidate: ComponentCandidate) => void
  onRollback: (candidate: ComponentCandidate) => void
  /** A failed candidate's fix, as a new draft that names it. */
  onRevise?: (candidate: ComponentCandidate, fields: ComponentEditFields, reason: string) => void
  /** Freeze how the dual run is judged, before any of its samples runs. */
  onEvaluationSpec?: (candidate: ComponentCandidate, spec: { hypothesis: string; metrics: Array<{ name: string; min_delta: number }>; min_samples: number }) => void
  /** Register one group of run sessions (old or new version) before it runs. */
  onEvaluationSessions?: (candidate: ComponentCandidate, variant: 'baseline' | 'candidate', sessionIds: string[]) => void
  /** Let the system compare the two finished groups and record the result on the candidate. */
  onEvaluationRecord?: (candidate: ComponentCandidate, baselineSessionIds: string[], candidateSessionIds: string[], note: string) => void
  /** The last refusal of something done to one candidate; it stays on that card until something on it succeeds. */
  refusal?: { candidateId: string; message: string } | null
}

const STATUS_WORDS: Record<string, readonly [string, string]> = {
  draft: ['待测试', 'Untested'], test_passed: ['测试通过', 'Test passed'], test_failed: ['测试没通过', 'Test failed'],
  approved: ['已批准', 'Approved'], rejected: ['已拒绝', 'Rejected'],
}
const ACTIVATION_WORDS: Record<string, readonly [string, string]> = {
  staged: ['已暂存', 'Staged'], activating: ['启用中', 'Activating'], active: ['已启用', 'Active'],
  rolling_back: ['回滚中', 'Rolling back'], rolled_back: ['已回滚', 'Rolled back'],
}
const ACTION_WORDS: Record<string, readonly [string, string]> = {
  created: ['生成', 'Made'], tested: ['测试', 'Tested'], revised: ['修订', 'Revised'], revision_of: ['由修订而来', 'Revision of'],
  evaluation_spec_bound: ['定评估标准', 'Standard frozen'], evaluation_recorded: ['双跑评估', 'Dual run'], reviewed: ['审批', 'Reviewed'],
}
const lines = (text: string) => text.split('\n').map(item => item.trim()).filter(Boolean)
// The records are stamped in UTC; a person reads their own clock.
function localStamp(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

function EditForm({ zh, detail, fingerprint, busy, connected, onEdit }: {
  zh: boolean; detail: ComponentDetail; fingerprint: string; busy: boolean; connected: boolean
  onEdit: ComponentWorkbenchProps['onEdit']
}) {
  const [version, setVersion] = useState(nextMinor(detail.version))
  const [description, setDescription] = useState(detail.summary)
  const [instructions, setInstructions] = useState(skillInstructions(detail.body))
  const [criteria, setCriteria] = useState(detail.contract.success_criteria.join('\n'))
  const [plan, setPlan] = useState('')
  // An edit starts from the text on this page. A cut text would come back shorter than the skill: not offered.
  if (detail.body_truncated) {
    return <p className="cl-line" data-testid="workbench-edit-cut">{zh ? '这个技能的正文太长，这里只显示了一部分：在这里编辑会丢掉后面的内容，所以不提供。' : 'This skill’s text is longer than this page shows: editing it here would drop the rest, so it is not offered.'}</p>
  }
  const ready = version.trim() && description.trim() && instructions.trim() && lines(criteria).length > 0 && lines(plan).length > 0 && fingerprint
  return (
    <form className="cl-workbench-edit" data-testid="workbench-edit" onSubmit={event => {
      event.preventDefault()
      if (!ready || busy || !connected) return
      onEdit({ version: version.trim(), description: description.trim(), instructions, success_criteria: lines(criteria), test_plan: lines(plan) }, fingerprint)
    }}>
      <p className="cl-line">{zh
        ? `编辑生成的是一个候选新版本（从 v${detail.version} 起），放在库旁边：正在使用的技能不变，直到它测试通过、审批、暂存并启用。`
        : `An edit makes a candidate new version (from v${detail.version}) beside the library: the skill in use does not change until it is tested, approved, staged and activated.`}</p>
      <label>{zh ? '新版本号' : 'New version'}<input value={version} onChange={event => setVersion(event.target.value)} /></label>
      <label>{zh ? '一句话用途' : 'What it is for'}<input value={description} maxLength={300} onChange={event => setDescription(event.target.value)} /></label>
      <label>{zh ? '说明（SKILL.md 正文）' : 'Instructions (SKILL.md body)'}<textarea rows={10} value={instructions} onChange={event => setInstructions(event.target.value)} /></label>
      <label>{zh ? '成功标准（每行一条）' : 'Success criteria (one per line)'}<textarea rows={3} value={criteria} onChange={event => setCriteria(event.target.value)} /></label>
      <label>{zh ? '测试计划（每行一条：怎么试、应当看到什么）' : 'Test plan (one per line: how to try it, what should happen)'}<textarea rows={3} value={plan} onChange={event => setPlan(event.target.value)} /></label>
      <button type="submit" className="workflows-invoke" data-testid="workbench-edit-submit" disabled={!ready || busy || !connected}>
        {busy ? (zh ? '生成中…' : 'Making…') : (zh ? '生成候选版本' : 'Make the candidate')}
      </button>
    </form>
  )
}

function Candidate({ zh, candidate, busy, connected, children }: { zh: boolean; candidate: ComponentCandidate; busy: boolean; connected: boolean; children?: ReactNode }) {
  const words = STATUS_WORDS[candidate.status] ?? [candidate.status, candidate.status]
  const activationWords = candidate.activation ? ACTIVATION_WORDS[candidate.activation.status] ?? ['切换中', 'Switching'] : null
  return (
    <li className="cl-candidate" data-candidate={candidate.candidate_id} data-status={candidate.status} aria-busy={busy || undefined}>
      <div className="cl-candidate-head">
        <b>v{candidate.version}</b>
        <small className="cl-meta"> · {zh ? `替代 v${candidate.current_version}` : `replaces v${candidate.current_version}`} · {candidate.source === 'owner_edit' ? (zh ? '你的编辑' : 'your edit') : (zh ? '改进提案' : 'improvement proposal')}{candidate.created_at ? ` · ${localStamp(candidate.created_at)}` : ''}</small>
        <span className="cl-chip">{(activationWords ?? words)[zh ? 0 : 1]}</span>
        {candidate.revises && <span className="cl-chip">{zh ? '修订版' : 'revision'}</span>}
        {candidate.superseded_by && <span className="cl-chip">{zh ? '已修订成新候选' : 'revised'}</span>}
        {!candidate.current && <span className="cl-chip cl-chip--warn">{zh ? '技能已变' : 'skill changed'}</span>}
        {!connected && <span className="cl-chip cl-chip--warn">{zh ? '未连接' : 'offline'}</span>}
      </div>
      <ol className="cl-candidate-steps" data-testid="workbench-steps" aria-label={zh ? '发布步骤' : 'Release steps'}>
        {candidateLadder(candidate).map(({ step, label, state }) => (
          <li key={step} data-step={step} data-state={state}>{label[zh ? 0 : 1]}{state === 'blocked' ? (zh ? '（没通过）' : ' (failed)') : ''}</li>
        ))}
      </ol>
      {candidate.test_note && <p className="cl-line">{zh ? '测试记录：' : 'Test record: '}{candidate.test_note}</p>}
      {candidate.evaluation_summary && <p className="cl-line" data-testid="workbench-evaluation-summary">{candidate.evaluation_summary}</p>}
      {candidate.diff && (
        <details className="cl-candidate-diff">
          <summary>{zh ? '看改了什么' : 'What changed'}</summary>
          <pre>{candidate.diff}</pre>
          {candidate.diff_truncated && <p className="cl-line">{zh ? '差异太长，只显示了开头。' : 'The diff is long; only its start is shown.'}</p>}
        </details>
      )}
      {children}
      {(candidate.history ?? []).length > 0 && (
        <details className="cl-candidate-history" data-testid="workbench-history">
          <summary>{zh ? '经过' : 'History'}</summary>
          <ol>
            {(candidate.history ?? []).map((entry, index) => (
              <li key={`${entry.at}-${index}`}>{localStamp(entry.at)} · {(ACTION_WORDS[entry.action] ?? [entry.action, entry.action])[zh ? 0 : 1]} · {entry.reason}{entry.actor ? `（${entry.actor}）` : ''}</li>
            ))}
          </ol>
        </details>
      )}
    </li>
  )
}

function ReviseForm({ zh, candidate, busy, connected, onRevise }: {
  zh: boolean; candidate: ComponentCandidate; busy: boolean; connected: boolean
  onRevise: (fields: ComponentEditFields, reason: string) => void
}) {
  const start = candidate.edit
  const [version, setVersion] = useState(start?.version ?? candidate.version)
  const [description, setDescription] = useState(start?.description ?? '')
  const [instructions, setInstructions] = useState(start?.instructions ?? '')
  const [criteria, setCriteria] = useState((start?.success_criteria ?? []).join('\n'))
  const [plan, setPlan] = useState((start?.test_plan ?? []).join('\n'))
  const [reason, setReason] = useState('')
  const ready = version.trim() && description.trim() && instructions.trim() && lines(criteria).length > 0 && lines(plan).length > 0 && reason.trim().length >= 8
  return (
    <form className="cl-workbench-edit" data-testid="workbench-revise-form" onSubmit={event => {
      event.preventDefault()
      if (!ready || busy || !connected) return
      onRevise({ version: version.trim(), description: description.trim(), instructions, success_criteria: lines(criteria), test_plan: lines(plan) }, reason.trim())
    }}>
      <p className="cl-line">{zh
        ? '从这个候选的内容改起。修订会生成一个新候选（它要重新测试、重新做双跑评估）；这个候选和它的记录原样保留。'
        : 'Start from this candidate’s text. A revision is a new candidate (tested and dual-run again); this one and its records stay as they are.'}</p>
      <label>{zh ? '版本号' : 'Version'}<input value={version} onChange={event => setVersion(event.target.value)} /></label>
      <label>{zh ? '一句话用途' : 'What it is for'}<input value={description} maxLength={300} onChange={event => setDescription(event.target.value)} /></label>
      <label>{zh ? '说明（SKILL.md 正文）' : 'Instructions (SKILL.md body)'}<textarea rows={8} value={instructions} onChange={event => setInstructions(event.target.value)} /></label>
      <label>{zh ? '成功标准（每行一条）' : 'Success criteria (one per line)'}<textarea rows={3} value={criteria} onChange={event => setCriteria(event.target.value)} /></label>
      <label>{zh ? '测试计划（每行一条）' : 'Test plan (one per line)'}<textarea rows={3} value={plan} onChange={event => setPlan(event.target.value)} /></label>
      <label>{zh ? '这次改了什么（至少 8 个字）' : 'What this revision changes (8+ characters)'}<input value={reason} onChange={event => setReason(event.target.value)} /></label>
      <button type="submit" className="workflows-invoke" data-testid="workbench-revise-submit" disabled={!ready || busy || !connected}>{zh ? '修订成新候选' : 'Revise into a new candidate'}</button>
    </form>
  )
}

function EvaluationSpecForm({ zh, busy, connected, onSubmit }: {
  zh: boolean; busy: boolean; connected: boolean
  onSubmit: (spec: { hypothesis: string; metrics: Array<{ name: string; min_delta: number }>; min_samples: number }) => void
}) {
  const [hypothesis, setHypothesis] = useState('')
  const [chosen, setChosen] = useState<Record<string, string>>({ completion_rate: '0.1' })
  const [samples, setSamples] = useState('3')
  const metrics = Object.entries(chosen).map(([name, value]) => ({ name, min_delta: Number(value) }))
  const count = Number(samples)
  const ready = hypothesis.trim().length > 0 && metrics.length > 0 && metrics.every(item => item.min_delta > 0 && item.min_delta <= 1) && Number.isInteger(count) && count >= 1 && count <= 1000
  return (
    <div className="cl-candidate-form" data-testid="workbench-evaluation-spec-form">
      <p className="cl-line">{zh
        ? '先定评估标准：比较什么、新版要好多少、每组至少几个样本。评估对象就是这个候选本身，由系统填；标准一旦定下就不能改。'
        : 'First fix the standard: what is compared, how much better the new version must be, how many samples per group. What is measured is this candidate itself, filled in by the system; once fixed it cannot change.'}</p>
      <input value={hypothesis} placeholder={zh ? '这一版应该比旧版好在哪' : 'How the new version should be better'} onChange={event => setHypothesis(event.target.value)} />
      {DUAL_RUN_METRICS.map(([name, zhLabel, enLabel]) => (
        <label key={name}>
          <input type="checkbox" checked={name in chosen} onChange={event => setChosen(current => {
            const copy = { ...current }
            if (event.target.checked) copy[name] = copy[name] ?? '0.1'; else delete copy[name]
            return copy
          })} />
          {zh ? zhLabel : enLabel}
          {name in chosen && (
            <>
              {zh ? '至少变好' : 'by at least'}
              <input type="number" min={0.01} max={1} step={0.01} value={chosen[name]} aria-label={zh ? '要求的提升' : 'Required improvement'} onChange={event => setChosen(current => ({ ...current, [name]: event.target.value }))} />
            </>
          )}
        </label>
      ))}
      <label>{zh ? '每组样本数' : 'Samples per group'}<input type="number" min={1} max={1000} step={1} value={samples} onChange={event => setSamples(event.target.value)} /></label>
      <button type="button" className="workflows-invoke" data-testid="workbench-evaluation-spec-submit" disabled={!ready || busy || !connected} onClick={() => onSubmit({ hypothesis: hypothesis.trim(), metrics, min_samples: count })}>
        {zh ? '定下评估标准（定了不能改）' : 'Fix the standard (final)'}
      </button>
    </div>
  )
}

function DualRunForm({ zh, candidate, busy, connected, onSessions, onRecord }: {
  zh: boolean; candidate: ComponentCandidate; busy: boolean; connected: boolean
  onSessions: (variant: 'baseline' | 'candidate', sessionIds: string[]) => void
  onRecord: (baselineSessionIds: string[], candidateSessionIds: string[], note: string) => void
}) {
  const [baseline, setBaseline] = useState('')
  const [trial, setTrial] = useState('')
  const [note, setNote] = useState('')
  const standard = candidate.evaluation_standard
  const need = standard?.min_samples ?? 1
  const baseIds = lines(baseline)
  const trialIds = lines(trial)
  const disabled = busy || !connected
  return (
    <div className="cl-candidate-form" data-testid="workbench-dual-run-form">
      {standard && (
        <p className="cl-line">{zh ? '评估标准：' : 'Standard: '}{standard.metrics.map(item => `${item.label} ${zh ? '至少变好' : 'better by'} ${item.min_delta}`).join(zh ? '；' : '; ')}{zh ? `；每组至少 ${need} 个样本。` : `; at least ${need} samples per group.`}</p>
      )}
      <p className="cl-line">{zh
        ? '旧版、新版各一组运行会话。每组要在开跑之前先登记到这套标准上（跑完再登记不算数）；两组都跑完后，由系统从运行记录算出指标并比对，不接受手填的数字。会话编号在运行记录里。'
        : 'One group of run sessions for the old version and one for the new. Register each group against this standard before it runs (registering afterwards does not count); when both have finished, the system derives the measures from the run records and compares them -- no typed numbers. Session ids are in the run records.'}</p>
      <textarea rows={3} value={baseline} placeholder={zh ? '旧版那组的会话编号（每行一个）' : 'Old-version session ids (one per line)'} onChange={event => setBaseline(event.target.value)} />
      <button type="button" className="workflows-cancel" disabled={disabled || baseIds.length < need} onClick={() => onSessions('baseline', baseIds)}>{zh ? '登记旧版那组（开跑前）' : 'Register the old group (before it runs)'}</button>
      <textarea rows={3} value={trial} placeholder={zh ? '新版那组的会话编号（每行一个）' : 'New-version session ids (one per line)'} onChange={event => setTrial(event.target.value)} />
      <button type="button" className="workflows-cancel" disabled={disabled || trialIds.length < need} onClick={() => onSessions('candidate', trialIds)}>{zh ? '登记新版那组（开跑前）' : 'Register the new group (before it runs)'}</button>
      <input value={note} placeholder={zh ? '说明（可空）' : 'Note (optional)'} onChange={event => setNote(event.target.value)} />
      <button type="button" className="workflows-invoke" data-testid="workbench-dual-run-record" disabled={disabled || baseIds.length < need || trialIds.length < need} onClick={() => onRecord(baseIds, trialIds, note.trim())}>
        {zh ? '两组都跑完了：比对并记录' : 'Both groups finished: compare and record'}
      </button>
    </div>
  )
}

/** The open step of one candidate: its form or button, the reason the rest is closed, and the last refusal. */
function CandidateActions({ zh, candidate, props }: { zh: boolean; candidate: ComponentCandidate; props: ComponentWorkbenchProps }) {
  const { next, note } = candidateNext(candidate)
  const busy = props.busy === candidate.candidate_id
  const connected = props.connected
  const refusal = props.refusal && props.refusal.candidateId === candidate.candidate_id ? componentRefusal(props.refusal.message) : ''
  return (
    <>
      {next === 'test' && (
        <>
          <p className="cl-line">{zh ? '第一步：照测试计划试一次，记下试了什么、看到了什么。' : 'First: try it by the test plan and record what was tried and what happened.'}</p>
          <TestForm zh={zh} busy={busy} connected={connected} onTest={(outcome, testNote) => props.onTest(candidate.candidate_id, outcome, testNote)} />
        </>
      )}
      {next === 'revise' && props.onRevise && <ReviseForm zh={zh} candidate={candidate} busy={busy} connected={connected} onRevise={(fields, reason) => props.onRevise?.(candidate, fields, reason)} />}
      {next === 'evaluation_spec' && props.onEvaluationSpec && <EvaluationSpecForm zh={zh} busy={busy} connected={connected} onSubmit={spec => props.onEvaluationSpec?.(candidate, spec)} />}
      {next === 'evaluation_run' && props.onEvaluationSessions && props.onEvaluationRecord && (
        <DualRunForm zh={zh} candidate={candidate} busy={busy} connected={connected}
          onSessions={(variant, ids) => props.onEvaluationSessions?.(candidate, variant, ids)}
          onRecord={(baseIds, trialIds, recordNote) => props.onEvaluationRecord?.(candidate, baseIds, trialIds, recordNote)} />
      )}
      {next === 'review' && <ReviewForm zh={zh} busy={busy} connected={connected} onReview={(decision, reviewNote) => props.onReview(candidate.candidate_id, decision, reviewNote, candidate.candidate_fingerprint)} />}
      {next === 'stage' && <button type="button" className="workflows-cancel" disabled={busy || !connected} onClick={() => props.onStage(candidate.candidate_id)}>{zh ? '暂存（封存新旧两版）' : 'Stage (seal both versions)'}</button>}
      {next === 'activate' && <button type="button" className="workflows-invoke" disabled={busy || !connected} onClick={() => props.onActivate(candidate)}>{zh ? '启用' : 'Activate'}</button>}
      {next === 'rollback' && <button type="button" className="workflows-cancel" disabled={busy || !connected} onClick={() => props.onRollback(candidate)}>{zh ? '回滚到上一版' : 'Roll back'}</button>}
      {note && <p className="cl-line" data-testid="workbench-candidate-note">{note[zh ? 0 : 1]}</p>}
      {refusal && <p className="cl-line cl-detail-error" role="alert" data-testid="workbench-candidate-refusal">{zh ? '没有做成：' : 'Refused: '}{refusal}</p>}
    </>
  )
}

/** 能启用了吗 (S3-5): once a candidate has a test verdict, the four yes / no rows behind 启用 under the question as the
 *  title (the design's 「测过才出现验收准入」, worded for a component). It reads the same ladder the card shows and opens nothing. */
function CanActivate({ zh, candidate }: { zh: boolean; candidate: ComponentCandidate }) {
  const { tested, rows, ready } = canActivateRows(candidate)
  if (!tested) return null
  const { next } = candidateNext(candidate)
  const activation = candidate.activation
  const line = next === 'activate'
    ? (zh ? '四项都齐了：下面的「启用」现在能按。' : 'All four are yes: 启用 below is open now.')
    : activation && (activation.status === 'active' || activation.status === 'activating')
      ? (zh ? '已经启用了；要撤回就用下面的「回滚」。' : 'Already activated; 回滚 below takes it back.')
      : activation && (activation.status === 'rolled_back' || activation.status === 'rolling_back')
        ? (zh ? '已回滚到上一版。' : 'Rolled back to the previous version.')
        : (zh ? '有一项还没齐，启用不开；缺的那一步在「测试」页上做。' : 'One is not yes yet, so activation stays closed; the missing step is done on the Test page.')
  return (
    <section className="cl-can-activate" data-testid="workbench-can-activate" data-ready={ready ? 'true' : 'false'} aria-label={zh ? '能启用了吗' : 'Can it be activated?'}>
      <h4>{zh ? '能启用了吗' : 'Can it be activated?'}</h4>
      <ol>
        {rows.map(row => (
          <li key={row.step} data-step={row.step} data-answer={row.state}><span>{row.question[zh ? 0 : 1]}</span><b>{row.answer[zh ? 0 : 1]}</b></li>
        ))}
      </ol>
      <p className="cl-line">{line}</p>
    </section>
  )
}

function TestForm({ zh, busy, connected, onTest }: { zh: boolean; busy: boolean; connected: boolean; onTest: (outcome: 'passed' | 'failed', note: string) => void }) {
  const [outcome, setOutcome] = useState<'passed' | 'failed'>('passed')
  const [note, setNote] = useState('')
  const ok = note.trim().length >= 8
  return (
    <div className="cl-candidate-form" data-testid="workbench-test-form">
      <select value={outcome} onChange={event => setOutcome(event.target.value === 'failed' ? 'failed' : 'passed')} aria-label={zh ? '结论' : 'Verdict'}>
        <option value="passed">{zh ? '通过' : 'Passed'}</option>
        <option value="failed">{zh ? '不通过' : 'Failed'}</option>
      </select>
      <input value={note} placeholder={zh ? '试了什么、看到了什么（至少 8 个字）' : 'What was tried and what happened (8+ characters)'} onChange={event => setNote(event.target.value)} />
      <button type="button" className="workflows-cancel" disabled={!ok || busy || !connected} onClick={() => onTest(outcome, note.trim())}>{zh ? '记录测试结论' : 'Record the verdict'}</button>
    </div>
  )
}

function ReviewForm({ zh, busy, connected, onReview }: { zh: boolean; busy: boolean; connected: boolean; onReview: (decision: 'approved' | 'rejected', note: string) => void }) {
  const [note, setNote] = useState('')
  return (
    <div className="cl-candidate-form" data-testid="workbench-review-form">
      <input value={note} placeholder={zh ? '审批意见（可空）' : 'Review note (optional)'} onChange={event => setNote(event.target.value)} />
      <button type="button" className="workflows-invoke" disabled={busy || !connected} onClick={() => onReview('approved', note.trim())}>{zh ? '批准' : 'Approve'}</button>
      <button type="button" className="workflows-cancel" disabled={busy || !connected} onClick={() => onReview('rejected', note.trim())}>{zh ? '拒绝' : 'Reject'}</button>
    </div>
  )
}

export function ComponentWorkbench({ zh, detail, ability, contract, props }: {
  zh: boolean
  detail: ComponentDetail
  /** The read-only sections, as the 查看 panel shows them. */
  ability: ReactNode
  contract: ReactNode
  props: ComponentWorkbenchProps
}) {
  const [tab, setTab] = useState<WorkbenchTab>(props.initialTab)
  const candidates = props.versions?.candidates ?? []
  const status = props.loading
    ? <p className="cl-empty">{zh ? '正在读取版本…' : 'Reading the versions…'}</p>
    : props.error ? <p className="cl-empty cl-detail-error" role="alert">{props.error}</p> : null
  return (
    <div className="cl-workbench" data-testid="component-workbench" data-tab={tab}>
      <div className="cl-workbench-tabs" role="tablist">
        {WORKBENCH_TABS.map(([key, zhLabel, enLabel]) => (
          <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? 'is-active' : ''} data-tab={key} onClick={() => setTab(key)}>{zh ? zhLabel : enLabel}</button>
        ))}
      </div>
      <div className="cl-detail-body">
        {tab === 'ability' && ability}
        {tab === 'contract' && (
          <>
            {contract}
            <section>
              <h3>{zh ? '编辑成新版本' : 'Edit into a new version'}</h3>
              {status ?? <EditForm zh={zh} detail={detail} fingerprint={props.versions?.fingerprint ?? ''} busy={props.busy === 'edit'} connected={props.connected} onEdit={props.onEdit} />}
            </section>
          </>
        )}
        {tab === 'test' && (
          <section data-testid="workbench-test">
            <h3>{zh ? '测试与评估' : 'Test and evaluate'}</h3>
            <p className="cl-line">{zh
              ? '每个候选按顺序走：测试 → 双跑评估 → 批准 → 暂存 → 启用，需要时回滚。只有现在能做的那一步会亮；测试或双跑评估没通过，就修订成新候选重新走。'
              : 'Each candidate goes in order: test → dual run → approve → stage → activate, and roll back if needed. Only the step open now is offered; a failed test or dual run is revised into a new candidate that goes again.'}</p>
            {status}
            {!status && candidates.length === 0 && <p className="cl-empty">{zh ? '还没有候选版本：先在「契约」里编辑一个。' : 'No candidate yet: make one under Contract.'}</p>}
            <ul className="cl-candidates">
              {candidates.map(candidate => (
                <Candidate key={candidate.candidate_id} zh={zh} candidate={candidate} busy={props.busy === candidate.candidate_id} connected={props.connected}>
                  <CandidateActions zh={zh} candidate={candidate} props={props} />
                </Candidate>
              ))}
            </ul>
          </section>
        )}
        {tab === 'publish' && (
          <section data-testid="workbench-publish">
            <h3>{zh ? `发布（库里现在是 v${detail.version}）` : `Publish (the library holds v${detail.version})`}</h3>
            <p className="cl-line">{zh
              ? `启用会替换共享库里的这个技能。使用它的 ${detail.used_on.length} 个步骤锁定的是它们保存时的版本号：启用后会显示版本不一致，要用新版本就在搭建台逐个确认并保存一次新版本。在途工单是否受影响未经实测。`
              : `Activation replaces this skill in the shared library. The ${detail.used_on.length} steps using it lock the version they were saved with: after activation they show a version mismatch, and each is moved on by saving a new version on the builder. Whether orders in flight are affected has not been tried.`}</p>
            {status}
            {!status && candidates.length === 0 && <p className="cl-empty">{zh ? '没有待发布的候选版本。' : 'No candidate to publish.'}</p>}
            <ul className="cl-candidates">
              {candidates.map(candidate => (
                <Candidate key={candidate.candidate_id} zh={zh} candidate={candidate} busy={props.busy === candidate.candidate_id} connected={props.connected}>
                  <CanActivate zh={zh} candidate={candidate} />
                  <CandidateActions zh={zh} candidate={candidate} props={props} />
                </Candidate>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  )
}
