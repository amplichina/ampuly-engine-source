import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'

import { ADMISSION_STAGE_LABELS, TEST_STEP_LABELS, type AdmissionView, type FlowTestView } from '../lib/flowAdmission'
import { versionsView, type FlowVersions } from '../lib/flowVersions'

// 搭建台's 测试 and 验收准入 (4.7 A2-2). Each is a view of one saved flow plus the transitions that already exist on
// its package card -- the same callbacks, the same server checks. Nothing here saves a draft, runs a paid step on its
// own, or publishes without the owner's click.

type Preflight = { ready: boolean; blockers: string[]; warnings?: string[]; steps: unknown[]; focus_node_id?: string; missing_fields?: string[] }

// A long list is shown by its head; the rest is counted and stays one click away on the package view, where the
// full preflight panel already lives.
const SHOWN = 6

function ShortList({ items, soft, zh }: { items: readonly string[]; soft?: boolean; zh: boolean }) {
  if (!items.length) return null
  const head = items.slice(0, SHOWN)
  return (
    <ul className={soft ? 'wf-action-soft' : undefined}>
      {head.map(item => <li key={item}>{item}</li>)}
      {items.length > SHOWN && <li className="wf-action-more">{zh ? `另有 ${items.length - SHOWN} 条（完整列表在「工作流包」视图）` : `${items.length - SHOWN} more (the full list is in the Package view)`}</li>}
    </ul>
  )
}
type TestRecord = { outcome: string; execution_mode?: string; date?: string; notes?: string } | null

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function DialogFrame({ testId, title, sub, onClose, zh, children }: {
  testId: string; title: string; sub: string; onClose: () => void; zh: boolean; children: ReactNode
}) {
  // The keyboard stays in the dialog while it is open (Tab and Shift+Tab go round inside it) and goes back to the
  // button that opened it when it closes.
  const frameRef = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    frameRef.current?.querySelector<HTMLElement>('.wf-action-head button')?.focus()
    return () => { opener?.focus?.() }
  }, [])
  const keepFocus = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab' || !frameRef.current) return
    const items = [...frameRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }
  return (
    <>
      <div className="wf-action-scrim" onClick={onClose} aria-hidden="true" />
      <section className="wf-action-dialog" role="dialog" aria-modal="true" aria-label={title} data-testid={testId} ref={frameRef} onKeyDown={keepFocus}>
        <header className="wf-action-head">
          <div><strong>{title}</strong><small>{sub}</small></div>
          <button type="button" onClick={onClose} aria-label={zh ? '关闭' : 'Close'}>×</button>
        </header>
        <div className="wf-action-body">{children}</div>
      </section>
    </>
  )
}

function recordLine(record: TestRecord, zh: boolean): string {
  if (!record) return zh ? '还没有试跑记录' : 'No test record yet'
  const outcome = record.outcome === 'passed' ? (zh ? '通过' : 'passed') : record.outcome === 'failed' ? (zh ? '未通过' : 'failed') : record.outcome
  const mode = record.execution_mode === 'simulated' ? (zh ? '（合同模拟）' : ' (contract simulation)') : record.execution_mode === 'provider' ? (zh ? '（真实模型）' : ' (real model)') : ''
  return `${record.date ? `${record.date} · ` : ''}${outcome}${mode}`
}

export type PublishedFlowTrialCandidate = {
  package_id: string
  package_version: string
  package_fingerprint: string
  workflow_id: string
  workflow_version: string
  name: string
}

export type FlowTrialTarget = {
  source: 'saved_draft' | 'published_employee'
  workflowVersion: string
  packageId?: string
  fingerprint?: string
  /** Continue/stop always address the existing round, never create another one. */
  flowId?: string
}

export type FlowTrialSummary = {
  flow_id: string
  workflow_id: string
  workflow_version: string
  state: string
  phase?: string
  phase_zh?: string
  request_id?: string
  budget_refused?: boolean
  budget?: { per_node?: number; total?: number | null; consumed?: number; sentence_zh?: string; refused?: boolean }
  resume_from?: string
  counts_as_evidence?: boolean
  model_calls_total?: number
  stopped_at?: string
  created_at?: string
  freeze?: { freeze_source?: string; package_id?: string; package_fingerprint?: string; package_version?: string; workflow_version?: string }
} | null

export function flowTrialTargetForRecord(trial: NonNullable<FlowTrialSummary>): FlowTrialTarget {
  const pin = trial.freeze
  return {
    source: pin ? 'published_employee' : 'saved_draft', workflowVersion: trial.workflow_version, flowId: trial.flow_id,
    ...(pin ? { packageId: pin.package_id, fingerprint: pin.package_fingerprint } : {}),
  }
}

function FlowTrialControls({ zh, connected, draftOpen, busy, steps, savedVersion, trial, onFlowTrial, publishedCandidates, publishedCandidatesError }: {
  zh: boolean
  connected: boolean
  draftOpen: boolean
  busy: boolean
  steps: number
  savedVersion: string
  trial: FlowTrialSummary
  publishedCandidates: readonly PublishedFlowTrialCandidate[]
  publishedCandidatesError?: string | null
  onFlowTrial?: (action: 'start' | 'continue' | 'new_round' | 'stop', limits: { perNode: number; total: number }, target: FlowTrialTarget) => void
}) {
  const stepCount = Math.max(steps, 1)
  const [perNode, setPerNode] = useState(12)
  const [total, setTotal] = useState(Math.min(200, 12 * stepCount))
  const [source, setSource] = useState<FlowTrialTarget['source']>('saved_draft')
  const [selectedPin, setSelectedPin] = useState('')
  const candidate = publishedCandidates.find(item => `${item.package_id}:${item.package_fingerprint}` === selectedPin)
  const target: FlowTrialTarget | null = source === 'saved_draft'
    ? { source, workflowVersion: savedVersion }
    : candidate && !publishedCandidatesError ? { source, workflowVersion: candidate.workflow_version, packageId: candidate.package_id, fingerprint: candidate.package_fingerprint } : null
  const phase = trial?.phase || (trial?.state === 'waiting_for_answer' ? 'waiting' : trial?.state === 'running' || trial?.state === 'queued' ? 'running' : trial ? 'finished' : '')
  const sameVersion = Boolean(trial && target && trial.workflow_version === target.workflowVersion
    && (trial.freeze?.package_fingerprint || '') === (target.fingerprint || ''))
  // A published round remains resumable after the mutable canvas moves on.
  const active = Boolean(trial) && (phase === 'accepted' || phase === 'running' || phase === 'waiting' || phase === 'unknown')
  const limits = { perNode: Math.min(20, Math.max(1, perNode)), total: Math.min(200, Math.max(1, total)) }
  const anotherRound = sameVersion && Boolean(trial) && !active
  return (
    <>
      {onFlowTrial && !active && <fieldset disabled={busy} data-testid="flow-trial-source">
        <legend>{zh ? '这次测什么' : 'What to test'}</legend>
        <label><input type="radio" name="flow-trial-source" checked={source === 'saved_draft'} onChange={() => setSource('saved_draft')} />{zh ? '试已保存草稿' : 'Try saved draft'}</label>
        {' · '}
        <label><input type="radio" name="flow-trial-source" checked={source === 'published_employee'} onChange={() => setSource('published_employee')} />{zh ? '验证已发布版本' : 'Verify published version'}</label>
        {source === 'published_employee' && <p>
          <label>{zh ? '已发布岗位包' : 'Published employee package'} <select data-testid="flow-trial-published-select" value={selectedPin} disabled={!connected || Boolean(publishedCandidatesError)} onChange={event => setSelectedPin(event.target.value)}>
            <option value="">{zh ? '选择已发布版本' : 'Select a published version'}</option>
            {publishedCandidates.map(item => <option key={`${item.package_id}:${item.package_fingerprint}`} value={`${item.package_id}:${item.package_fingerprint}`}>{item.name || item.package_id} · {item.package_version} · {zh ? '流程' : 'flow'} {item.workflow_version}</option>)}
          </select></label>
        </p>}
        {publishedCandidatesError && <p className="wf-action-warn" role="status">{zh ? '已发布目录暂时无法核实，仍可试草稿；请刷新后再选择发布版。' : 'The published catalog cannot be verified. Draft trials remain available; refresh before choosing a publication.'}</p>}
        {!publishedCandidatesError && !publishedCandidates.length && <p className="wf-action-soft">{zh ? '还没有可选择的发布版。先试草稿；岗位包发布后再验证其冻结版本。' : 'No published version is available. Try the draft first, then verify a published employee package.'}</p>}
        <p className="wf-action-soft">{source === 'saved_draft'
          ? (zh ? `试已保存的 v${savedVersion}，不等同于已发布岗位包验收。` : `Tries saved v${savedVersion}; this is not a published employee package acceptance.`)
          : (zh ? '按发布时冻结的流程运行，不受当前草稿修改影响；服务端会重新核对项目、版本与发布指纹。' : 'Runs the graph frozen at publication, independent of the current draft. The server rechecks project, version and fingerprint.')}</p>
      </fieldset>}
      <p>{zh
        ? '按依赖顺序真实跑完全部 AI 步骤。下游用这一轮上游留下的产物。人做的步骤按已放行处理；有一步没通过就停。受理还不是跑完。'
        : 'Runs every AI step in dependency order. Each step uses what the step before it left in this same run. A person’s step is taken as released. The run stops at the first step that does not pass. Accepted is not finished.'}</p>
      <p className="wf-action-soft">{zh ? '会调用已配置模型，按实际调用计费；这里限制调用次数，不等同于金额预算。' : 'Uses configured models with actual usage charges. These limits cap calls, not currency.'}</p>
      {onFlowTrial && !active && (
        <p className="wf-action-soft" data-testid="flow-trial-budget">
          <label>{zh ? '每步最多' : 'Per step'} <input data-testid="flow-trial-per-node" type="number" min={1} max={20} value={perNode} onChange={event => setPerNode(Number(event.target.value))} /> {zh ? '次' : 'calls'}</label>
          {' · '}
          <label>{zh ? '整次最多' : 'Whole run'} <input data-testid="flow-trial-total" type="number" min={1} max={200} value={total} onChange={event => setTotal(Number(event.target.value))} /> {zh ? '次' : 'calls'}</label>
        </p>
      )}
      {trial?.budget?.sentence_zh && <p className="wf-action-soft" data-testid="flow-trial-budget-live">{trial.budget.sentence_zh}</p>}
      {trial?.budget_refused && <p className="wf-action-warn" role="status" data-testid="flow-trial-budget-refused">{zh ? '整次次数已用完，这次没有继续。' : 'The whole-run limit is spent, so this run did not continue.'}</p>}
      {onFlowTrial && !active && (
        <button type="button" className="workflows-invoke" data-testid={anotherRound ? 'flow-trial-new-round' : 'flow-trial-start'} disabled={!connected || busy || !target || (source === 'saved_draft' && draftOpen)} onClick={() => { if (target) onFlowTrial(anotherRound ? 'new_round' : 'start', limits, target) }}>
          {busy ? (zh ? '正在提交…' : 'Sending…') : anotherRound ? (zh ? '另开一轮' : 'Start another round') : (zh ? '整流程试跑' : 'Run whole-flow trial')}
        </button>
      )}
      {onFlowTrial && (phase === 'accepted' || phase === 'running') && (
        <button type="button" className="workflows-cancel" data-testid="flow-trial-progress" disabled>{zh ? '查看进度' : 'View progress'}{trial?.phase_zh ? ` · ${trial.phase_zh}` : ''}</button>
      )}
      {onFlowTrial && phase === 'waiting' && (
        <button type="button" className="workflows-invoke" data-testid="flow-trial-continue" disabled={!connected || busy} onClick={() => { if (trial) onFlowTrial('continue', limits, flowTrialTargetForRecord(trial)) }}>
          {zh ? '回答后继续' : 'Continue after the answer'}
        </button>
      )}
      {onFlowTrial && phase === 'unknown' && (
        <p className="wf-action-warn" role="status" data-testid="flow-trial-unknown">{trial?.phase_zh || (zh ? '结果还不能确定，先核验，不要重跑。' : 'The result is not known yet. Check it before running again.')}</p>
      )}
      {onFlowTrial && active && phase !== 'unknown' && (
        <button type="button" className="workflows-cancel" data-testid="flow-trial-stop" disabled={!connected || busy} onClick={() => { if (trial) onFlowTrial('stop', limits, flowTrialTargetForRecord(trial)) }}>
          {zh ? '停止' : 'Stop'}
        </button>
      )}
      {draftOpen && onFlowTrial && !active && source === 'saved_draft' && (
        <p className="wf-action-warn" role="status">{zh
          ? '有未保存改动时不能开整流程试跑：先保存草稿。'
          : 'A whole-flow trial cannot start while the canvas has unsaved changes: save the draft first.'}</p>
      )}
      {trial && (
        <div className="wf-action-result" role="status" data-testid="flow-trial-latest">
          <strong>{zh ? '这一轮' : 'This round'}: {trial.phase_zh || trial.phase || trial.state}</strong>
          <p className="wf-action-soft">
            {trial.workflow_version ? `v${trial.workflow_version}` : ''}
            {trial.freeze?.package_fingerprint ? (zh ? ` · 已发布冻结版 ${trial.freeze.package_version || ''}` : ` · published frozen version ${trial.freeze.package_version || ''}`) : (zh ? ' · 草稿试跑' : ' · draft trial')}
            {trial.counts_as_evidence && trial.freeze?.package_fingerprint ? (zh ? ' · 可作为该发布版的流程证据' : ' · flow evidence for this publication') : ''}
            {trial.counts_as_evidence && !trial.freeze?.package_fingerprint ? (zh ? ' · 草稿步骤已通过，尚非发布版验收' : ' · draft steps passed; not publication acceptance') : ''}
            {typeof trial.model_calls_total === 'number' ? (zh ? ` · 已用 ${trial.model_calls_total} 次` : ` · ${trial.model_calls_total} model calls used`) : ''}
            {trial.resume_from ? (zh ? ` · 从 ${trial.resume_from} 继续` : ` · resume at ${trial.resume_from}`) : ''}
            {trial.stopped_at ? (zh ? ` · 停在 ${trial.stopped_at}` : ` · stopped at ${trial.stopped_at}`) : ''}
          </p>
        </div>
      )}
    </>
  )
}

export function FlowTestDialog({ zh, flowName, savedVersion, draftOpen, connected, test, preflight, preflightBusy, latestTest, onPreflight, onOpenStep, onRecordTest, onFlowTrial, flowTrialBusy = false, latestFlowTrial = null, publishedCandidates = [], publishedCandidatesError, employeeTrial, onClose }: {
  zh: boolean
  flowName: string
  savedVersion: string
  draftOpen: boolean
  connected: boolean
  test: FlowTestView
  preflight: Preflight | null
  preflightBusy: boolean
  latestTest: TestRecord
  onPreflight?: () => void
  onOpenStep?: (nodeId: string) => void
  onRecordTest?: () => void
  /** Whole-flow trial on a saved draft or an exact published pin. Resume addresses the original round. */
  onFlowTrial?: (action: 'start' | 'continue' | 'new_round' | 'stop', limits: { perNode: number; total: number }, target: FlowTrialTarget) => void
  flowTrialBusy?: boolean
  latestFlowTrial?: FlowTrialSummary
  publishedCandidates?: readonly PublishedFlowTrialCandidate[]
  publishedCandidatesError?: string | null
  employeeTrial?: ReactNode
  onClose: () => void
}) {
  const whole = preflight && !preflight.focus_node_id ? preflight : null
  return (
    <DialogFrame testId="flow-test-dialog" zh={zh} onClose={onClose}
      title={zh ? `测试 · ${flowName}` : `Test · ${flowName}`}
      sub={zh ? `预检与逐步试跑测已保存的 v${savedVersion}；整流程可选择草稿或已发布冻结版。测试不改草稿、不发布。` : `Preflight and step trials use saved v${savedVersion}; a whole-flow trial can select a draft or a frozen publication. Testing never changes a draft or publishes.`}>
      {draftOpen && (
        <p className="wf-action-warn" role="status" data-testid="flow-test-unsaved">{zh
          ? '画布上有未保存的改动：预检与逐步试跑不含这些改动。要测它们，先保存草稿；已发布冻结版不受影响。'
          : 'Preflight and step trials exclude unsaved canvas changes. Save the draft to test them; a frozen publication is unaffected.'}</p>
      )}
      <section className="wf-action-section" data-testid="flow-test-preflight">
        <h4>{zh ? '1 · 整条预检（模拟，不花钱）' : '1 · Whole-flow preflight (simulated, free)'}</h4>
        <p>{zh ? '按已保存的合同把每一步走一遍：谁做、要什么、交什么、会碰到哪些工具。不调用模型，也不执行任何动作。' : 'Walks every step of the saved contract: who, what it needs, what it delivers, which tools it touches. No model is called and nothing is done.'}</p>
        {onPreflight && (
          <button type="button" className="workflows-cancel" disabled={!connected || preflightBusy} onClick={onPreflight}>
            {preflightBusy ? (zh ? '预检中…' : 'Checking…') : (zh ? '预检整条流程' : 'Preflight the whole flow')}
          </button>
        )}
        {whole && (() => {
          const missing = (whole.missing_fields ?? []).filter(Boolean)
          // The intake fields the first step reads are a separate thing from the flow's own contract: say so once,
          // and where to give them (the order form -- filling it places nothing), instead of listing them as a fault.
          const rest = missing.length ? whole.blockers.filter(item => !missing.some(field => item.includes(field))) : whole.blockers
          return (
            <div className={`wf-action-result${whole.ready ? ' is-ready' : ' is-blocked'}`} role="status" data-testid="flow-test-preflight-result">
              <strong>{whole.ready ? (zh ? '合同走得通（仅模拟）' : 'The contract holds (simulated only)') : (zh ? '有步骤走不通' : 'Some steps do not hold')}</strong>
              {missing.length > 0 && (
                <p className="wf-action-soft" data-testid="flow-test-missing-fields">{zh
                  ? `预检要用一组开单字段的值走第一步，还缺：${missing.join('、')}。在「工作流包」视图的「发起订单」表单里填好再预检——填写不会下单。`
                  : `The preflight walks the first step with a set of intake values; missing: ${missing.join(', ')}. Fill them in the Package view's order form, then preflight again -- filling it places no order.`}</p>
              )}
              <ShortList items={rest} zh={zh} />
              <ShortList items={whole.warnings ?? []} soft zh={zh} />
            </div>
          )
        })()}
      </section>
      {!employeeTrial && <section className="wf-action-section" data-testid="flow-test-steps">
        <h4>{zh ? `2 · 逐步真实试跑（${test.passed}/${test.ai.length} 个 AI 步骤已通过）` : `2 · Real step trials (${test.passed}/${test.ai.length} AI steps passed)`}</h4>
        <p>{zh ? '每个 AI 步骤在隔离工作区里真实跑一次，花费有上限、跑前先确认；试跑在这一步的「交付」页里发起。按顺序试：后一步可以用前一步通过的产物（在那一步的「交付」页里选），人工步骤在试跑里按已放行处理。' : 'Each AI step runs for real in an isolated workspace, with a capped spend confirmed first; a trial starts on the step’s Deliver page. Go in order: a step can take what the step before it produced in a passing trial (chosen on its Deliver page), and people’s steps are taken as released.'}</p>
        {test.ai.length === 0 ? <em>{zh ? '这条流程没有 AI 步骤。' : 'This flow has no AI step.'}</em> : (
          <ul className="wf-action-steps">
            {test.ai.map(step => (
              <li key={step.id} data-step={step.id} data-state={step.state} data-next={step.id === test.next || undefined}>
                <span>{step.name}{step.id === test.next && <em className="wf-action-next">{zh ? ' · 建议下一步试这里' : ' · try this next'}</em>}</span>
                <small>{TEST_STEP_LABELS[step.state][zh ? 0 : 1]}</small>
                {onOpenStep && <button type="button" className="workflows-cancel" onClick={() => onOpenStep(step.id)}>{zh ? '去试跑 →' : 'Trial →'}</button>}
              </li>
            ))}
          </ul>
        )}
        {(test.people.length > 0 || test.calls.length > 0) && (
          <p className="wf-action-soft">
            {test.people.length > 0 && (zh ? `人做 / 人审的步骤（${test.people.map(step => step.name).join('、')}）不在试跑里跑：真实工单会把它们交给负责人。` : `Steps a person does or signs (${test.people.map(step => step.name).join(', ')}) are not run by a trial: a real order hands them to their owner.`)}
            {test.calls.length > 0 && (zh ? ` 调用其他流程的步骤（${test.calls.map(step => step.name).join('、')}）要去测被调用的那条流程。` : ` Steps that call another flow (${test.calls.map(step => step.name).join(', ')}) are tested by testing that flow.`)}
          </p>
        )}
      </section>}
      {employeeTrial ?? <section className="wf-action-section" data-testid="flow-test-flow-trial">
        <h4>{zh ? '2b · 整流程试跑' : '2b · Whole-flow trial'}</h4>
        <FlowTrialControls zh={zh} connected={connected} draftOpen={draftOpen} busy={flowTrialBusy} steps={test.ai.length} savedVersion={savedVersion} trial={latestFlowTrial} onFlowTrial={onFlowTrial} publishedCandidates={publishedCandidates} publishedCandidatesError={publishedCandidatesError} />
      </section>}
      <section className="wf-action-section" data-testid="flow-test-record">
        <h4>{zh ? '3 · 测试记录' : '3 · Test record'}</h4>
        <p>{zh ? '最近一次：' : 'Latest: '}<strong>{recordLine(latestTest, zh)}</strong></p>
        <p className="wf-action-soft">{zh ? '提交测试需要一条非模拟的「通过」记录。试跑都过了之后，在这里记下这次结果。' : 'Submitting for testing needs a passing record that is not a simulation. Once the trials pass, record the result.'}</p>
        {onRecordTest && <button type="button" className="workflows-cancel" onClick={onRecordTest}>{zh ? '记录试跑结果 →' : 'Record a test result →'}</button>}
      </section>
    </DialogFrame>
  )
}

const ACTION_TEXT = {
  submit_testing: ['提交测试', 'Submit for testing'],
  request_release: ['申请发布审批', 'Request release approval'],
  publish: ['发布', 'Publish'],
} as const

export function FlowAdmissionDialog({ zh, flowName, version, connected, view, busy, versions, projectNameOf, onSubmitTesting, onRequestRelease, onDecideRelease, onPublish, onOpenPackage, onClose }: {
  zh: boolean
  flowName: string
  version: string
  connected: boolean
  view: AdmissionView
  busy: boolean
  /** 版本与采用: the record, 'loading' until it arrives, null when it could not be read; absent = not shown (not the owner). */
  versions?: FlowVersions | null | 'loading'
  projectNameOf?: (projectId: string) => string
  onSubmitTesting?: () => void
  onRequestRelease?: () => void
  onDecideRelease?: (decision: 'approved' | 'rejected') => void
  onPublish?: () => void
  onOpenPackage?: () => void
  onClose: () => void
}) {
  const act = view.next === 'submit_testing' ? onSubmitTesting : view.next === 'request_release' ? onRequestRelease : view.next === 'publish' ? onPublish : undefined
  return (
    <DialogFrame testId="flow-admission-dialog" zh={zh} onClose={onClose}
      title={zh ? `验收准入 · ${flowName}` : `Admission · ${flowName}`}
      sub={zh ? `v${version} · 保存、测试、准入是三件事：准入之后，项目才能采用它接单。` : `v${version} · Saving, testing and admission are three things: only after admission can a project adopt it.`}>
      <ol className="wf-admission-track" data-stage={view.stage}>
        {view.track.map(item => (
          <li key={item.stage} data-state={item.state}>{ADMISSION_STAGE_LABELS[item.stage][zh ? 0 : 1]}</li>
        ))}
      </ol>
      {view.stage === 'retired' && <p>{zh ? '这条流程已停用，不再接单。' : 'This flow is retired and takes no orders.'}</p>}
      {view.stage === 'published' && (
        <p>{zh ? `已发布 v${version}。项目要用它，到项目中心的「业务流」里采用这个版本；回滚与灰度在「工作流包」视图。` : `v${version} is published. A project adopts this version under 项目中心 → 业务流; rollback and rollout are in the Package view.`}</p>
      )}
      {view.blockers.length > 0 && (
        <div className="wf-action-blockers" data-testid="flow-admission-blockers">
          <ShortList items={view.blockers.map(item => item[zh ? 0 : 1])} zh={zh} />
        </div>
      )}
      <div className="wf-action-buttons">
        {view.next === 'decide_release' && onDecideRelease && (
          <>
            <span className="wf-action-soft">{zh ? '发布审批已提交，等你决定：' : 'Release approval requested; your decision:'}</span>
            <button type="button" className="workflows-invoke" data-testid="flow-admission-approve" disabled={!connected || busy || !view.ready} onClick={() => onDecideRelease('approved')}>{zh ? '批准发布' : 'Approve release'}</button>
            <button type="button" className="workflows-cancel" data-testid="flow-admission-reject" disabled={!connected || busy || !view.ready} onClick={() => onDecideRelease('rejected')}>{zh ? '拒绝发布' : 'Reject release'}</button>
          </>
        )}
        {view.next !== 'decide_release' && view.next !== 'none' && act && (
          <button type="button" className="workflows-invoke" data-testid="flow-admission-next" disabled={!connected || busy || !view.ready} onClick={act}>
            {busy ? (zh ? '处理中…' : 'Working…') : ACTION_TEXT[view.next][zh ? 0 : 1]}
          </button>
        )}
        {onOpenPackage && <button type="button" className="workflows-cancel" onClick={onOpenPackage}>{zh ? '发布证据与回滚 →' : 'Evidence and rollback →'}</button>}
      </div>
      {versions !== undefined && <VersionsSection zh={zh} versions={versions} projectNameOf={projectNameOf} />}
    </DialogFrame>
  )
}

/** 流程版本面板 (lib/flowVersions.ts): what this flow has published and which project runs which. Read-only. */
function VersionsSection({ zh, versions, projectNameOf }: { zh: boolean; versions: FlowVersions | null | 'loading'; projectNameOf?: (projectId: string) => string }) {
  const view = versions && versions !== 'loading' ? versionsView(versions, zh, projectNameOf) : null
  return (
    <section className="wf-action-section wf-admission-versions" data-testid="flow-admission-versions">
      <h4>{zh ? '版本与采用' : 'Versions and adoption'}</h4>
      {versions === 'loading' && <p className="wf-action-soft">{zh ? '正在读取版本记录…' : 'Reading the version record…'}</p>}
      {versions === null && <p className="wf-action-soft">{zh ? '版本记录读不出来。' : 'The version record could not be read.'}</p>}
      {view && (
        <>
          <p data-testid="flow-versions-current">{view.current}</p>
          {view.currentDiff && (
            <p data-testid="flow-versions-current-diff" className={view.currentDiff.warn ? 'wf-action-warn' : 'wf-action-soft'}>{view.currentDiff.text}</p>
          )}
          {view.skillsNote && <p className="wf-action-soft" data-testid="flow-versions-skills">{view.skillsNote}</p>}
          <h5>{zh ? '发布过的版本' : 'Published'}</h5>
          {view.published.length
            ? <ul data-testid="flow-versions-published">{view.published.map((line, index) => (
              <li key={line}>{line}{view.publishedDiffs[index] && <small className="wf-version-diff">{view.publishedDiffs[index]}</small>}</li>
            ))}</ul>
            : <p className="wf-action-soft">{zh ? '还没有发布过的版本。' : 'No version published yet.'}</p>}
          <h5>{zh ? '采用它的项目' : 'Projects using it'}</h5>
          {view.adoptions.length
            ? <ul data-testid="flow-versions-adoptions">{view.adoptions.map(item => (
              <li key={item.text} data-behind={item.behind ? '' : undefined}>{item.text}{item.upgrade && <small className="wf-version-diff">{item.upgrade}</small>}</li>
            ))}</ul>
            : <p className="wf-action-soft">{zh ? '还没有项目采用这条流程。' : 'No project has adopted this flow.'}</p>}
          {view.behindNote && <p className="wf-action-soft" data-testid="flow-versions-behind">{view.behindNote}</p>}
        </>
      )}
    </section>
  )
}
