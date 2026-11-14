import { employeeOperationWords } from './AIEmployeeProjectPanel'
import type { EmployeeOperationReceipt } from './AIEmployeeProjectPanel'

/** Controlled artifact identity, not a path, URL, body or grant to read the file. */
export interface EmployeeArtifactReference {
  artifact_id: string
  version: string
  content_hash: string
  label?: string
  controlled_reference?: string | null
}

export function employeeArtifactComplete(reference: EmployeeArtifactReference | null | undefined): reference is EmployeeArtifactReference {
  return !!reference?.artifact_id.trim() && !!reference.version.trim() && !!reference.content_hash.trim()
}

export function employeeArtifactEqual(left: EmployeeArtifactReference | null | undefined, right: EmployeeArtifactReference | null | undefined): boolean {
  return employeeArtifactComplete(left) && employeeArtifactComplete(right) && left.artifact_id === right.artifact_id
    && left.version === right.version && left.content_hash === right.content_hash
}

/** Whitelist the read reference; extra fields of an accidentally unprojected object never become an access grant. */
export function employeeArtifactReadReference(reference: EmployeeArtifactReference): EmployeeArtifactReference {
  return { artifact_id: reference.artifact_id, version: reference.version, content_hash: reference.content_hash,
    ...(reference.label ? { label: reference.label } : {}),
    ...(reference.controlled_reference ? { controlled_reference: reference.controlled_reference } : {}) }
}

export interface EmployeeRunCost {
  state: 'reported' | 'unknown' | 'partial'
  amount: number | null
  currency: string | null
  basis: string | null
  observed_at: string | null
}

export function employeeRunCostWords(cost: EmployeeRunCost | null | undefined): string {
  if (cost?.state !== 'reported' || typeof cost.amount !== 'number' || !Number.isFinite(cost.amount) || cost.amount < 0
    || !cost.currency?.trim() || !cost.basis?.trim() || !cost.observed_at?.trim()) return '实际费用待核实（无完整金额与计价依据）'
  return `${cost.amount} ${cost.currency} · 依据：${cost.basis} · 时间：${cost.observed_at}`
}

export type EmployeeFileStage = 'generation' | 'structure' | 'facts' | 'visual' | 'human_acceptance' | 'sent' | 'delivered'
export type EmployeeFileStageState = 'not_started' | 'pending' | 'passed' | 'failed' | 'unknown' | 'partial' | 'not_applicable'
export interface EmployeeFileStageEvidence {
  state: EmployeeFileStageState
  artifact: EmployeeArtifactReference | null
  source: string | null
  observed_at: string | null
  scope: string | null
  message?: string | null
}
export interface EmployeeFileEvidence {
  artifact: EmployeeArtifactReference
  stages: Partial<Record<EmployeeFileStage, EmployeeFileStageEvidence>>
}

export const employeeFileStageLabels: Record<EmployeeFileStage, string> = {
  generation: '生成', structure: '结构检查', facts: '事实检查', visual: '视觉检查', human_acceptance: '人工验收', sent: '发送', delivered: '送达',
}

/** A check or effect of another artifact/version/hash does not certify the displayed file. */
export function employeeFileStageState(file: EmployeeFileEvidence, stage: EmployeeFileStage): EmployeeFileStageState {
  const evidence = file.stages[stage]
  if (!evidence) return 'unknown'
  if (!employeeArtifactEqual(evidence.artifact, file.artifact)) return 'unknown'
  if ((evidence.state === 'passed' || evidence.state === 'not_applicable') && (!evidence.source?.trim() || !evidence.observed_at?.trim())) return 'unknown'
  return evidence.state
}

function stageWords(stage: EmployeeFileStage, state: EmployeeFileStageState): string {
  if (state === 'passed') {
    if (stage === 'generation') return '生成记录已确认'
    if (stage === 'human_acceptance') return '人工验收记录通过'
    if (stage === 'sent') return '发送记录已确认'
    if (stage === 'delivered') return '送达记录已确认'
    return '所列检查范围通过'
  }
  return { not_started: '尚未开始', pending: '处理中', failed: '失败', unknown: '待核实', partial: '部分完成', not_applicable: '不适用（见依据）' }[state]
}

export interface AIEmployeeRunView {
  project_id: string
  order_id: string
  run_id: string
  node_id: string
  attempt: number | null
  snapshot_revision: string | null
  current_snapshot_revision: string | null
  basis_state: 'frozen' | 'legacy_unpinned' | 'unknown'
  executor_name: string | null
  executor_kind: 'ai_employee' | 'human' | 'legacy' | 'unknown'
  frozen_employee: {
    package_id: string; package_version: string; package_fingerprint: string
    instance_id: string; instance_revision: string
    model_name: string | null; tool_names: readonly string[] | null
    reviewer_slot: string | null
  } | null
  inputs: readonly EmployeeArtifactReference[] | null
  outputs: readonly EmployeeArtifactReference[] | null
  waiting: { state: 'waiting' | 'none' | 'unknown'; person_name: string | null; slot_name: string | null; reason: string | null }
  budget: {
    step_limit: number | null; attempt_limit: number | null; assistant_limit: number | null; token_limit: number | null
    amount_limit: number | null; currency: string | null; source: string | null
  }
  usage: { steps: number | null; attempts: number | null; tokens: number | null; cumulative_cost: EmployeeRunCost | null }
  files: readonly EmployeeFileEvidence[] | null
  delivery_consequence: string | null
  allowed_actions: readonly ('inspect_run' | 'open_artifact')[]
}

export interface AIEmployeeRunPanelProps {
  project_id: string | null
  run: AIEmployeeRunView | null
  connected: boolean
  read_busy: boolean
  receipt: EmployeeOperationReceipt
  onInspect?: (request: { project_id: string; order_id: string; run_id: string; node_id: string; attempt: number; expected_snapshot_revision: string; read_only: true }) => void
  onOpenArtifact?: (request: { project_id: string; order_id: string; run_id: string; node_id: string; attempt: number; artifact: EmployeeArtifactReference; read_only: true }) => void
}

function runIdentityReason(props: AIEmployeeRunPanelProps): string {
  const run = props.run
  if (!props.project_id?.trim() || !run || run.project_id !== props.project_id) return '运行快照不属于当前项目。'
  if (!props.connected) return '连接已断开，当前显示仅作待核实记录。'
  if (props.read_busy) return '正在读取运行依据。'
  if (!run || !run.order_id.trim() || !run.run_id.trim() || !run.node_id.trim()
    || !Number.isInteger(run.attempt) || (run.attempt ?? 0) < 1) return '订单、运行、节点或 attempt 尚未明确。'
  if (!run.snapshot_revision?.trim() || !run.current_snapshot_revision?.trim()) return '运行快照 revision 尚未加载。'
  if (run.snapshot_revision !== run.current_snapshot_revision) return '运行记录已变化，先由父层刷新快照。'
  return ''
}

export function employeeRunActionReason(props: AIEmployeeRunPanelProps, action: 'inspect_run' | 'open_artifact', artifact?: EmployeeArtifactReference): string {
  const reason = runIdentityReason(props)
  if (reason) return reason
  const run = props.run!
  if (!run.allowed_actions.includes(action)) return '服务端未允许此读取操作。'
  if (action === 'inspect_run') return props.onInspect ? '' : '运行依据读取入口尚未接线。'
  if (!props.onOpenArtifact) return '受控文件读取入口尚未接线。'
  if (!employeeArtifactComplete(artifact) || !artifact.controlled_reference?.trim()) return '文件版本、hash 或受控引用尚未完整。'
  const known = [...(run.inputs ?? []), ...(run.outputs ?? [])]
  if (!known.some(reference => employeeArtifactEqual(reference, artifact) && reference.controlled_reference === artifact.controlled_reference)) {
    return '文件不属于本次运行已登记的输入或输出。'
  }
  return ''
}

export function employeeRunBasisComplete(run: AIEmployeeRunView): boolean {
  const pin = run.frozen_employee
  return run.basis_state === 'frozen' && !!pin?.package_id.trim() && !!pin.package_version.trim()
    && !!pin.package_fingerprint.trim() && !!pin.instance_id.trim() && !!pin.instance_revision.trim()
    && !!run.order_id.trim() && !!run.run_id.trim() && !!run.node_id.trim()
    && Number.isInteger(run.attempt) && (run.attempt ?? 0) >= 1
}

function count(value: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= 0 ? String(value) : '待核实'
}

export function employeeRunAmountLimitWords(run: AIEmployeeRunView): string {
  const budget = run.budget
  return typeof budget.amount_limit === 'number' && Number.isFinite(budget.amount_limit) && budget.amount_limit >= 0
    && budget.currency?.trim() && budget.source?.trim()
    ? `${budget.amount_limit} ${budget.currency} · 依据：${budget.source}` : '金额上限待核实'
}

/** Historical pins are displayed as recorded; no current-instance fallback, fetch, effect or expense reset. */
export function AIEmployeeRunPanel(props: AIEmployeeRunPanelProps) {
  const run = props.run
  if (!run) return <section className="employee-run-panel employee-operations" data-testid="ai-employee-run-panel">
    <h2>实际运行依据</h2><p>真实运行记录尚未加载。</p></section>
  if (!props.project_id?.trim() || run.project_id !== props.project_id) return <section className="employee-run-panel employee-operations" data-testid="ai-employee-run-panel">
    <h2>实际运行依据</h2><p role="status">运行快照不属于当前项目；等待当前项目的运行记录。</p></section>
  const inspectReason = employeeRunActionReason(props, 'inspect_run')
  const pin = run.basis_state === 'frozen' ? run.frozen_employee : null
  const renderArtifact = (artifact: EmployeeArtifactReference, key: string) => {
    const reason = employeeRunActionReason(props, 'open_artifact', artifact)
    return <li key={key} className="employee-run-panel__artifact">
      <strong>{artifact.label || artifact.artifact_id || '未命名文件'}</strong>
      <span>artifact {artifact.artifact_id || '待登记'} · 版本 {artifact.version || '未知'} · hash {artifact.content_hash || '未知'}</span>
      <button type="button" data-action="open_artifact" data-artifact-id={artifact.artifact_id} disabled={!!reason} title={reason || undefined}
        onClick={() => {
          if (!employeeRunActionReason(props, 'open_artifact', artifact) && run.attempt !== null) props.onOpenArtifact?.({
            project_id: run.project_id, order_id: run.order_id, run_id: run.run_id, node_id: run.node_id, attempt: run.attempt,
            artifact: employeeArtifactReadReference(artifact), read_only: true,
          })
        }}>查看此版本受控文件</button>
      {reason && <p className="employee-operations__muted">{reason}</p>}
    </li>
  }
  return <section className="employee-run-panel employee-operations" data-testid="ai-employee-run-panel" data-basis-state={run.basis_state}>
    <header><h2>实际运行依据</h2><p>以本次 attempt 的保存记录核对员工、材料与累计使用量。</p></header>
    {!props.connected && <p role="status">连接已断开；运行状态待重新核实。</p>}
    <dl className="employee-operations__facts">
      <div><dt>订单</dt><dd>{run.order_id || '待加载'}</dd></div><div><dt>运行</dt><dd>{run.run_id || '待加载'}</dd></div>
      <div><dt>节点</dt><dd>{run.node_id || '待加载'}</dd></div><div><dt>attempt</dt><dd>{Number.isInteger(run.attempt) && (run.attempt ?? 0) >= 1 ? run.attempt : '待核实'}</dd></div>
      <div><dt>实际执行者</dt><dd>{run.executor_name || '尚未提供'}</dd></div>
      <div><dt>执行者类型</dt><dd>{run.executor_kind === 'ai_employee' ? 'AI 员工' : run.executor_kind === 'human' ? '人工' : run.executor_kind === 'legacy' ? '历史执行者' : '待核实'}</dd></div>
      <div><dt>运行快照 revision</dt><dd>{run.snapshot_revision || '待加载'} · 当前 {run.current_snapshot_revision || '待加载'}</dd></div>
    </dl>
    {run.basis_state === 'legacy_unpinned' ? <p className="employee-operations__notice" role="status">legacy_unpinned：这份历史记录未冻结员工运行依据，不能用当前最新实例补作旧运行版本。</p>
      : run.basis_state === 'unknown' ? <p className="employee-operations__notice">冻结状态待核实。</p>
      : <div className="employee-operations__card"><h3>本次冻结的员工依据</h3>
        {!employeeRunBasisComplete(run) && <p className="employee-operations__notice">冻结引用不完整，需核对保存记录。</p>}
        <dl className="employee-operations__facts">
          <div><dt>标准包及版本</dt><dd>{pin?.package_id || '未知'} · {pin?.package_version || '未知'}</dd></div>
          <div><dt>包指纹</dt><dd>{pin?.package_fingerprint || '未知'}</dd></div>
          <div><dt>实际实例</dt><dd>{pin?.instance_id || '未知'}</dd></div>
          <div><dt>实例 revision</dt><dd>{pin?.instance_revision || '未知'}</dd></div>
          <div><dt>模型</dt><dd>{pin?.model_name || '尚未记录'}</dd></div>
          <div><dt>工具</dt><dd>{pin?.tool_names?.join('、') || '尚未记录'}</dd></div>
          <div><dt>冻结复核槽位</dt><dd>{pin?.reviewer_slot || '尚未记录'}</dd></div>
        </dl>
      </div>}
    <div className="employee-operations__card"><h3>当前等谁</h3>
      <p>{run.waiting.state === 'waiting' ? `${run.waiting.person_name || '人员待核实'} · ${run.waiting.slot_name || '槽位待核实'}`
        : run.waiting.state === 'none' ? '服务端未记录等待人' : '等待状态待核实'}</p>
      <p>{run.waiting.reason || '等待原因尚未提供'}</p>
    </div>
    <div className="employee-run-panel__materials">{([['inputs', '固定输入'], ['outputs', '登记输出']] as const).map(([key, label]) => {
      const references = run[key]
      return <section key={key}><h3>{label}</h3>{references === null ? <p>材料引用尚未加载。</p>
        : references.length === 0 ? <p>本次记录未登记此类材料。</p>
          : <ul>{references.map((artifact, index) => renderArtifact(artifact, `${key}:${index}`))}</ul>}</section>
    })}</div>
    <section className="employee-operations__card"><h3>数量、预算与累计费用</h3>
      <dl className="employee-operations__facts">
        <div><dt>已用步数 / 步数上限</dt><dd>{count(run.usage.steps)} / {count(run.budget.step_limit)} 步</dd></div>
        <div><dt>累计 attempt / 次数上限</dt><dd>{count(run.usage.attempts)} / {count(run.budget.attempt_limit)} 次</dd></div>
        <div><dt>子助手数量上限</dt><dd>{count(run.budget.assistant_limit)} 个</dd></div>
        <div><dt>已用 token / token 上限</dt><dd>{count(run.usage.tokens)} / {count(run.budget.token_limit)}</dd></div>
        <div><dt>金额上限</dt><dd>{employeeRunAmountLimitWords(run)}</dd></div>
        <div><dt>实际累计费用</dt><dd>{employeeRunCostWords(run.usage.cumulative_cost)}</dd></div>
      </dl>
      <p className="employee-operations__muted">数量上限与金额上限各自独立；累计费用沿用运行账目，返修确认不会在页面清零。</p>
    </section>
    <section className="employee-run-panel__files"><h3>文件证据状态</h3>
      <p className="employee-operations__muted">生成、结构与事实检查、视觉检查、人工验收、发送和送达各以自己的材料版本证据为准。</p>
      {run.files === null ? <p>文件证据尚未加载。</p> : run.files.length === 0 ? <p>本次运行尚无文件证据记录。</p>
        : run.files.map((file, index) => <article key={index} className="employee-operations__card" data-file-id={file.artifact.artifact_id}>
          <h4>{file.artifact.label || file.artifact.artifact_id}</h4><p>版本 {file.artifact.version || '未知'} · hash {file.artifact.content_hash || '未知'}</p>
          <ol className="employee-run-panel__evidence-stages">{(Object.keys(employeeFileStageLabels) as EmployeeFileStage[]).map(stage => {
            const registered = [...(run.inputs ?? []), ...(run.outputs ?? [])].some(reference => employeeArtifactEqual(reference, file.artifact))
            const state = registered ? employeeFileStageState(file, stage) : 'unknown'
            const record = file.stages[stage]
            return <li key={stage} data-stage={stage} data-evidence-state={state}>
              <strong>{employeeFileStageLabels[stage]}：{stageWords(stage, state)}</strong>
              <p>范围：{record?.scope || '未提供'} · 来源：{record?.source || '未提供'} · 时间：{record?.observed_at || '未提供'}</p>
              {record && state !== record.state && <p>记录缺少同版本依据或属于其他材料；当前项待核实。</p>}
              {!registered && <p>本次输入或输出尚未登记此版本材料。</p>}
              {record?.message && <p>{record.message}</p>}
            </li>
          })}</ol>
        </article>)}
      <p>服务端交付后果：{run.delivery_consequence || '尚未提供，不能据单项检查推断交付完成。'}</p>
    </section>
    <p role="status">{employeeOperationWords[props.receipt.state]}{props.receipt.message ? `：${props.receipt.message}` : ''}</p>
    <div><button type="button" data-action="inspect_run" disabled={!!inspectReason} title={inspectReason || undefined} onClick={() => {
      if (!employeeRunActionReason(props, 'inspect_run') && run.attempt !== null && run.snapshot_revision) props.onInspect?.({
        project_id: run.project_id, order_id: run.order_id, run_id: run.run_id, node_id: run.node_id, attempt: run.attempt,
        expected_snapshot_revision: run.snapshot_revision, read_only: true,
      })
    }}>只读核对本次运行</button>{inspectReason && <p className="employee-operations__muted">{inspectReason}</p>}</div>
  </section>
}
