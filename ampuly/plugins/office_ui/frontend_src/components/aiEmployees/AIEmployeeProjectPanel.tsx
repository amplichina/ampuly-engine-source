/** Private leaf view models. The host maps AE00 snapshots, permissions and tracked requests. */
export type EmployeeOperationState = 'idle' | 'pending' | 'received' | 'succeeded' | 'failed' | 'unknown' | 'partial'
export interface EmployeeOperationReceipt { state: EmployeeOperationState; request_id?: string | null; message?: string }

export const employeeOperationWords: Record<EmployeeOperationState, string> = {
  idle: '尚无操作结果', pending: '正在发送，等待回执', received: '请求已收到，等待处理结果',
  succeeded: '服务端已确认完成', failed: '操作失败', unknown: '结果未知，先只读核对', partial: '部分完成，先只读核对',
}

export function employeeOperationBlocksWrite(receipt: EmployeeOperationReceipt, busy: boolean): boolean {
  return busy || ['pending', 'received', 'unknown', 'partial'].includes(receipt.state)
}

export interface EmployeeQualityMetric {
  name: string
  numerator: number | null
  denominator: number | null
  source: string | null
  observed_at: string | null
}

export function employeeQualityWords(metric: EmployeeQualityMetric): string {
  const { numerator, denominator } = metric
  if (typeof numerator !== 'number' || typeof denominator !== 'number'
    || !Number.isFinite(numerator) || !Number.isFinite(denominator)
    || numerator < 0 || denominator < 0 || numerator > denominator
    || !metric.source?.trim() || !metric.observed_at?.trim()) return '数据待核实（缺完整分子、分母、来源或时间）'
  if (denominator === 0) return '尚无样本（分母为 0），不计算通过率'
  return `${numerator} / ${denominator}`
}

export interface EmployeeConnectionName {
  connection_id: string
  name: string
  state: 'connected' | 'missing' | 'blocked' | 'unknown'
}

export interface AIEmployeeProjectView {
  project_id: string
  package_id: string
  package_name: string
  package_version: string
  instance_id: string
  /** Optional verified instance label; distinct from the employee package's name. */
  instance_name?: string
  purpose: string
  owner_name: string | null
  revision: string | null
  current_revision: string | null
  adoption_state: 'adopted' | 'revoked' | 'unknown'
  intake_state: 'open' | 'paused' | 'closed' | 'unknown'
  execution_state: 'active' | 'paused' | 'stopped' | 'unknown'
  ready: 'ready' | 'not_ready' | 'unknown'
  readiness_source: string | null
  observed_at: string | null
  gaps: readonly string[] | null
  knowledge_connections: readonly EmployeeConnectionName[] | null
  software_connections: readonly EmployeeConnectionName[] | null
  validation: {
    state: 'not_tested' | 'passed' | 'failed' | 'unknown' | 'partial'
    package_version: string | null
    instance_revision: string | null
    scope: string | null
    formats: readonly string[] | null
    environments: readonly string[] | null
    model_name: string | null
    tool_names: readonly string[] | null
    taskset_fingerprint?: string | null
    source: string | null
    observed_at: string | null
  } | null
  quality: readonly EmployeeQualityMetric[] | null
  allowed_actions: readonly AIEmployeeProjectAction[]
  busy: boolean
  receipt: EmployeeOperationReceipt
}

/** 'test' is a disabled UI placeholder: AE00 defines package authorization tests, no instance test action. */
export type AIEmployeeProjectAction = 'configure' | 'test' | 'view_quality'
export interface AIEmployeeProjectIntent {
  action: AIEmployeeProjectAction
  project_id: string
  package_id: string
  package_version: string
  instance_id: string
  expected_revision: string
}

export interface AIEmployeeProjectPanelProps {
  project_id: string | null
  connected: boolean
  /** null = not loaded; [] = an authoritative empty project list. Never substitute sample employees. */
  employees: readonly AIEmployeeProjectView[] | null
  load_error?: string | null
  read_busy?: boolean
  onAction?: (intent: AIEmployeeProjectIntent) => void
  /** Narrow hosts must name the actual supported edit, not imply full configuration. */
  configure_label?: string
  onReload?: (request: { project_id: string; read_only: true }) => void
}

export function employeeProjectActionReason(props: AIEmployeeProjectPanelProps, employee: AIEmployeeProjectView, action: AIEmployeeProjectAction): string {
  if (!props.connected) return '连接已断开，先恢复连接并核对状态。'
  if (!props.project_id?.trim() || employee.project_id !== props.project_id) return '目标不属于当前项目。'
  if (!props.employees?.some(item => item === employee)) return '目标不在当前项目快照中。'
  if (!employee.package_id.trim() || !employee.package_version.trim() || !employee.instance_id.trim()) return '包版本或项目实例尚未明确。'
  if (!employee.revision?.trim() || !employee.current_revision?.trim()) return '实例 revision 尚未加载。'
  if (employee.revision !== employee.current_revision) return '实例已变化，先重新读取；不覆盖旧 revision。'
  if (!employee.allowed_actions.includes(action)) return '服务端未允许此操作。'
  if (action === 'test') return '项目实例测试暂不可用；请到标准包进行授权测试。'
  if (!props.onAction) return '此操作尚未接线。'
  if (employeeOperationBlocksWrite(employee.receipt, employee.busy)) return '上次请求尚未核实；先只读核对，不重发。'
  return ''
}

export function employeeProjectIntent(props: AIEmployeeProjectPanelProps, employee: AIEmployeeProjectView, action: AIEmployeeProjectAction): AIEmployeeProjectIntent | null {
  if (employeeProjectActionReason(props, employee, action) || !employee.revision || !props.project_id) return null
  return { action, project_id: props.project_id, package_id: employee.package_id, package_version: employee.package_version,
    instance_id: employee.instance_id, expected_revision: employee.revision }
}

function Connections({ label, entries }: { label: string; entries: readonly EmployeeConnectionName[] | null }) {
  const words = { connected: '服务端记录已连接', missing: '缺连接', blocked: '受阻', unknown: '待核实' }
  return <div className="employee-project-panel__connections"><h4>{label}</h4>
    {entries === null ? <p>连接名称尚未加载。</p> : entries.length === 0 ? <p>尚未配置连接。</p>
      : <ul>{entries.map(entry => <li key={entry.connection_id}>{entry.name || '未命名连接'} · {words[entry.state]}</li>)}</ul>}
  </div>
}

/** Names and evidence only: credentials, knowledge bodies and live state transitions remain in the host. */
export function AIEmployeeProjectPanel(props: AIEmployeeProjectPanelProps) {
  const reloadDisabled = !props.connected || !props.project_id?.trim() || !!props.read_busy || !props.onReload
  const intakeWords = { open: '开放接单', paused: '暂停接单', closed: '关闭接单', unknown: '接单状态待核实' }
  const executionWords = { active: '允许执行', paused: '执行已暂停', stopped: '执行已停止', unknown: '执行状态待核实' }
  const employees = props.employees?.filter(employee => employee.project_id === props.project_id) ?? null
  return <section className="employee-project-panel employee-operations" data-testid="ai-employee-project-panel">
    <header><h2>项目 AI 员工</h2><p className="employee-operations__muted">按项目实例查看已采用的包版本、用途与运行资格。</p></header>
    {!props.connected && <p role="status">连接已断开；列表状态需要重新核实。</p>}
    {props.load_error && <p role="alert">{props.load_error}</p>}
    {!props.project_id?.trim() ? <p>先选择项目，再查看本项目采用的 AI 员工。</p>
      : employees === null ? <p>真实员工列表尚未加载。</p>
      : employees.length === 0 ? <p>本项目尚未采用 AI 员工实例。</p>
      : <div className="employee-project-panel__list">{employees.map((employee, index) => {
        const validation = employee.validation
        const validationCurrent = !!validation && validation.package_version === employee.package_version
          && validation.instance_revision === employee.revision && employee.current_revision === employee.revision
          && !!validation.source?.trim() && !!validation.observed_at?.trim()
        const readinessCurrent = !!employee.revision?.trim() && employee.revision === employee.current_revision
          && !!employee.readiness_source?.trim() && !!employee.observed_at?.trim()
        return <article key={`${employee.project_id}:${employee.instance_id}:${index}`} className="employee-operations__card"
          data-instance-id={employee.instance_id} data-project-id={employee.project_id}>
          <h3>{employee.instance_name || employee.package_name || '未命名项目实例'} · 包版本 {employee.package_version || '待加载'}</h3>
          <dl className="employee-operations__facts">
            <div><dt>项目实例</dt><dd>{employee.instance_id || '待确定'}</dd></div>
            <div><dt>实例用途</dt><dd>{employee.purpose || '待说明'}</dd></div>
            <div><dt>负责人</dt><dd>{employee.owner_name || '待配置'}</dd></div>
            <div><dt>项目采用</dt><dd>{employee.adoption_state === 'adopted' ? '服务端记录已采用' : employee.adoption_state === 'revoked' ? '已撤销采用' : '采用状态待核实'}</dd></div>
            <div><dt>接单状态</dt><dd>{intakeWords[employee.intake_state]}</dd></div>
            <div><dt>执行状态</dt><dd>{executionWords[employee.execution_state]}</dd></div>
            <div><dt>实例 revision</dt><dd>{employee.revision || '待加载'} · 当前 {employee.current_revision || '待加载'}</dd></div>
            <div><dt>服务端就绪</dt><dd>{!readinessCurrent ? '就绪性待核实' : employee.ready === 'ready' ? '本次快照确认就绪' : employee.ready === 'not_ready' ? '尚未就绪' : '就绪性待核实'}</dd></div>
          </dl>
          <p className="employee-operations__muted">就绪依据：{employee.readiness_source || '尚未提供'} · 时间：{employee.observed_at || '尚未提供'}</p>
          <div><h4>就绪缺口</h4>{employee.gaps === null ? <p>缺口清单尚未加载。</p> : employee.gaps.length === 0
            ? <p>本次服务端快照未列缺口；接单与执行仍分别以各自状态为准。</p>
            : <ul>{employee.gaps.map((gap, gapIndex) => <li key={gapIndex}>{gap}</li>)}</ul>}</div>
          <div className="employee-project-panel__connection-grid"><Connections label="企业知识连接" entries={employee.knowledge_connections} />
            <Connections label="软件连接" entries={employee.software_connections} /></div>
          <details><summary>验证范围与质量记录</summary>
            {!validation ? <p>验证记录尚未加载。</p> : <div className="employee-operations__stack">
              <p data-validation-state={validationCurrent ? validation.state : 'unknown'}>{!validationCurrent ? '历史或不完整验证记录，当前实例需核对'
                : validation.state === 'passed' ? '已通过所列检查范围' : validation.state === 'failed' ? '验证失败'
                : validation.state === 'partial' ? '部分验证完成' : validation.state === 'not_tested' ? '尚未测试' : '验证结果待核实'}</p>
              <p>记录版本 {validation.package_version || '未知'} · 实例 revision {validation.instance_revision || '未知'}</p>
              <p>范围：{validation.scope || '未提供'} · 格式：{validation.formats?.join('、') || '未提供'} · 环境：{validation.environments?.join('、') || '未提供'}</p>
              <p>模型：{validation.model_name || '未提供'} · 工具：{validation.tool_names?.join('、') || '未提供'} · 任务集指纹：{validation.taskset_fingerprint || '未提供'}</p>
              <p>来源：{validation.source || '未提供'} · 时间：{validation.observed_at || '未提供'}</p>
            </div>}
            {employee.quality === null ? <p>质量指标尚未加载。</p> : employee.quality.length === 0 ? <p>尚无质量样本。</p>
              : <ul>{employee.quality.map((metric, metricIndex) => <li key={metricIndex}>{metric.name}：{employeeQualityWords(metric)}
                <span className="employee-operations__muted"> · 来源：{metric.source || '未提供'} · 时间：{metric.observed_at || '未提供'}</span></li>)}</ul>}
          </details>
          <p role={['failed', 'unknown', 'partial'].includes(employee.receipt.state) ? 'alert' : 'status'}>
            {employeeOperationWords[employee.receipt.state]}{employee.receipt.message ? `：${employee.receipt.message}` : ''}</p>
          <div className="employee-operations__actions">{(['configure', 'test', 'view_quality'] as const).map(action => {
            const reason = employeeProjectActionReason(props, employee, action)
            return <div key={action}><button type="button" data-action={action} data-instance-id={employee.instance_id}
              disabled={!!reason} title={reason || undefined} onClick={() => {
                const intent = employeeProjectIntent(props, employee, action)
                if (intent) props.onAction?.(intent)
              }}>{action === 'configure' ? (props.configure_label ?? '配置项目实例') : action === 'test' ? '实例测试（暂不可用）' : '查看质量依据'}</button>
              {reason && <p className="employee-operations__muted">{reason}</p>}</div>
          })}</div>
        </article>
      })}</div>}
    <div><button type="button" data-action="reload_project" disabled={reloadDisabled} onClick={() => {
      if (!reloadDisabled && props.project_id) props.onReload?.({ project_id: props.project_id, read_only: true })
    }}>只读核对项目员工</button>{!props.onReload && <p className="employee-operations__muted">读取入口尚未接线。</p>}</div>
  </section>
}


