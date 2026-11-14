import type { ReactNode } from 'react'

/** Private view DTOs. The host maps AE00 snapshots; these are not transport contracts. */
export type AIEmployeeRequestState = 'idle' | 'pending' | 'received' | 'succeeded' | 'failed' | 'unknown' | 'partial'
export type AIEmployeeLibraryAction = 'view' | 'adopt' | 'configure' | 'edit' | 'test' | 'use'
export type AIEmployeeEditScope = 'project_instance' | 'standard_package'
export interface AIEmployeePackageRef { package_id: string; package_version: string }
export interface AIEmployeeInstanceRef extends AIEmployeePackageRef {
  project_id: string; instance_id: string; instance_revision: string
}
export interface AIEmployeePackageView extends AIEmployeePackageRef {
  name: string; responsibility: string; inputs: readonly string[]; outputs: readonly string[]
  applicable: readonly string[]; excluded: readonly string[]
  skills: readonly { id: string; version: string; name: string }[]
  tools: readonly { id: string; version: string; name: string }[]
  models: readonly { id: string; name: string; purpose: string }[]
  source: { label: string; license?: string; reference?: string }
  package_status: 'draft' | 'candidate' | 'published' | 'retired' | 'unknown'
  verification: {
    state: 'not_tested' | 'passed' | 'failed' | 'unknown' | 'partial'
    version?: string; revision?: string; scope: string; formats: readonly string[]; environments: readonly string[]
    mode: 'simulation' | 'provider' | 'unknown'; unchecked: readonly string[]
  }
  cost: { state: 'reported' | 'unknown'; amount?: number; currency?: string; basis?: string }
  revision: string | null; loaded_revision: string | null
  allowed_actions: readonly AIEmployeeLibraryAction[]
}
export interface AIEmployeeInstanceView extends AIEmployeeInstanceRef {
  name: string; purpose: string
  /** Display text is never included in a drag reference. */
  owner_label: string
  loaded_revision: string | null
  server_ready: boolean | null; readiness_revision: string | null; gaps: readonly string[]
  adoption_status: 'adopted' | 'pending' | 'revoked' | 'unknown'
  knowledge_status: string; connection_status: string
  allowed_actions: readonly AIEmployeeLibraryAction[]
}
export interface AIEmployeeProjectView {
  project_id: string; name: string; revision: string | null; loaded_revision: string | null
  allowed_actions: readonly AIEmployeeLibraryAction[]
}
export interface AIEmployeeLibraryRequest {
  action: AIEmployeeLibraryAction; scope: AIEmployeeEditScope
  package: AIEmployeePackageRef; expected_package_revision?: string
  instance?: AIEmployeeInstanceRef; expected_instance_revision?: string
  project_id?: string; expected_project_revision?: string
}
export interface AIEmployeeLibraryProps {
  packages: readonly AIEmployeePackageView[]; instances: readonly AIEmployeeInstanceView[]
  project: AIEmployeeProjectView | null; selected_package: AIEmployeePackageRef | null
  selected_instance_id: string | null; query: string; edit_scope: AIEmployeeEditScope | null
  connected: boolean; busy: boolean; result: { state: AIEmployeeRequestState; message?: string }
  loading?: boolean; error?: string
  onQueryChange?: (query: string) => void
  onSelectPackage?: (reference: AIEmployeePackageRef) => void
  onSelectInstance?: (reference: AIEmployeeInstanceRef) => void
  onSelectProject?: () => void
  onEditScopeChange?: (scope: AIEmployeeEditScope | null) => void
  onView?: (reference: AIEmployeePackageRef) => void
  onAdopt?: (request: AIEmployeeLibraryRequest) => void
  onConfigure?: (request: AIEmployeeLibraryRequest) => void
  onEdit?: (request: AIEmployeeLibraryRequest) => void
  onTest?: (request: AIEmployeeLibraryRequest) => void
  /** Opens the host's destination chooser. This callback is not permission to bind or run. */
  onUse?: (request: AIEmployeeLibraryRequest) => void
}

const stateWords: Record<AIEmployeeRequestState, string> = {
  idle: '尚无操作结果', pending: '正在发送，等待回执', received: '已收到，等待处理结果',
  succeeded: '服务端已确认完成', failed: '操作失败', unknown: '结果未知，先核实状态', partial: '部分完成，先核实差异',
}
const packageWords: Record<AIEmployeePackageView['package_status'], string> = {
  draft: '标准包草稿', candidate: '标准包候选', published: '标准包已发布', retired: '标准包已停用', unknown: '标准包状态待核实',
}

function validId(value: string | null | undefined): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 160
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)
}
export function aiEmployeeInstanceReference(instance: AIEmployeeInstanceView): AIEmployeeInstanceRef {
  return {
    project_id: instance.project_id, instance_id: instance.instance_id, instance_revision: instance.instance_revision,
    package_id: instance.package_id, package_version: instance.package_version,
  }
}
export function aiEmployeeInstanceReadinessReason(instance: AIEmployeeInstanceView, projectId: string | null): string {
  if (!projectId || instance.project_id !== projectId) return '此实例不属于当前项目。'
  if (![instance.project_id, instance.instance_id, instance.instance_revision, instance.package_id, instance.package_version].every(validId)) return '实例标识或版本尚未加载。'
  if (instance.loaded_revision !== instance.instance_revision) return '实例已变化；重新读取后再选择。'
  if (instance.adoption_status !== 'adopted') return '本项目尚未确认采用此实例。'
  if (instance.server_ready !== true) return '实例尚未就绪；先配置知识、连接或人员。'
  if (instance.readiness_revision !== instance.instance_revision) return '就绪记录不对应当前实例 revision。'
  if (instance.gaps.length) return `就绪缺口：${instance.gaps.join('；')}`
  return ''
}
export function aiEmployeeLibraryCostWords(cost: AIEmployeePackageView['cost']): string {
  if (cost.state !== 'reported' || typeof cost.amount !== 'number' || !Number.isFinite(cost.amount)
    || cost.amount < 0 || !cost.currency?.trim() || !cost.basis?.trim()) return '费用待核实；没有完整计价依据'
  return `已报告费用：${cost.amount} ${cost.currency}（${cost.basis}）`
}
export function aiEmployeeLibraryCandidates(props: AIEmployeeLibraryProps, pkg: AIEmployeePackageRef): AIEmployeeInstanceView[] {
  if (!props.project) return []
  return props.instances.filter(item => item.project_id === props.project!.project_id
    && item.package_id === pkg.package_id && item.package_version === pkg.package_version)
}
function selectedInstance(props: AIEmployeeLibraryProps, pkg: AIEmployeePackageRef): AIEmployeeInstanceView | null {
  if (!props.selected_instance_id) return null
  const candidates = aiEmployeeLibraryCandidates(props, pkg).filter(item => item.instance_id === props.selected_instance_id)
  return candidates.length === 1 ? candidates[0] : null
}
export function aiEmployeeLibraryActionReason(props: AIEmployeeLibraryProps, pkg: AIEmployeePackageView, action: AIEmployeeLibraryAction, scope: AIEmployeeEditScope = 'project_instance'): string {
  if (!props.connected) return '连接已断开；恢复连接后核实状态。'
  if (props.busy || props.result.state === 'pending' || props.result.state === 'received') return '请求仍在处理中。'
  if (props.result.state === 'unknown' || props.result.state === 'partial') return '先核实上次操作结果；不会自动重发。'
  if (props.loading || props.error) return '先完成数据读取并核实错误。'
  const matchingPackages = props.packages.filter(item => item.package_id === pkg.package_id && item.package_version === pkg.package_version)
  if (matchingPackages.length !== 1 || matchingPackages[0] !== pkg) return '标准包不在当前目录或标识重复，先重新读取。'
  if (![pkg.package_id, pkg.package_version].every(validId)) return '标准包标识或版本尚未加载。'
  if (!validId(pkg.revision) || pkg.loaded_revision !== pkg.revision) return '标准包 revision 尚未加载或已变化。'
  if (action === 'view') {
    if (!pkg.allowed_actions.includes('view')) return '服务端未允许读取此包的来源介绍；已加载的详情仍可阅读。'
    return props.onView ? '' : '查看入口尚未接线；下方仍可阅读已加载的介绍。'
  }
  // AE00 exposes package test preparation/start, not a project-instance test action.
  // An instance.allowed_actions entry cannot create a transport operation that is absent.
  if (action === 'test' && scope !== 'standard_package') return '当前合同只支持标准包测试；先明确选择标准包作用范围。项目实例试单尚未在此接线。'
  const callback = action === 'adopt' ? props.onAdopt : action === 'configure' ? props.onConfigure
    : action === 'edit' ? props.onEdit : action === 'test' ? props.onTest : props.onUse
  if (!callback) return '此操作尚未接线。'
  if (scope === 'standard_package') {
    if (action !== 'edit' && action !== 'test') return '此操作不能作用于公共标准包。'
    if (props.edit_scope !== 'standard_package') return '先明确选择标准包作用范围；不会默认修改公共包。'
    return pkg.allowed_actions.includes(action) ? '' : '服务端未允许操作此标准包。'
  }
  const project = props.project
  if (!project || !validId(project.project_id)) return '先选择所属项目。'
  if (!validId(project.revision) || project.loaded_revision !== project.revision) return '项目 revision 尚未加载或已变化。'
  if (action === 'adopt') {
    if (!pkg.allowed_actions.includes('adopt') || !project.allowed_actions.includes('adopt')) return '服务端未允许本项目采用此包。'
    return ''
  }
  const instance = selectedInstance(props, pkg)
  if (!instance) return '先明确选择本项目实例；同包可有多个实例，不自动猜选。'
  if (![instance.project_id, instance.instance_id, instance.package_id, instance.package_version].every(validId)) return '实例标识尚未加载。'
  if (!validId(instance.instance_revision) || instance.loaded_revision !== instance.instance_revision) return '实例 revision 尚未加载或已变化。'
  if (!instance.allowed_actions.includes(action)) return '服务端未允许操作此实例。'
  if (action === 'use') return aiEmployeeInstanceReadinessReason(instance, project.project_id)
  return ''
}
export function aiEmployeeLibraryRequest(props: AIEmployeeLibraryProps, pkg: AIEmployeePackageView, action: AIEmployeeLibraryAction, scope: AIEmployeeEditScope = 'project_instance'): AIEmployeeLibraryRequest | null {
  if (aiEmployeeLibraryActionReason(props, pkg, action, scope)) return null
  const request: AIEmployeeLibraryRequest = { action, scope, package: { package_id: pkg.package_id, package_version: pkg.package_version } }
  if (action === 'view') return request
  request.expected_package_revision = pkg.revision!
  if (scope === 'project_instance') {
    request.project_id = props.project!.project_id
    request.expected_project_revision = props.project!.revision!
    if (action !== 'adopt') {
      const instance = selectedInstance(props, pkg)!
      request.instance = aiEmployeeInstanceReference(instance)
      request.expected_instance_revision = instance.instance_revision
    }
  }
  return request
}
export function aiEmployeeLibraryInvoke(props: AIEmployeeLibraryProps, pkg: AIEmployeePackageView, action: AIEmployeeLibraryAction, scope: AIEmployeeEditScope = 'project_instance'): boolean {
  const request = aiEmployeeLibraryRequest(props, pkg, action, scope)
  if (!request) return false
  if (action === 'view') props.onView?.(request.package)
  else if (action === 'adopt') props.onAdopt?.(request)
  else if (action === 'configure') props.onConfigure?.(request)
  else if (action === 'edit') props.onEdit?.(request)
  else if (action === 'test') props.onTest?.(request)
  else props.onUse?.(request)
  return true
}
function list(items: readonly string[]): ReactNode {
  return items.length ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="ai-employee-library__muted">尚未提供</p>
}
function actionButton(props: AIEmployeeLibraryProps, pkg: AIEmployeePackageView, action: AIEmployeeLibraryAction, label: string, scope: AIEmployeeEditScope = 'project_instance') {
  const reason = aiEmployeeLibraryActionReason(props, pkg, action, scope)
  return <span className="ai-employee-library__action" key={`${scope}-${action}`}>
    <button type="button" data-action={action} data-scope={scope} disabled={!!reason} title={reason || undefined}
      onClick={() => aiEmployeeLibraryInvoke(props, pkg, action, scope)}>{label}</button>
    {reason && <small>{reason}</small>}
  </span>
}
function packageVerified(pkg: AIEmployeePackageView): boolean {
  return pkg.verification.state === 'passed' && pkg.verification.version === pkg.package_version
    && !!pkg.revision && pkg.verification.revision === pkg.revision && pkg.loaded_revision === pkg.revision
}
/** Does not load remote catalog data, create an instance or dispatch workflow changes by itself. */
export function AIEmployeeLibrary(props: AIEmployeeLibraryProps) {
  const query = props.query.trim().toLocaleLowerCase()
  const filtered = props.packages.filter(item => `${item.name} ${item.responsibility} ${item.applicable.join(' ')} ${item.package_id}`.toLocaleLowerCase().includes(query))
  const selected = props.packages.filter(item => item.package_id === props.selected_package?.package_id && item.package_version === props.selected_package.package_version)
  const pkg = selected.length === 1 ? selected[0] : null
  const instances = pkg ? aiEmployeeLibraryCandidates(props, pkg) : []
  const instance = pkg ? selectedInstance(props, pkg) : null
  const scope = props.edit_scope ?? 'project_instance'
  return <section className="ai-employee-library" aria-label="AI 员工组件库" data-result-state={props.result.state}>
    <header className="ai-employee-library__header"><div><h2>AI 员工</h2><p className="ai-employee-library__muted">选岗位、配置本项目实例，再加入业务流。标准包通过测试不代表项目实例可用。</p></div>
      <button type="button" disabled={!props.onSelectProject} onClick={() => props.onSelectProject?.()}>{props.project ? `当前项目：${props.project.name}` : '先选择项目'}</button>
    </header>
    <label className="ai-employee-library__search">搜索岗位或能力<input type="search" value={props.query} placeholder="如资料核对、文档交付" disabled={!props.onQueryChange}
      onChange={event => props.onQueryChange?.(event.target.value)} /></label>
    {!props.onQueryChange && <small className="ai-employee-library__muted">搜索尚未接线</small>}
    <p role="status">{stateWords[props.result.state]}{props.result.message ? ` · ${props.result.message}` : ''}</p>
    {props.loading && <p role="status">正在读取员工目录…</p>}
    {props.error && <p role="alert">目录读取失败：{props.error}。保留已加载的介绍；操作暂停。</p>}
    <div className="ai-employee-library__layout">
      <div className="ai-employee-library__list" aria-label="岗位列表">
        {!filtered.length && <p>{props.packages.length ? '没有匹配的岗位；换个关键词。' : '还没有员工包；可通过安安设计岗位，提交测试后再采用。'}</p>}
        {filtered.map(item => {
          const candidates = aiEmployeeLibraryCandidates(props, item)
          return <article key={`${item.package_id}:${item.package_version}`} className="ai-employee-library__card" data-selected={pkg === item}>
            <button className="ai-employee-library__select" type="button" aria-pressed={pkg === item} disabled={!props.onSelectPackage}
              onClick={() => props.onSelectPackage?.({ package_id: item.package_id, package_version: item.package_version })}><strong>{item.name}</strong><span>{item.package_version}</span></button>
            <p>{item.responsibility}</p><p className="ai-employee-library__muted">{packageWords[item.package_status]} · {packageVerified(item) ? '通过所列测试范围' : '测试范围待核实或未通过'}</p>
            <p className="ai-employee-library__muted">{props.project ? `本项目 ${candidates.length} 个实例` : '尚未选择项目'} · 点击查看，不自动运行</p>
          </article>
        })}
      </div>
      <div className="ai-employee-library__detail" aria-label="员工详情">
        {!pkg ? <p>{props.selected_package ? '所选包或版本已不在当前目录，请重新选择。' : '选择一个岗位，查看职责、适用边界和验证依据。'}</p> : <>
          <header><h3>{pkg.name} <small>{pkg.package_version}</small></h3><p>{pkg.responsibility}</p></header>
          <div className="ai-employee-library__states"><span>{packageWords[pkg.package_status]}</span><span>{instance ? `项目采用：${instance.adoption_status === 'adopted' ? '已采用' : '待核实或未采用'}` : '尚未选择项目实例'}</span>
            <span data-ready={!!instance && !aiEmployeeInstanceReadinessReason(instance, props.project?.project_id ?? null)}>{instance && !aiEmployeeInstanceReadinessReason(instance, props.project?.project_id ?? null) ? '当前实例可用' : '当前实例未确认可用'}</span></div>
          <div className="ai-employee-library__facts"><section><h4>需要什么输入</h4>{list(pkg.inputs)}</section><section><h4>交付什么结果</h4>{list(pkg.outputs)}</section>
            <section><h4>适合处理</h4>{list(pkg.applicable)}</section><section><h4>不承担的任务</h4>{list(pkg.excluded)}</section></div>
          <details><summary>方法、Skill、工具与模型</summary><h4>Skill</h4>{list(pkg.skills.map(item => `${item.name} · ${item.id}@${item.version}`))}<h4>工具 / 连接器</h4>{list(pkg.tools.map(item => `${item.name} · ${item.id}@${item.version}`))}<h4>调用模型</h4>{list(pkg.models.map(item => `${item.name} · ${item.purpose}`))}<p className="ai-employee-library__muted">模型只是此岗位的调用选项。能力和权限以实际项目配置为准。</p></details>
          <details><summary>来源、验证范围与费用</summary><p>{pkg.source.label}{pkg.source.license ? ` · ${pkg.source.license}` : ''}</p>{pkg.source.reference && <p className="ai-employee-library__muted">来源参考：{pkg.source.reference}</p>}
            <p>{packageVerified(pkg) ? '当前包通过所列检查范围' : '尚无当前版本与 revision 的完整通过记录'} · {pkg.verification.mode === 'simulation' ? '合同模拟' : pkg.verification.mode === 'provider' ? '模型实跑' : '运行方式待核实'}</p>
            <p>检查范围：{pkg.verification.scope || '尚未提供'}</p><p>格式：{pkg.verification.formats.join('、') || '尚未提供'}；环境：{pkg.verification.environments.join('、') || '尚未提供'}</p>
            <h4>不在此记录覆盖范围内</h4>{list(pkg.verification.unchecked)}<p>{aiEmployeeLibraryCostWords(pkg.cost)}</p></details>
          <section><h4>本项目实例</h4>{!props.project ? <p>先选择项目，再采用、配置或调用；不会把企业知识写进公共包。</p> : !instances.length ? <p>本项目还没有此版本的实例。采用会建立独立的项目配置，是否成功以服务端回执为准。</p> : <>
            <label>选择用途<select value={instance?.instance_id ?? ''} disabled={!props.onSelectInstance} onChange={event => {
              const matching = instances.filter(item => item.instance_id === event.target.value)
              if (matching.length === 1) props.onSelectInstance?.(aiEmployeeInstanceReference(matching[0]))
            }}><option value="">请选择项目实例</option>{instances.map((item, index) => <option key={`${item.instance_id}:${index}`} value={item.instance_id}>{item.name} · {item.purpose}</option>)}</select></label>
            {!props.onSelectInstance && <small>实例选择尚未接线</small>}
            {instance && <div className="ai-employee-library__instance"><p>用途：{instance.purpose} · 负责人：{instance.owner_label || '未指定'}</p><p>知识：{instance.knowledge_status}；软件连接：{instance.connection_status}</p><p>{aiEmployeeInstanceReadinessReason(instance, props.project.project_id) || '服务端确认此 revision 的实例可用；调用仍需选择目标节点。'}</p><small>实例 {instance.instance_id} · revision {instance.instance_revision}</small></div>}
          </>}</section>
          <div className="ai-employee-library__actions">
            {!props.project ? <button type="button" disabled={!props.onSelectProject} onClick={() => props.onSelectProject?.()}>选择项目后采用</button>
              : !instances.length ? actionButton(props, pkg, 'adopt', '采用到本项目')
                : !instance ? <p>先选择项目实例；不会按包名自动分配执行者。</p>
                  : aiEmployeeInstanceReadinessReason(instance, props.project.project_id) ? actionButton(props, pkg, 'configure', '配置项目实例') : actionButton(props, pkg, 'use', '调用到业务流')}
            {actionButton(props, pkg, 'view', '查看来源介绍')}
            <details><summary>编辑与测试</summary><label>作用范围<select value={props.edit_scope ?? ''} disabled={!props.onEditScopeChange} onChange={event => {
              const value = event.target.value
              if (value === 'project_instance' || value === 'standard_package' || value === '') props.onEditScopeChange?.(value || null)
            }}><option value="">默认当前项目实例</option><option value="project_instance">当前项目实例</option><option value="standard_package">标准包草稿（公共包）</option></select></label>
              {scope === 'standard_package' && <p>这是公共标准包编辑，不是本项目配置；不要放入企业私有知识或人员资料。</p>}
              <div className="ai-employee-library__actions">{actionButton(props, pkg, 'edit', '编辑', scope)}{actionButton(props, pkg, 'test', '测试标准包', scope)}</div><p className="ai-employee-library__muted">这里只测试标准包，不测试项目实例；包测试通过不替代实例就绪。测试可能调用付费模型；固定输入、预算与确认在测试编辑器设置，不直接启动。</p></details>
          </div>
        </>}
      </div>
    </div>
  </section>
}
