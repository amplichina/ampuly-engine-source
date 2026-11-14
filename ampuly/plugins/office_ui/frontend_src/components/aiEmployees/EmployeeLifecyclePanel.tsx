import { useEffect, useState, useSyncExternalStore } from 'react'
import { EmployeeLifecycleHistory } from '../EmployeeLifecycleHistory'
import { historyBlocksNewTest, lifecycleStateWords } from '../../lib/aiEmployeeLifecycleHistory'
import { EmployeeLifecycleError, LIFECYCLE_ACTIONS, lifecycleCandidateKey } from '../../lib/aiEmployeeLifecycleController'
import type { EmployeeLifecycleController, LifecycleCandidate, LifecycleEntry, LifecycleJudgment, LifecycleTrial } from '../../lib/aiEmployeeLifecycleController'

export interface EmployeeLifecyclePanelProps {
  controller: EmployeeLifecycleController; project_id: string | null
  /** Only the parent's current ready draft-detail receipt, never a local editor manifest. */
  candidate: LifecycleCandidate | null; editable: boolean; mode?: 'package' | 'instance'
}
const PACKAGE_STEPS = [
  { id: 'save', label: '保存' },
  { id: 'prepare', label: '准备测试' },
  { id: 'budget', label: '限额' },
  { id: 'review', label: '审核' },
  { id: 'publish', label: '发布' },
] as const
type LifecycleStepId = typeof PACKAGE_STEPS[number]['id'] | 'done'

const actionWords: Record<string, string> = {
  [LIFECYCLE_ACTIONS.prepare]: '准备测试', [LIFECYCLE_ACTIONS.start]: '受控测试', [LIFECYCLE_ACTIONS.detail]: '读取测试记录',
  [LIFECYCLE_ACTIONS.lookup]: '只读核对原测试', [LIFECYCLE_ACTIONS.review]: '人工审核', [LIFECYCLE_ACTIONS.publish]: '发布岗位',
  [LIFECYCLE_ACTIONS.publishLookup]: '只读核对原发布',
  [LIFECYCLE_ACTIONS.history]: '读取原测试与发布历史',
  [LIFECYCLE_ACTIONS.abandon]: '作废原测试',
}
/** Existing asset-editor visual language; no independent socket or App navigation. */
export function EmployeeLifecyclePanel({ controller, project_id, candidate, editable, mode = 'package' }: EmployeeLifecyclePanelProps) {
  const instanceMode = mode === 'instance' || !!candidate?.instance_selection
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const [message, setMessage] = useState('')
  const [selectedHistoryKey, setSelectedHistoryKey] = useState<string | null>(null)
  const key = candidate ? lifecycleCandidateKey(candidate) : null
  const history = snapshot.histories.find(h => h.project_id === project_id && h.package_id === candidate?.package_id)
  const entry = snapshot.entries.find(item => item.key === selectedHistoryKey && item.candidate.project_id === project_id && item.candidate.package_id === candidate?.package_id)
    ?? snapshot.entries.find(item => item.key === key)
  const currentEntry = !entry || entry.key === key
  const busy = controller.hasBlockingWrite()
  const current = snapshot.connected && !!project_id && candidate?.project_id === project_id
  const pendingRead = !!entry && snapshot.receipts.some(r => r.entry_key === entry.key && r.state === 'pending')
  const canAct = !!current && currentEntry && editable && !busy && !pendingRead && !!entry && !entry.stale
  const run = (operation: () => unknown): void => {
    try { operation(); setMessage('') }
    catch (error) { setMessage(error instanceof EmployeeLifecycleError ? error.message : '当前结果不能核实；未自动重试。') }
  }
  const unresolved = snapshot.receipts.filter(r => r.state === 'unknown' || r.state === 'pending')
  useEffect(() => {
    setSelectedHistoryKey(null)
    if (snapshot.connected && candidate?.project_id === project_id) {
      try { controller.readHistory(candidate) } catch { /* Existing pending read is retained; no retry loop. */ }
    }
  }, [controller, project_id, candidate?.package_id, key, snapshot.connected])
  const latest = entry && snapshot.receipts.find(r => r.request_id === entry.latest_request_id)
  const steps = instanceMode ? PACKAGE_STEPS.filter(step => step.id !== 'publish') : PACKAGE_STEPS
  const stepId: LifecycleStepId = !candidate ? 'save'
    : entry?.published ? 'done'
    : !editable && !entry?.trial ? 'save'
    : entry?.trial
      ? (entry.trial.state === 'awaiting_review' ? 'review'
        : entry.trial.state === 'approved' ? (instanceMode ? 'done' : 'publish')
        : ['failed', 'rejected', 'cancelled', 'abandoned'].includes(entry.trial.state) ? 'prepare'
        : 'budget')
      : entry?.prepared ? 'budget'
      : 'prepare'
  const currentIndex = stepId === 'done' ? steps.length : steps.findIndex(step => step.id === stepId)
  const prepareDisabled = !current || !editable || busy || pendingRead || !history || historyBlocksNewTest(history)
  const prepareLabel = entry?.trial?.state === 'abandoned' ? '准备新的测试（不计费）' : entry?.prepared ? '重新核对测试条件' : '准备测试（不计费）'
  const prepare = () => { if (candidate) run(() => controller.prepare(candidate)) }
  return <section className="ai-employee-editor__section ai-employee-lifecycle" aria-label="岗位测试与发布" data-testid="employee-lifecycle-panel">
    <h3>{instanceMode ? '验证这名员工' : '上线步骤'}</h3>
    <ol className="ai-employee-lifecycle-steps" data-testid="employee-lifecycle-steps" aria-label="上线步骤">
      {steps.map((step, index) => {
        const state = index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'upcoming'
        return <li key={step.id} data-step={step.id} data-state={state} aria-current={state === 'current' ? 'step' : undefined}><span>{step.label}</span></li>
      })}
    </ol>
    {message && <p role="alert">{message}</p>}
    {snapshot.journal_error && <p role="alert">{snapshot.journal_error}</p>}
    {!snapshot.connected && <p role="status">连接已断开。保留原请求；重连后不会自动重发。</p>}
    {unresolved.map(receipt => <section className="ai-employee-editor__status" key={receipt.request_id} role="status">
      <strong>{actionWords[receipt.action]} · {receipt.state === 'pending' ? '已发出，等回执' : '结果未知'}</strong>
      <p>{receipt.message}</p><p>项目 {receipt.project_id} · 岗位 {receipt.package_id} · 原请求 {receipt.request_id}</p>
      {receipt.state === 'unknown' && receipt.action === LIFECYCLE_ACTIONS.start && <button type="button"
        disabled={!snapshot.connected || receipt.project_id !== project_id || snapshot.receipts.some(r => r.entry_key === receipt.entry_key && r.state === 'pending')}
        onClick={() => run(() => controller.lookupOriginalStart(receipt.request_id))}>只读核对原测试</button>}
      {receipt.state === 'unknown' && receipt.action === LIFECYCLE_ACTIONS.publish && <button type="button"
        disabled={!snapshot.connected || receipt.project_id !== project_id || snapshot.receipts.some(r => r.entry_key === receipt.entry_key && r.state === 'pending')}
        onClick={() => run(() => controller.lookupOriginalPublish(receipt.request_id))}>只读核对原发布</button>}
      {receipt.state === 'unknown' && receipt.action === LIFECYCLE_ACTIONS.review && <>
        <p>只核对原测试记录中的审核请求编号与决定；不会再次提交审核。</p>
        <button type="button" disabled={!snapshot.connected || receipt.project_id !== project_id || snapshot.receipts.some(r => r.entry_key === receipt.entry_key && r.state === 'pending')}
          onClick={() => run(() => controller.lookupOriginalReview(receipt.request_id))}>只读核对原审核</button>
      </>}
      {receipt.state === 'unknown' && receipt.action === LIFECYCLE_ACTIONS.abandon && <>
        <p>作废是否完成还不能确定；只核对原请求，不再次提交，也不重新测试。</p>
        <button type="button" disabled={!snapshot.connected || receipt.project_id !== project_id || snapshot.receipts.some(r => r.entry_key === receipt.entry_key && r.state === 'pending')}
          onClick={() => run(() => controller.lookupOriginalAbandon(receipt.request_id))}>只读核对原作废</button>
      </>}
    </section>)}
    {latest && latest.state !== 'pending' && latest.state !== 'unknown' && <p role="status">{latest.message}</p>}
    <div className="ai-employee-lifecycle-current" data-testid="employee-lifecycle-current" data-current-step={stepId}>
      {stepId === 'save' && <p>{!candidate
        ? (instanceMode ? '先保存并重新读取实例，再读取它采用的固定岗位版本。' : '先打开一个已保存的岗位草稿。这里使用服务端草稿，不使用未保存编辑。')
        : (instanceMode ? '先保存当前编辑并重新核对实例；已有测试记录仍可查看。' : '先保存当前编辑并重新核对草稿；已有测试记录仍可查看。')}</p>}
      {candidate && !editable && stepId !== 'save' && <p>{instanceMode ? '先保存当前编辑并重新核对实例；已有测试记录仍可查看。' : '先保存当前编辑并重新核对草稿；已有测试记录仍可查看。'}</p>}
      {candidate && !currentEntry && <p role="status">{instanceMode ? '正在查看原实例修订的记录，不会将它当作当前配置的测试凭证。' : '正在查看旧版本的原记录，不会将它当作当前草稿的测试凭证。'}
        <button type="button" onClick={() => setSelectedHistoryKey(null)}>{instanceMode ? '返回当前实例配置' : '返回当前草稿'}</button></p>}
      {stepId === 'prepare' && candidate && currentEntry && <button type="button" className="ai-employee-editor__primary" disabled={prepareDisabled} onClick={prepare}>{prepareLabel}</button>}
      {stepId === 'budget' && entry?.prepared && !entry.trial && <TestBudgetForm key={entry.key + '/' + entry.latest_request_id} entry={entry}
        disabled={!canAct || !entry.prepared.allowed_actions.includes(LIFECYCLE_ACTIONS.start)} run={run} controller={controller} />}
      {entry?.trial && (stepId !== 'save' && stepId !== 'prepare' || entry.trial.state === 'abandoned') && <>
        <h4>{lifecycleStateWords[entry.trial.state]}</h4>
        <p>测试编号 {entry.trial.trial_id} · {entry.trial.context.model_config.provider} / {entry.trial.context.model_config.model} · 已有 {entry.trial.observations.length} / {entry.trial.sample_count} 条观察</p>
        <p className="ai-employee-editor__muted">任务范围：{instanceMode ? '岗位包任务集，加上本实例已保存的参数与知识。' : '岗位包任务集。'}只展示真实执行结果。</p>
        {instanceMode && entry.trial.state === 'approved' && <p role="status">当前实例的这组配置已通过；不会发布共享岗位包，也不会自动启动业务任务。</p>}
        <button type="button" className={stepId === 'budget' ? 'ai-employee-editor__primary' : undefined} disabled={!current || pendingRead} onClick={() => run(() => controller.refreshTrial(entry.key))}>读取最新测试结果</button>
        {entry.stale && <p role="status">当前记录已过期或正在核对；审核、发布暂不可用。</p>}
        {entry.trial.state === 'outcome_unknown' && <AbandonTrialForm key={entry.trial.trial_id} entry={entry} controller={controller} run={run}
          disabled={!current || !controller.canAbandonTrial(entry.key)} />}
        {entry.trial.abandon && <section className="ai-employee-editor__section" aria-label="原测试作废记录">
          <p>这次测试已结束追查，不能审核或发布；这不代表测试通过，也不证明外部动作已撤销。原预算、费用与观察记录均保留。</p>
          <p>作废人 {entry.trial.abandon.actor} · {entry.trial.abandon.at}</p>
          <p>作废原因：{entry.trial.abandon.reason}</p>
          <p>原作废请求 {entry.trial.abandon.request_id} · 原预算编号 {entry.trial.abandon.budget_id ?? '尚无已绑定预算编号（费用未被认定为 0）'}</p>
          <details><summary>查看保留的观察记录</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(entry.trial.observations, null, 2)}</pre></details>
          {currentEntry && history?.status !== 'ready' && <button type="button" disabled={!current || pendingRead || history?.status === 'pending'}
            onClick={() => candidate && run(() => controller.readHistory(candidate))}>刷新历史，核对能否准备新测试</button>}
          {history?.status === 'ready' && historyBlocksNewTest(history) && <p>还有未完成或结果未知的操作；请先核对这些原记录，再准备新测试。</p>}
        </section>}
        {stepId === 'review' && <TrialReview key={entry.trial.trial_id + '/' + entry.trial.revision} trial={entry.trial} entryKey={entry.key} controller={controller} run={run}
          disabled={!canAct || !entry.trial.allowed_actions.includes(LIFECYCLE_ACTIONS.review)} />}
        {stepId === 'publish' && <section className="ai-employee-editor__section">
          <h4>发布为可用岗位</h4>
          <p>只发布本次已测试、已人工审核的固定内容。不会替换现有实例。</p>
          <button type="button" className="ai-employee-editor__primary" disabled={!canAct || !entry.trial.test_passed || !entry.trial.allowed_actions.includes(LIFECYCLE_ACTIONS.publish)}
            onClick={() => run(() => controller.publishPackage(entry.key))}>发布已审核版本</button>
        </section>}
      </>}
      {entry?.published && <p role="status">服务端已确认发布。请读取“发布目录”查看此固定版本；这不等于已启用员工执行。</p>}
    </div>
    {candidate && <details className="ai-employee-lifecycle-more">
      <summary>记录与核对</summary>
      <p>当前岗位：{candidate.package_id} · 内容指纹 {candidate.fingerprint.slice(0, 12)}</p>
      {candidate.instance_selection && <p>项目实例 {candidate.instance_selection.instance_id} · 修订 {candidate.instance_selection.expected_revision}</p>}
      <EmployeeLifecycleHistory controller={controller} candidate={candidate} history={history} connected={!!current} run={run} onSelect={setSelectedHistoryKey} />
      {stepId === 'budget' && currentEntry && !entry?.trial && <button type="button" disabled={prepareDisabled} onClick={prepare}>重新核对测试条件</button>}
    </details>}
  </section>
}
function AbandonTrialForm({ entry, disabled, run, controller }: {
  entry: LifecycleEntry; disabled: boolean; run: (fn: () => unknown) => void; controller: EmployeeLifecycleController
}) {
  const [reason, setReason] = useState(''), [consent, setConsent] = useState(false)
  return <fieldset className="ai-employee-editor__section" disabled={disabled} data-testid="employee-lifecycle-abandon">
    <legend>不再追查这次未知测试</legend>
    <p>仅项目所有者可作废这次测试。作废后保留原预算、费用和观察，不会撤销已经发生的外部动作；本次不能再审核或发布。</p>
    <label className="ai-employee-editor__field">为什么作废<textarea aria-label="原测试作废原因" maxLength={4000} value={reason} onChange={e => setReason(e.target.value)} /></label>
    <label><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />我确认放弃本次测试结果，保留原记录与费用。</label>
    <button type="button" disabled={!reason.trim() || reason.includes('\0') || !consent}
      onClick={() => run(() => controller.abandonTrial(entry.key, reason))}>确认作废本次测试</button>
    <p className="ai-employee-editor__muted">收到作废回执并刷新历史后，才能另行准备新测试。准备本身不调用模型，新测试须重新设定限额并确认。</p>
  </fieldset>
}
function TestBudgetForm({ entry, disabled, run, controller }: {
  entry: LifecycleEntry; disabled: boolean; run: (fn: () => unknown) => void; controller: EmployeeLifecycleController
}) {
  const [cost, setCost] = useState(''), [calls, setCalls] = useState(''), [seconds, setSeconds] = useState('')
  const [reason, setReason] = useState(''), [consent, setConsent] = useState(false)
  const valid = /^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$/.test(cost) && Number(cost) > 0
    && /^\d+$/.test(calls) && Number(calls) >= 1 && Number(calls) <= 128
    && /^\d+$/.test(seconds) && Number(seconds) >= 1 && Number(seconds) <= 600 && !!reason.trim() && consent
  return <fieldset className="ai-employee-editor__section ai-employee-lifecycle-budget" data-testid="employee-lifecycle-budget" disabled={disabled}>
    <legend>给这次测试设好边界</legend>
    <p>固定模型：{entry.prepared?.context.model_config.provider} / {entry.prepared?.context.model_config.model} · {entry.prepared?.sample_count} 个任务样本</p>
    <label className="ai-employee-editor__field">金额上限（元）<input aria-label="本次测试金额上限" inputMode="decimal" maxLength={16} value={cost} onChange={e => setCost(e.target.value)} /></label>
    <label className="ai-employee-editor__field">模型调用上限（1—128 次）<input aria-label="本次测试模型调用上限" inputMode="numeric" maxLength={3} value={calls} onChange={e => setCalls(e.target.value)} /></label>
    <label className="ai-employee-editor__field">最长耗时（1—600 秒）<input aria-label="本次测试最长耗时" inputMode="numeric" maxLength={3} value={seconds} onChange={e => setSeconds(e.target.value)} /></label>
    <label className="ai-employee-editor__field">这次重点检查什么<textarea aria-label="本次测试原因" maxLength={4000} value={reason} onChange={e => setReason(e.target.value)} /></label>
    <p className="ai-employee-editor__muted">按实际用量计价。没有价格依据就停止，未知费用不会记为 0。</p>
    <label><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />我确认使用以上上限调用模型测试，可能产生费用。</label>
    <button type="button" className="ai-employee-editor__primary" disabled={!valid} onClick={() => run(() => controller.startTest(entry.key,
      { max_cost_cny: cost, max_model_calls: Number(calls), max_wall_clock_s: Number(seconds) }, reason))}>开始受控测试（会计费）</button>
  </fieldset>
}
function TrialReview({ trial, disabled, entryKey, controller, run }: {
  trial: LifecycleTrial; disabled: boolean; entryKey: string; controller: EmployeeLifecycleController; run: (fn: () => unknown) => void
}) {
  const [judgments, setJudgments] = useState<Record<string, { non_inferior_met?: boolean; absolute_threshold_met?: boolean }>>({})
  const [note, setNote] = useState('')
  const reviewed = trial.observations.length === trial.sample_count && trial.observations.every(o => {
    const j = judgments[o.case_id]; return j && typeof j.non_inferior_met === 'boolean' && typeof j.absolute_threshold_met === 'boolean'
  })
  const passing = reviewed && trial.observations.every(o => o.completed && o.hard_fail_count === 0 && judgments[o.case_id].non_inferior_met && judgments[o.case_id].absolute_threshold_met)
  const submit = (decision: 'approve' | 'reject'): void => run(() => controller.review(entryKey, decision, trial.observations.map(o =>
    ({ case_id: o.case_id, ...judgments[o.case_id] }) as LifecycleJudgment), note))
  return <div className="ai-employee-editor__section">
    <h4>逐条看输出，再决定是否通过</h4>
    {trial.cases.map((sample, index) => {
      const id = String(sample.id), observation = trial.observations[index]
      return <section className="ai-employee-editor__section" key={id} aria-label={`测试样本 ${id}`}>
        <h5>{id}</h5><details><summary>任务输入与判定要求</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(sample, null, 2)}</pre></details>
        {!observation ? <p>此条尚无执行结果。</p> : <>
          <p>{observation.completed ? '执行已完成' : '执行未完成'} · 硬性检查失败 {observation.hard_fail_count} 项</p>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }} aria-label={`样本 ${id} 的真实输出`}>{observation.output}</pre>
          {trial.state === 'awaiting_review' && <fieldset disabled={disabled}>
            <legend>你的质量判断（两项都需明确选择）</legend>
            {(['non_inferior_met', 'absolute_threshold_met'] as const).map(field => <label className="ai-employee-editor__field" key={field}>
              {field === 'non_inferior_met' ? '与任务集对照标准相比不退步？' : '达到任务集最低质量要求？'}
              <select aria-label={`${id} ${field === 'non_inferior_met' ? '不退步判断' : '最低质量判断'}`}
                value={judgments[id]?.[field] === undefined ? '' : judgments[id][field] ? 'yes' : 'no'}
                onChange={e => setJudgments(old => ({ ...old, [id]: { ...old[id], [field]: e.target.value === '' ? undefined : e.target.value === 'yes' } }))}>
                <option value="">请明确选择</option><option value="yes">是</option><option value="no">否</option>
              </select>
            </label>)}
          </fieldset>}
        </>}
      </section>
    })}
    {trial.state === 'awaiting_review' && <fieldset disabled={disabled}>
      <label className="ai-employee-editor__field">审核意见<textarea aria-label="岗位测试审核意见" maxLength={4000} value={note} onChange={e => setNote(e.target.value)} /></label>
      <button type="button" className="ai-employee-editor__primary" disabled={!passing} onClick={() => submit('approve')}>逐条审核通过</button>
      <button type="button" disabled={!reviewed} onClick={() => submit('reject')}>审核不通过</button>
      <p className="ai-employee-editor__muted">{trial.context.qualification_scope?.kind === 'instance' ? '通过不会自动勾选，也不发布岗位包。' : '通过不会自动勾选。审核后仍需另行发布。'}</p>
    </fieldset>}
  </div>
}

