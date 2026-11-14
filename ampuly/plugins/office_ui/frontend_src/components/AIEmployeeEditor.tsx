import type { ReactNode } from 'react'

/** Private editor view models, not an AE00 wire contract. The host maps authoritative snapshots and actions. */
export type AIEmployeeEditorScope = 'standard_package' | 'project_instance' | 'node_override'
export type AIEmployeeEditorAction = 'save' | 'test' | 'publish'
export type AIEmployeeEditorResultState = 'idle' | 'pending' | 'received' | 'succeeded' | 'failed' | 'unknown' | 'partial'

export interface AIEmployeeEditorBinding {
  id: string
  version: string
}

export interface AIEmployeeEditorDraft {
  name: string
  delivery: {
    responsibility: string; inputs: string; outputs: string; acceptance: string
    applicable_tasks: string; excluded_tasks: string; format: string; environment: string
  }
  method: { instructions: string; skills: AIEmployeeEditorBinding[]; tools: AIEmployeeEditorBinding[] }
  knowledge: { references: string; boundaries: string; model_id: string; model_constraints: string }
  people: {
    owner_id: string; reviewer_id: string; return_to_id: string
    approval_required: boolean; return_instructions: string
  }
  version: { candidate_version: string; change_note: string }
}

export interface AIEmployeeEditorCatalogEntry {
  id: string
  label: string
  version?: string
  description?: string
  permission_note?: string
  retest_scope?: string
  available?: boolean
}

/** Package / instance identity only; employee_id belongs to the existing personnel roster. */
export interface AIEmployeeEditorTarget {
  package_id: string
  project_id?: string
  instance_id?: string
  node_id?: string
}

export interface AIEmployeeEditorTestSetup {
  fixed_input: string
  expected_output: string
  budget_limit: string
  currency: string
}

export interface AIEmployeeEditorTestEvidence {
  state: 'not_run' | 'pending' | 'passed' | 'failed' | 'unknown' | 'partial'
  target_scope?: AIEmployeeEditorScope
  target?: AIEmployeeEditorTarget
  version?: string
  revision?: string
  execution_mode?: 'simulation' | 'provider' | 'unknown'
  scope?: string
  formats?: readonly string[]
  environments?: readonly string[]
  checks?: readonly { label: string; state: 'passed' | 'failed' | 'unknown' | 'not_checked' }[]
  message?: string
  cost?: { state: 'reported' | 'unknown'; amount?: number | null; currency?: string; basis?: string }
}

export interface AIEmployeeEditorRequest {
  action: AIEmployeeEditorAction
  scope: AIEmployeeEditorScope
  target: AIEmployeeEditorTarget
  expected_revision: string
  version: string
  draft?: AIEmployeeEditorDraft
  test?: { fixed_input: string; expected_output: string; budget_limit: number; currency: string }
}

export interface AIEmployeeEditorProps {
  draft: AIEmployeeEditorDraft
  target: AIEmployeeEditorTarget
  /** No default: changing scope must load that target's revision and allowed_actions in the host. */
  scope: AIEmployeeEditorScope | null
  allowed_scopes: readonly AIEmployeeEditorScope[]
  saved_version: string
  base_revision: string | null
  current_revision: string | null
  connected: boolean
  dirty: boolean
  busy: AIEmployeeEditorAction | 'reload' | null
  /** Projected from real server actions for this scope; absence never grants permission.
   * 'test' is package-only and must come from the authorized employee_package_test_prepare/start flow. */
  allowed_actions: readonly AIEmployeeEditorAction[]
  result: { state: AIEmployeeEditorResultState; action?: AIEmployeeEditorAction; message?: string }
  catalogs: {
    skills: readonly AIEmployeeEditorCatalogEntry[]; tools: readonly AIEmployeeEditorCatalogEntry[]
    models: readonly AIEmployeeEditorCatalogEntry[]; people: readonly AIEmployeeEditorCatalogEntry[]
  }
  test_setup: AIEmployeeEditorTestSetup
  test_evidence: AIEmployeeEditorTestEvidence | null
  /** A host-computed comparison; this editor neither merges nor replaces other people's changes. */
  differences?: readonly { field: string; baseline: string; current: string }[]
  package_status?: string
  adoption_status?: string
  instance_status?: string
  onChange?: (draft: AIEmployeeEditorDraft) => void
  onScopeChange?: (scope: AIEmployeeEditorScope | null) => void
  onTestSetupChange?: (setup: AIEmployeeEditorTestSetup) => void
  onSave?: (request: AIEmployeeEditorRequest) => void
  /** Package-only intent: the host supplies AE00 reason/data_scope/budget and tracked authorization.
   * Never map it to an instance/node test or the production place_and_launch path. */
  onTest?: (request: AIEmployeeEditorRequest) => void
  onPublish?: (request: AIEmployeeEditorRequest) => void
  /** Read-only reconciliation; a response must not discard dirty changes without a separate host decision. */
  onReload?: (request: { target: AIEmployeeEditorTarget; scope: AIEmployeeEditorScope | null; preserve_draft: true }) => void
  onOpenBinding?: (reference: { kind: 'skill' | 'tool'; id: string; version: string }) => void
}

const scopeWords: Record<AIEmployeeEditorScope, string> = {
  standard_package: '标准包草稿（影响公共包）',
  project_instance: '当前项目实例',
  node_override: '仅本节点覆盖',
}
const resultWords: Record<AIEmployeeEditorResultState, string> = {
  idle: '尚无操作结果', pending: '正在发送，等待回执', received: '请求已收到，等待处理结果',
  succeeded: '服务端已确认完成', failed: '操作失败', unknown: '结果未知，先核实状态', partial: '只完成一部分，先核实差异',
}
const testWords: Record<AIEmployeeEditorTestEvidence['state'], string> = {
  not_run: '尚未测试', pending: '测试处理中', passed: '通过所列检查范围',
  failed: '测试失败', unknown: '测试结果待核实', partial: '部分检查完成',
}

function processing(props: AIEmployeeEditorProps): boolean {
  return !!props.busy || props.result.state === 'pending' || props.result.state === 'received'
}

function targetReason(props: AIEmployeeEditorProps): string {
  if (!props.scope) return '先选择保存作用范围；不会默认修改公共包。'
  if (!props.allowed_scopes.includes(props.scope)) return '当前作用范围不可编辑。'
  if (!props.target.package_id.trim()) return '标准包标识尚未加载。'
  if (props.scope !== 'standard_package' && !props.target.project_id?.trim()) return '先选择当前项目。'
  if (props.scope === 'project_instance' && !props.target.instance_id?.trim()) return '项目实例尚未确定；包名不能代替实例。'
  if (props.scope === 'node_override' && !props.target.node_id?.trim()) return '当前节点尚未确定。'
  return ''
}

export function aiEmployeeEditorHasConflict(props: AIEmployeeEditorProps): boolean {
  return !!props.base_revision?.trim() && !!props.current_revision?.trim()
    && props.base_revision.trim() !== props.current_revision.trim()
}

export function aiEmployeeEditorEvidenceCurrent(props: AIEmployeeEditorProps): boolean {
  const evidence = props.test_evidence
  if (!evidence || !props.scope || targetReason(props) || evidence.target_scope !== props.scope || !evidence.target) return false
  const matchingTarget = evidence.target.package_id === props.target.package_id
    && (props.scope === 'standard_package' || evidence.target.project_id === props.target.project_id)
    && (props.scope === 'standard_package' || evidence.target.instance_id === props.target.instance_id)
    && (props.scope !== 'node_override' || evidence.target.node_id === props.target.node_id)
  return matchingTarget && !props.dirty && !!props.base_revision?.trim()
    && props.current_revision === props.base_revision && !!props.saved_version.trim()
    && props.draft.version.candidate_version === props.saved_version
    && evidence.revision === props.base_revision && evidence.version === props.saved_version
}

/** The server still rechecks scope, revision, budget and eligibility when the host dispatches. */
export function aiEmployeeEditorActionReason(props: AIEmployeeEditorProps, action: AIEmployeeEditorAction): string {
  if (!props.connected) return '连接已断开；保留草稿，恢复连接后核实状态。'
  if (processing(props)) return '当前请求仍在处理中。'
  if (props.result.state === 'unknown' || props.result.state === 'partial') return '先重新读取并核实上次结果；不会自动重发。'
  const reason = targetReason(props)
  if (reason) return reason
  if (!props.base_revision?.trim() || !props.current_revision?.trim()) return 'revision 尚未加载，暂不可提交。'
  if (aiEmployeeEditorHasConflict(props)) return '版本已变化；查看差异并重新载入，拒绝覆盖。'
  if (!props.allowed_actions.includes(action)) return '服务端未允许此操作。'
  if (action === 'test' && props.scope !== 'standard_package') return '测试仅用于标准包；实例与节点范围尚无测试动作。'
  const callback = action === 'save' ? props.onSave : action === 'test' ? props.onTest : props.onPublish
  if (!callback) return '此操作尚未接线。'
  if (action === 'save') {
    if (!props.draft.version.candidate_version.trim()) return '填写候选版本。'
    return props.dirty ? '' : '没有未保存改动。'
  }
  if (props.dirty) return '先保存改动；测试与发布针对已保存的版本。'
  if (!props.saved_version.trim() || props.draft.version.candidate_version.trim() !== props.saved_version.trim()) {
    return '候选版本与已保存版本不同，先核实版本。'
  }
  if (props.test_evidence?.state === 'pending') return '测试仍在处理中。'
  if (action === 'test') {
    if (props.test_evidence?.state === 'unknown' || props.test_evidence?.state === 'partial') return '先核实上次测试结果与费用，不重复提交测试。'
    const setup = props.test_setup
    if (!setup.fixed_input.trim() || !setup.expected_output.trim()) return '填写固定测试输入与预期结果。'
    if (!setup.budget_limit.trim() || !Number.isFinite(Number(setup.budget_limit)) || Number(setup.budget_limit) < 0
      || !setup.currency.trim()) return '填写有效的测试预算上限与币种；无价不能按 0 计算。'
    return ''
  }
  if (props.scope !== 'standard_package') return '包发布只用于标准包；项目采用与实例就绪分别处理。'
  if (props.test_evidence?.state !== 'passed' || !aiEmployeeEditorEvidenceCurrent(props)) {
    return '尚无当前版本与 revision 的通过记录；未知或部分完成不能算通过。'
  }
  return ''
}

export function aiEmployeeEditorRequest(props: AIEmployeeEditorProps, action: AIEmployeeEditorAction): AIEmployeeEditorRequest | null {
  if (aiEmployeeEditorActionReason(props, action) || !props.scope || !props.base_revision) return null
  const target: AIEmployeeEditorTarget = { package_id: props.target.package_id }
  if (props.scope !== 'standard_package') target.project_id = props.target.project_id
  if (props.scope === 'project_instance') target.instance_id = props.target.instance_id
  if (props.scope === 'node_override') {
    target.node_id = props.target.node_id
    if (props.target.instance_id) target.instance_id = props.target.instance_id
  }
  return {
    action, scope: props.scope, target, expected_revision: props.base_revision,
    version: action === 'save' ? props.draft.version.candidate_version : props.saved_version,
    ...(action === 'save' ? { draft: structuredClone(props.draft) } : {}),
    ...(action === 'test' ? { test: {
      fixed_input: props.test_setup.fixed_input, expected_output: props.test_setup.expected_output,
      budget_limit: Number(props.test_setup.budget_limit), currency: props.test_setup.currency.trim(),
    } } : {}),
  }
}

export function aiEmployeeEditorCostWords(cost: AIEmployeeEditorTestEvidence['cost']): string {
  if (cost?.state !== 'reported' || typeof cost.amount !== 'number' || !Number.isFinite(cost.amount)
    || cost.amount < 0 || !cost.currency?.trim() || !cost.basis?.trim()) return '费用待核实（尚无完整计价依据）'
  return `已报告费用：${cost.amount} ${cost.currency}；依据：${cost.basis}`
}

function Choice({ label, value, entries, onChange }: {
  label: string; value: string; entries: readonly AIEmployeeEditorCatalogEntry[]; onChange: (value: string) => void
}) {
  const missing = value && !entries.some(entry => entry.id === value)
  return <label className="ai-employee-editor__field">{label}
    <select aria-label={label} value={value} onChange={event => onChange(event.target.value)}>
      <option value="">请选择</option>
      {missing && <option value={value}>未解析引用 · {value}</option>}
      {entries.map(entry => <option key={entry.id} value={entry.id} disabled={entry.available === false}>
        {entry.label}{entry.version ? ` · ${entry.version}` : ''}{entry.available === false ? '（不可选）' : ''}
      </option>)}
    </select>
  </label>
}

function TextField({ label, value, onChange, multiline = false }: {
  label: string; value: string; onChange: (value: string) => void; multiline?: boolean
}) {
  return <label className="ai-employee-editor__field">{label}
    {multiline
      ? <textarea aria-label={label} rows={3} value={value} onChange={event => onChange(event.target.value)} />
      : <input aria-label={label} value={value} onChange={event => onChange(event.target.value)} />}
  </label>
}

/** Fully controlled leaf: no network, storage, optimistic success, authorization changes or draft reconciliation. */
export function AIEmployeeEditor(props: AIEmployeeEditorProps) {
  const { draft, catalogs } = props
  const conflict = aiEmployeeEditorHasConflict(props)
  const editingDisabled = processing(props) || !props.onChange
  const change = (apply: (next: AIEmployeeEditorDraft) => void) => {
    if (editingDisabled) return
    const next = structuredClone(draft)
    apply(next)
    props.onChange?.(next)
  }
  const dispatch = (action: AIEmployeeEditorAction) => {
    const request = aiEmployeeEditorRequest(props, action)
    if (!request) return
    if (action === 'save') props.onSave?.(request)
    else if (action === 'test') props.onTest?.(request)
    else props.onPublish?.(request)
  }
  const section = (id: string, title: string, children: ReactNode) => <fieldset
    className="ai-employee-editor__section" data-section={id} disabled={editingDisabled}>
    <legend>{title}</legend>{children}
  </fieldset>
  const bindings = (kind: 'skill' | 'tool') => {
    const key = kind === 'skill' ? 'skills' : 'tools'
    const word = kind === 'skill' ? 'Skill' : '工具'
    return <div className="ai-employee-editor__bindings">
      <h4>{word}引用</h4>
      {draft.method[key].length === 0 && <p className="ai-employee-editor__muted">还没有选择{word}；目录为空时等待父层加载。</p>}
      {draft.method[key].map((binding, index) => {
        const entry = catalogs[key].find(item => item.id === binding.id)
        return <div key={index} className="ai-employee-editor__binding" data-binding-kind={kind}>
          <Choice label={`${word} ${index + 1}`} value={binding.id} entries={catalogs[key]} onChange={id => change(next => {
            next.method[key][index] = { id, version: catalogs[key].find(item => item.id === id)?.version ?? '' }
          })} />
          <TextField label={`${word} ${index + 1} 绑定版本`} value={binding.version}
            onChange={version => change(next => { next.method[key][index].version = version })} />
          <p className="ai-employee-editor__muted">{entry?.description || (binding.id ? '此引用等待目录核对。' : '选择引用后显示权限与重测范围。')}</p>
          <p className="ai-employee-editor__muted">权限：{entry?.permission_note || '尚未提供；选择引用不授予权限。'}</p>
          <p className="ai-employee-editor__muted">需重测范围：{entry?.retest_scope || '由服务端重新评估。'}</p>
          <div className="ai-employee-editor__row">
            <button type="button" disabled={!props.onOpenBinding || !binding.id.trim()} onClick={() => {
              if (!editingDisabled && binding.id.trim()) props.onOpenBinding?.({ kind, ...binding })
            }}>打开{word}编辑器</button>
            <button type="button" onClick={() => change(next => { next.method[key].splice(index, 1) })}>移除{word} {index + 1}</button>
          </div>
        </div>
      })}
      <button type="button" onClick={() => change(next => { next.method[key].push({ id: '', version: '' }) })}>添加{word}引用</button>
    </div>
  }
  const evidence = props.test_evidence
  const model = catalogs.models.find(entry => entry.id === draft.knowledge.model_id)
  const reloadDisabled = !props.connected || processing(props) || !props.onReload
  return <section className="ai-employee-editor" data-testid="ai-employee-editor" data-dirty={props.dirty}
    data-connected={props.connected} data-result-state={props.result.state} data-revision-conflict={conflict}>
    <header className="ai-employee-editor__header">
      <h2>AI 员工编辑器</h2>
      <p className="ai-employee-editor__muted">{draft.name || '未命名 AI 员工'} · 已保存版本 {props.saved_version || '待加载'}</p>
      <label className="ai-employee-editor__field">保存作用范围
        <select aria-label="保存作用范围" value={props.scope ?? ''} disabled={processing(props) || !props.onScopeChange}
          onChange={event => {
            const next = (event.target.value || null) as AIEmployeeEditorScope | null
            if (!processing(props) && (next === null || props.allowed_scopes.includes(next))) props.onScopeChange?.(next)
          }}>
          <option value="">请选择作用范围</option>
          {(Object.keys(scopeWords) as AIEmployeeEditorScope[]).map(scope => <option key={scope} value={scope}
            disabled={!props.allowed_scopes.includes(scope)}>{scopeWords[scope]}</option>)}
        </select>
      </label>
      <p className="ai-employee-editor__muted">{props.scope ? `本次编辑：${scopeWords[props.scope]}。` : '先选择保存作用范围；不会默认修改公共包。'}
        {props.scope === 'standard_package' && ' 公共包变更需要独立测试与发布。'}
        {props.scope === 'project_instance' && ` 项目 ${props.target.project_id || '待选择'} · 实例 ${props.target.instance_id || '待确定'}。`}
        {props.scope === 'node_override' && ` 项目 ${props.target.project_id || '待选择'} · 节点 ${props.target.node_id || '待确定'}。`}
      </p>
    </header>
    <div className="ai-employee-editor__status" role={conflict || ['failed', 'unknown', 'partial'].includes(props.result.state) ? 'alert' : 'status'}>
      <p>{props.connected ? '已连接' : '连接已断开，操作暂停'} · {props.dirty ? '有未保存改动' : '无未保存改动'}
        {props.busy && ` · ${props.busy === 'reload' ? '正在读取' : props.busy === 'save' ? '正在保存' : props.busy === 'test' ? '正在测试' : '正在请求发布'}`}</p>
      <p>编辑基准 revision：{props.base_revision || '待加载'} · 当前 revision：{props.current_revision || '待加载'}</p>
      <p>{props.result.action && `${props.result.action === 'save' ? '保存' : props.result.action === 'test' ? '测试' : '发布'}：`}
        {resultWords[props.result.state]}{props.result.message ? `：${props.result.message}` : ''}</p>
      {conflict && <p>版本已变化；拒绝覆盖。重新读取保留草稿，由父层提供差异与载入选择。</p>}
      {(props.result.state === 'unknown' || props.result.state === 'partial') && <p>先核实已完成的范围；不会自动重发保存、测试或发布。</p>}
      <button type="button" disabled={reloadDisabled} onClick={() => {
        if (!reloadDisabled) props.onReload?.({ target: {
          package_id: props.target.package_id,
          ...(props.target.project_id !== undefined ? { project_id: props.target.project_id } : {}),
          ...(props.target.instance_id !== undefined ? { instance_id: props.target.instance_id } : {}),
          ...(props.target.node_id !== undefined ? { node_id: props.target.node_id } : {}),
        }, scope: props.scope, preserve_draft: true })
      }}>重新读取并保留草稿</button>
      {props.differences && props.differences.length > 0 && <details className="ai-employee-editor__differences" open={conflict}>
        <summary>服务端差异（{props.differences.length} 项）</summary>
        <table><thead><tr><th>字段</th><th>编辑基准</th><th>当前保存值</th></tr></thead>
          <tbody>{props.differences.map((item, index) => <tr key={index}><th>{item.field}</th><td>{item.baseline}</td><td>{item.current}</td></tr>)}</tbody>
        </table>
      </details>}
    </div>
    <div className="ai-employee-editor__sections">
      {section('delivery', '职责与交付', <>
        <TextField label="员工名称" value={draft.name} onChange={name => change(next => { next.name = name })} />
        {([['responsibility', '岗位职责'], ['inputs', '输入要求'], ['outputs', '交付物'], ['acceptance', '验收标准'],
          ['applicable_tasks', '适用任务'], ['excluded_tasks', '不适用任务'], ['format', '交付格式'], ['environment', '运行环境']] as const)
          .map(([key, label]) => <TextField key={key} label={label} value={draft.delivery[key]} multiline
            onChange={value => change(next => { next.delivery[key] = value })} />)}
      </>)}
      {section('method', '工作方法与工具', <>
        <TextField label="工作方法与执行边界" value={draft.method.instructions} multiline
          onChange={value => change(next => { next.method.instructions = value })} />
        <p className="ai-employee-editor__muted">更换 Skill、工具或绑定版本后，权限与重测范围由服务端重新评估；本页的选择不授予工具权限。</p>
        {bindings('skill')}{bindings('tool')}
      </>)}
      {section('knowledge', '企业知识与模型', <>
        <TextField label="企业知识引用与来源" value={draft.knowledge.references} multiline
          onChange={value => change(next => { next.knowledge.references = value })} />
        <TextField label="知识使用边界" value={draft.knowledge.boundaries} multiline
          onChange={value => change(next => { next.knowledge.boundaries = value })} />
        <Choice label="模型能力" value={draft.knowledge.model_id} entries={catalogs.models}
          onChange={id => change(next => { next.knowledge.model_id = id })} />
        <p className="ai-employee-editor__muted">{model?.description || '模型能力与可用性等待目录核对。'}</p>
        <p className="ai-employee-editor__muted">模型权限：{model?.permission_note || '由服务端决定。'} 需重测范围：{model?.retest_scope || '由服务端评估。'}</p>
        <TextField label="模型约束与回退要求" value={draft.knowledge.model_constraints} multiline
          onChange={value => change(next => { next.knowledge.model_constraints = value })} />
      </>)}
      {section('people', '人员与回转', <>
        {([['owner_id', '负责人'], ['reviewer_id', '复核人'], ['return_to_id', '打回后交给谁']] as const)
          .map(([key, label]) => <Choice key={key} label={label} value={draft.people[key]} entries={catalogs.people}
            onChange={id => change(next => { next.people[key] = id })} />)}
        <label className="ai-employee-editor__check"><input type="checkbox" checked={draft.people.approval_required}
          onChange={event => change(next => { next.people.approval_required = event.target.checked })} />需要人工复核</label>
        <TextField label="打回意见与重派规则" value={draft.people.return_instructions} multiline
          onChange={value => change(next => { next.people.return_instructions = value })} />
        <p className="ai-employee-editor__muted">人员配置只描述交接规则；真实重派需另走服务端资格、状态与幂等检查。</p>
      </>)}
      <section className="ai-employee-editor__section" data-section="version" aria-label="测试与版本">
        <h3>测试与版本</h3>
        <fieldset className="ai-employee-editor__inner" disabled={editingDisabled}>
          <legend>候选版本</legend>
          <TextField label="候选版本" value={draft.version.candidate_version}
            onChange={value => change(next => { next.version.candidate_version = value })} />
          <TextField label="变更说明" value={draft.version.change_note} multiline
            onChange={value => change(next => { next.version.change_note = value })} />
        </fieldset>
        <dl className="ai-employee-editor__versions">
          <div><dt>标准包发布</dt><dd>{props.package_status || '尚未加载'}</dd></div>
          <div><dt>项目采用</dt><dd>{props.adoption_status || '尚未加载'}</dd></div>
          <div><dt>实例就绪</dt><dd>{props.instance_status || '尚未加载'}</dd></div>
        </dl>
        <p className="ai-employee-editor__muted">包发布、项目采用、实例就绪分别确认；测试通过只覆盖所列版本、格式与环境。</p>
        <p className="ai-employee-editor__muted">当前测试入口只针对已保存标准包；项目实例与节点覆盖分别保存，暂不提供实例或节点测试。</p>
        <fieldset className="ai-employee-editor__inner" disabled={processing(props) || !props.onTestSetupChange}>
          <legend>固定输入与预算</legend>
          {([['fixed_input', '固定测试输入'], ['expected_output', '预期测试结果'], ['budget_limit', '测试预算上限'], ['currency', '测试预算币种']] as const)
            .map(([key, label]) => <TextField key={key} label={label} value={props.test_setup[key]}
              multiline={key === 'fixed_input' || key === 'expected_output'} onChange={value => {
                if (!processing(props)) props.onTestSetupChange?.({ ...props.test_setup, [key]: value })
              }} />)}
        </fieldset>
        <div className="ai-employee-editor__evidence" data-testid="ai-employee-test-evidence" data-test-state={evidence?.state || 'not_run'}>
          <p>{testWords[evidence?.state || 'not_run']}
            {evidence?.execution_mode === 'simulation' && '（合同模拟）'}{evidence?.execution_mode === 'provider' && '（模型执行记录）'}</p>
          {evidence && <>
            <p>记录版本 {evidence.version || '未知'} · revision {evidence.revision || '未知'}</p>
            {!aiEmployeeEditorEvidenceCurrent(props) && <p>这份记录不覆盖当前编辑内容；需要核对版本与 revision，修改后重测。</p>}
            <p>检查范围：{evidence.scope || '未提供'} · 格式：{evidence.formats?.join('、') || '未提供'} · 环境：{evidence.environments?.join('、') || '未提供'}</p>
            {evidence.checks && <ul>{evidence.checks.map((check, index) => <li key={index} data-check-state={check.state}>
              {check.label}：{check.state === 'passed' ? '通过' : check.state === 'failed' ? '失败' : check.state === 'unknown' ? '待核实' : '未检查'}
            </li>)}</ul>}
            {evidence.message && <p>{evidence.message}</p>}
          </>}
          <p>{aiEmployeeEditorCostWords(evidence?.cost)}</p>
        </div>
      </section>
    </div>
    <footer className="ai-employee-editor__actions">
      {(['save', 'test', 'publish'] as const).map(action => {
        const reason = aiEmployeeEditorActionReason(props, action)
        const label = action === 'save' ? '保存当前作用范围' : action === 'test' ? '测试已保存标准包' : '请求发布标准包'
        return <div key={action} className="ai-employee-editor__action">
          <button type="button" data-action={action} disabled={!!reason} title={reason || undefined} onClick={() => dispatch(action)}>{label}</button>
          {reason && <p className="ai-employee-editor__muted">{reason}</p>}
        </div>
      })}
      <p className="ai-employee-editor__muted">操作资格由服务端决定；点击后等待回执与处理结果，页面不会自行标记成功。</p>
    </footer>
  </section>
}
