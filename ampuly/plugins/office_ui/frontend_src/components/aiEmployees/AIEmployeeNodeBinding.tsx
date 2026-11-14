import type { ReactNode } from 'react'

/** Private controlled view models. These are not AE00 requests or authorization grants. */
export type EmployeeNodeScope = 'node_override' | 'project_instance'
export type EmployeeNodeAction = 'replace_executor' | 'add_collaborator' | 'add_step' | 'save_configuration' | 'reference_an_an'
export type EmployeeNodeResult = 'idle' | 'pending' | 'received' | 'succeeded' | 'failed' | 'unknown' | 'partial'
export interface EmployeeNodeInstance {
  id: string; project_id: string; revision: string; purpose: string
  employee_name: string; package_id: string; package_version: string; ready: boolean
}
export interface EmployeeNodeReference { id: string; version: string }
export interface EmployeeNodeCatalogEntry extends EmployeeNodeReference { label: string; available: boolean }
export interface EmployeeNodeMapping { port: string; source_ref: string; version: string; content_hash: string }
export interface EmployeeNodeHumanSlot {
  slot_id: string; employee_id: string; channel_id: string; return_instructions: string
}
export interface EmployeeNodeMethodConfiguration {
  model: EmployeeNodeReference
  skills: EmployeeNodeReference[]; tools: EmployeeNodeReference[]
}
export interface EmployeeNodeConfiguration extends EmployeeNodeMethodConfiguration {
  owner_id: string; return_to_id: string; reviewer_slots: EmployeeNodeHumanSlot[]
  input_map: EmployeeNodeMapping[]; output_map: EmployeeNodeMapping[]
}
export interface EmployeeNodeContextReference {
  project_id: string; workflow_id: string; node_id: string; node_revision: string
  artifact_id?: string; version?: string; content_hash?: string
}
export interface EmployeeNodeRequest {
  action: Exclude<EmployeeNodeAction, 'reference_an_an'>
  scope: EmployeeNodeScope
  project_id: string; workflow_id: string; node_id: string | null
  expected_workflow_revision: string; expected_node_revision: string | null
  expected_binding_revision: string; expected_instance_revision: string
  instance_id: string; package_id: string; package_version: string
  collaborator_slot_id?: string; new_step_label?: string; configuration?: EmployeeNodeMethodConfiguration | EmployeeNodeConfiguration
}
export interface EmployeeNodeAnAnRequest {
  project_id: string; workflow_id: string; node_id: string; node_revision: string
  instance_id: string; instance_revision: string; package_id: string; package_version: string
  upstream: EmployeeNodeContextReference[]; downstream: EmployeeNodeContextReference[]
  /** Referencing context is neither publication nor permission to execute a suggested change. */
  suggestion_only: true
}
export interface AIEmployeeNodeBindingProps {
  project_id: string; workflow_id: string
  expected_workflow_revision: string | null; current_workflow_revision: string | null
  node: { id: string; name: string; bound_instance_id: string | null; expected_revision: string | null; current_revision: string | null } | null
  expected_binding_revision: string | null; current_binding_revision: string | null
  selected_instance_id: string | null; instances: readonly EmployeeNodeInstance[]
  expected_instance_revision: string | null; current_instance_revision: string | null
  /** Empty and existing-node drops require different explicit confirmations. */
  drop: { placement: 'empty' | 'node'; candidate_instance_ids: readonly string[]; new_step_label: string; collaborator_supported: boolean; collaborator_slot_id: string | null } | null
  scope: EmployeeNodeScope | null; allowed_scopes: readonly EmployeeNodeScope[]
  connected: boolean; busy: boolean; dirty: boolean; allowed_actions: readonly EmployeeNodeAction[]
  result: { state: EmployeeNodeResult; message?: string }
  draft: EmployeeNodeConfiguration
  /** Frozen host baseline: local editing may not silently remove declared review seats. */
  required_reviewer_slot_ids: readonly string[]
  catalogs: {
    models: readonly EmployeeNodeCatalogEntry[]; skills: readonly EmployeeNodeCatalogEntry[]; tools: readonly EmployeeNodeCatalogEntry[]
    people: readonly { id: string; label: string; available: boolean }[]
    channels: readonly { id: string; label: string; available: boolean }[]
    mapping_sources: readonly { id: string; project_id: string; version: string; content_hash: string; available: boolean }[]
  }
  upstream: readonly EmployeeNodeContextReference[]; downstream: readonly EmployeeNodeContextReference[]
  onSelectInstance?: (reference: { project_id: string; instance_id: string; revision: string; preserve_draft: true }) => void
  onScopeChange?: (request: { scope: EmployeeNodeScope | null; preserve_draft: true }) => void
  onChange?: (configuration: EmployeeNodeConfiguration) => void
  onCommit?: (request: EmployeeNodeRequest) => void
  onReferenceAnAn?: (request: EmployeeNodeAnAnRequest) => void
  onOpenReference?: (reference: { kind: 'skill' | 'tool'; id: string; version: string }) => void
  onReload?: (request: { project_id: string; workflow_id: string; node_id: string | null; instance_id: string | null; preserve_draft: true }) => void
}

const words: Record<EmployeeNodeResult, string> = {
  idle: '尚无提交结果', pending: '正在发送，等待回执', received: '请求已收到，等待处理结果',
  succeeded: '服务端已确认完成', failed: '操作失败', unknown: '结果未知，先核实，不重复提交', partial: '只完成一部分，先核实差异',
}
const nonempty = (value: string | null | undefined) => typeof value === 'string' && value.trim().length > 0
const pairCurrent = (expected: string | null, current: string | null) => nonempty(expected) && expected === current
const blockedResult = (props: AIEmployeeNodeBindingProps) => props.busy || ['pending', 'received', 'unknown', 'partial'].includes(props.result.state)

export function employeeNodeSelectedInstance(props: AIEmployeeNodeBindingProps): EmployeeNodeInstance | null {
  const matches = props.instances.filter(item => item.id === props.selected_instance_id)
  return matches.length === 1 ? matches[0] : null
}
function baseReason(props: AIEmployeeNodeBindingProps): string {
  if (!props.connected) return '连接已断开，暂不操作。'
  if (blockedResult(props)) return words[props.result.state] === words.idle ? '正在处理，等待结果。' : words[props.result.state]
  if (!nonempty(props.project_id) || !nonempty(props.workflow_id)) return '先确定所属项目与业务流。'
  if (!pairCurrent(props.expected_workflow_revision, props.current_workflow_revision)) return '业务流版本未加载或已变化，重新读取并保留草稿。'
  if (!pairCurrent(props.expected_binding_revision, props.current_binding_revision)) return '员工绑定版本未加载或已变化，拒绝覆盖。'
  if (props.node && (!nonempty(props.node.id) || !pairCurrent(props.node.expected_revision, props.node.current_revision))) return '节点版本未加载或已变化，拒绝覆盖。'
  const instance = employeeNodeSelectedInstance(props)
  if (!instance) return '明确选择一个员工实例；同包多个用途不会自动选人。'
  if (instance.project_id !== props.project_id) return '实例不属于当前项目，不可调用。'
  if (!nonempty(instance.id) || !nonempty(instance.purpose) || !nonempty(instance.package_id) || !nonempty(instance.package_version)) return '实例用途或包版本不完整，先配置。'
  if (!pairCurrent(props.expected_instance_revision, props.current_instance_revision) || instance.revision !== props.current_instance_revision) return '实例版本未加载或已变化，重新核对实例。'
  return ''
}
function referenceKnown(reference: EmployeeNodeReference, entries: readonly EmployeeNodeCatalogEntry[]) {
  return nonempty(reference.id) && nonempty(reference.version) && entries.filter(item => item.id === reference.id && item.version === reference.version && item.available).length === 1
}
function mappingsValid(mappings: readonly EmployeeNodeMapping[]) {
  return mappings.length <= 100 && new Set(mappings.map(item => item.port)).size === mappings.length
    && mappings.every(item => nonempty(item.port) && nonempty(item.source_ref) && nonempty(item.version) && nonempty(item.content_hash))
}
function configurationReason(props: AIEmployeeNodeBindingProps): string {
  const config = props.draft
  if (!referenceKnown(config.model, props.catalogs.models)) return '选择有明确版本且可用的模型能力。'
  for (const [references, entries, label] of [[config.skills, props.catalogs.skills, 'Skill'], [config.tools, props.catalogs.tools, '工具']] as const) {
    if (references.length > 100 || new Set(references.map(item => `${item.id}\u0000${item.version}`)).size !== references.length
      || references.some(item => !referenceKnown(item, entries))) return `${label}引用未解析、版本不可用或重复，先核对。`
  }
  if (props.scope === 'node_override') {
    const personKnown = (id: string) => nonempty(id) && props.catalogs.people.filter(item => item.id === id && item.available).length === 1
    const channelKnown = (id: string) => nonempty(id) && props.catalogs.channels.filter(item => item.id === id && item.available).length === 1
    if (!personKnown(config.owner_id) || !personKnown(config.return_to_id)) return '配置在岗的真人负责人和打回接收人；AI 执行者不能替代人审。'
    if (props.required_reviewer_slot_ids.length > 100 || props.required_reviewer_slot_ids.some(item => !nonempty(item))
      || new Set(props.required_reviewer_slot_ids).size !== props.required_reviewer_slot_ids.length
      || props.required_reviewer_slot_ids.some(id => !config.reviewer_slots.some(item => item.slot_id === id))) return '既有人工审核席位不可被员工配置移除。'
    if (config.reviewer_slots.length > 100 || new Set(config.reviewer_slots.map(item => item.slot_id)).size !== config.reviewer_slots.length
      || config.reviewer_slots.some(item => !nonempty(item.slot_id) || !personKnown(item.employee_id) || !channelKnown(item.channel_id) || !nonempty(item.return_instructions))) return '人工审核槽位、人员、交互渠道或回转意见不完整。'
    if (!mappingsValid(config.input_map) || !mappingsValid(config.output_map)) return '输入输出映射需要唯一端口及明确来源、版本和哈希。'
    if ([...config.input_map, ...config.output_map].some(mapping => props.catalogs.mapping_sources.filter(source =>
      source.id === mapping.source_ref && source.project_id === props.project_id && source.version === mapping.version
      && source.content_hash === mapping.content_hash && source.available).length !== 1)) return '映射来源不在当前项目的受控引用目录，先核对。'
  }
  return ''
}
function contextValid(props: AIEmployeeNodeBindingProps): boolean {
  return [...props.upstream, ...props.downstream].length <= 100 && [...props.upstream, ...props.downstream].every(item =>
    item.project_id === props.project_id && item.workflow_id === props.workflow_id && nonempty(item.node_id) && nonempty(item.node_revision)
    && (!item.artifact_id || (nonempty(item.version) && nonempty(item.content_hash))))
}
export function employeeNodeActionReason(props: AIEmployeeNodeBindingProps, action: EmployeeNodeAction): string {
  const common = baseReason(props)
  if (common) return common
  if (!props.allowed_actions.includes(action)) return '服务端未允许此操作。'
  if (action === 'reference_an_an') {
    if (!props.onReferenceAnAn) return '安安引用尚未接线。'
    if (!props.node || props.node.bound_instance_id !== props.selected_instance_id) return '先确认并保存节点执行实例，再引用给安安。'
    if (!contextValid(props)) return '上下游引用跨项目或缺少版本依据，先核对。'
    return ''
  }
  if (!props.onCommit) return '保存与绑定尚未接线。'
  if (!props.scope || !props.allowed_scopes.includes(props.scope)) return '先明确保存作用范围，不默认修改公共包。'
  if (action === 'save_configuration') {
    if (!props.dirty) return '没有未保存改动。'
    if (props.scope === 'node_override' && (!props.node || props.node.bound_instance_id !== props.selected_instance_id)) return '先确认执行实例绑定，再保存本节点配置。'
    return configurationReason(props)
  }
  if (props.scope !== 'node_override') return '步骤增改与员工绑定仅作用于本节点，不修改项目实例方法。'
  if (!props.drop) return '先从组件面板明确选择或拖入员工实例。'
  if (!props.drop.candidate_instance_ids.includes(props.selected_instance_id!)) return '选择拖入候选中的明确实例。'
  if (!employeeNodeSelectedInstance(props)?.ready) return '此实例尚未就绪，先在项目中补齐配置。'
  if (props.drop.placement === 'empty' && props.node) return '空白新增步骤与已有节点目标不一致。'
  if (props.drop.placement === 'node' && !props.node) return '已有节点目标尚未加载。'
  if (action === 'add_step') return nonempty(props.drop.new_step_label) ? '' : '填写新增步骤名称。'
  if (!props.node || props.drop.placement !== 'node') return '此动作只用于已有节点；空白处应新增步骤。'
  if (action === 'add_collaborator') {
    if (!props.drop.collaborator_supported) return '协作者席位尚未接入，不会退成一段提示词。'
    if (!nonempty(props.drop.collaborator_slot_id)) return '协作者需要独立且稳定的席位标识。'
  }
  return ''
}

function safeMapping(value: EmployeeNodeMapping): EmployeeNodeMapping {
  return { port: value.port, source_ref: value.source_ref, version: value.version, content_hash: value.content_hash }
}
function safeConfiguration(props: AIEmployeeNodeBindingProps): EmployeeNodeMethodConfiguration | EmployeeNodeConfiguration {
  const value = props.draft
  const reference = (item: EmployeeNodeReference) => ({ id: item.id, version: item.version })
  const method = { model: reference(value.model), skills: value.skills.map(reference), tools: value.tools.map(reference) }
  // Omit, rather than blank, human approvals and mappings when editing project-instance methods.
  if (props.scope === 'project_instance') return method
  return { ...method, owner_id: value.owner_id, return_to_id: value.return_to_id,
    reviewer_slots: value.reviewer_slots.map(item => ({ slot_id: item.slot_id, employee_id: item.employee_id, channel_id: item.channel_id, return_instructions: item.return_instructions })),
    input_map: value.input_map.map(safeMapping), output_map: value.output_map.map(safeMapping) }
}
export function employeeNodeRequest(props: AIEmployeeNodeBindingProps, action: Exclude<EmployeeNodeAction, 'reference_an_an'>): EmployeeNodeRequest | null {
  if (employeeNodeActionReason(props, action)) return null
  const instance = employeeNodeSelectedInstance(props)!
  return {
    action, scope: props.scope!, project_id: props.project_id, workflow_id: props.workflow_id, node_id: props.node?.id ?? null,
    expected_workflow_revision: props.expected_workflow_revision!, expected_node_revision: props.node?.expected_revision ?? null,
    expected_binding_revision: props.expected_binding_revision!, expected_instance_revision: props.expected_instance_revision!,
    instance_id: instance.id, package_id: instance.package_id, package_version: instance.package_version,
    ...(action === 'add_collaborator' ? { collaborator_slot_id: props.drop!.collaborator_slot_id! } : {}),
    ...(action === 'add_step' ? { new_step_label: props.drop!.new_step_label } : {}),
    ...(action === 'save_configuration' ? { configuration: safeConfiguration(props) } : {}),
  }
}
export function employeeNodeAnAnRequest(props: AIEmployeeNodeBindingProps): EmployeeNodeAnAnRequest | null {
  if (employeeNodeActionReason(props, 'reference_an_an')) return null
  const instance = employeeNodeSelectedInstance(props)!
  const reference = (item: EmployeeNodeContextReference): EmployeeNodeContextReference => ({
    project_id: item.project_id, workflow_id: item.workflow_id, node_id: item.node_id, node_revision: item.node_revision,
    ...(item.artifact_id ? { artifact_id: item.artifact_id, version: item.version, content_hash: item.content_hash } : {}),
  })
  return { project_id: props.project_id, workflow_id: props.workflow_id, node_id: props.node!.id, node_revision: props.node!.current_revision!,
    instance_id: instance.id, instance_revision: instance.revision, package_id: instance.package_id, package_version: instance.package_version,
    upstream: props.upstream.map(reference), downstream: props.downstream.map(reference), suggestion_only: true }
}
export function employeeNodeCardSummary(value: { employee_name: string; package_version: string; instance_purpose: string; human_owner_label: string; human_review_label: string }) {
  return { description: `${value.employee_name} · ${value.package_version}`, responsible: `${value.instance_purpose} · 真人负责：${value.human_owner_label}`, review: value.human_review_label }
}
/** Explicit allowlist projection, not a sanitizer for arbitrary public packages. Instance IDs/permissions stay out. */
export function employeeNodePortableBinding(value: { package_id: string; package_version: string; executor_slot: string; collaborator_slots: readonly string[]; reviewer_slots: readonly string[] }) {
  const slots = (values: readonly string[]) => [...new Set(values.filter(nonempty))]
  if (!nonempty(value.package_id) || !nonempty(value.package_version) || !nonempty(value.executor_slot)
    || value.collaborator_slots.length > 100 || value.reviewer_slots.length > 100) return null
  return { package_id: value.package_id, package_version: value.package_version, executor_slot: value.executor_slot,
    collaborator_slots: slots(value.collaborator_slots), reviewer_slots: slots(value.reviewer_slots), requires_project_configuration: true as const }
}

/** Controlled leaf: no drop-payload parsing, network, storage, automatic mutation or simulated success. */
export function AIEmployeeNodeBinding(props: AIEmployeeNodeBindingProps) {
  const selected = employeeNodeSelectedInstance(props)
  const instance = selected?.project_id === props.project_id ? selected : null
  const editBlocked = !!baseReason(props) || !props.onChange || !props.scope || !props.allowed_scopes.includes(props.scope) || !props.allowed_actions.includes('save_configuration')
  const change = (apply: (value: EmployeeNodeConfiguration) => void) => {
    if (editBlocked) return
    const next = structuredClone(props.draft); apply(next); props.onChange?.(next)
  }
  const dispatch = (action: EmployeeNodeAction) => {
    if (action === 'reference_an_an') { const value = employeeNodeAnAnRequest(props); if (value) props.onReferenceAnAn?.(value) }
    else { const value = employeeNodeRequest(props, action); if (value) props.onCommit?.(value) }
  }
  const scopeLocked = !props.connected || blockedResult(props) || !props.onScopeChange
  const selectionLocked = !props.connected || blockedResult(props) || !props.onSelectInstance
  const choices = (label: string, value: string, entries: readonly { id: string; label: string; available: boolean }[], update: (value: string) => void) => <label className="employee-node-binding__field">{label}
    <select aria-label={label} value={value} onChange={event => { if (!editBlocked && entries.filter(item => item.id === event.target.value && item.available).length === 1) update(event.target.value) }}>
      <option value="">请选择</option>{value && !entries.some(item => item.id === value) && <option value={value}>未解析 · {value}</option>}
      {entries.map(item => <option key={item.id} value={item.id} disabled={!item.available}>{item.label}</option>)}
    </select></label>
  const bindings = (kind: 'skill' | 'tool') => {
    const key = kind === 'skill' ? 'skills' : 'tools'; const label = kind === 'skill' ? 'Skill' : '工具'
    return <div className="employee-node-binding__group"><h4>{label}</h4>{props.draft[key].map((ref, index) => <div className="employee-node-binding__row" key={index}>
      <label className="employee-node-binding__field">{label} {index + 1}（含版本）<select aria-label={`${label} ${index + 1}`} value={JSON.stringify([ref.id, ref.version])} onChange={event => {
        if (editBlocked) return
        const entry = props.catalogs[key].find(item => JSON.stringify([item.id, item.version]) === event.target.value && item.available)
        if (entry) change(next => { next[key][index] = { id: entry.id, version: entry.version } })
      }}><option value={JSON.stringify([ref.id, ref.version])}>{ref.id || '请选择'} · {ref.version || '版本待选择'}</option>
        {props.catalogs[key].filter(item => item.id !== ref.id || item.version !== ref.version).map(item => <option key={JSON.stringify([item.id, item.version])} value={JSON.stringify([item.id, item.version])} disabled={!item.available}>{item.label} · {item.version}</option>)}</select></label>
      <button type="button" data-open-reference={`${kind}-${index}`} disabled={!props.connected || blockedResult(props) || !props.onOpenReference || !referenceKnown(ref, props.catalogs[key])} onClick={() => {
        if (props.connected && !blockedResult(props) && props.onOpenReference && referenceKnown(ref, props.catalogs[key])) props.onOpenReference({ kind, id: ref.id, version: ref.version })
      }}>进入{label}编辑器</button>
      <button type="button" disabled={editBlocked} onClick={() => change(next => { next[key].splice(index, 1) })}>移除</button>
    </div>)}<button type="button" disabled={editBlocked} onClick={() => change(next => { next[key].push({ id: '', version: '' }) })}>添加{label}</button></div>
  }
  const mappings = (key: 'input_map' | 'output_map', label: string) => <div className="employee-node-binding__group"><h4>{label}</h4>{props.draft[key].map((mapping, index) => <div className="employee-node-binding__mapping" key={index}>
    {(['port', 'source_ref', 'version', 'content_hash'] as const).map(field => <label className="employee-node-binding__field" key={field}>{`${label} ${index + 1} ${field}`}<input aria-label={`${label} ${index + 1} ${field}`} value={mapping[field]} onChange={event => {
      if (props.scope === 'node_override') change(next => { next[key][index][field] = event.target.value })
    }} /></label>)}<button type="button" disabled={editBlocked} onClick={() => { if (props.scope === 'node_override') change(next => { next[key].splice(index, 1) }) }}>移除映射</button>
  </div>)}<button type="button" disabled={editBlocked} onClick={() => { if (props.scope === 'node_override') change(next => { next[key].push({ port: '', source_ref: '', version: '', content_hash: '' }) }) }}>添加{label}</button></div>
  const action = (name: EmployeeNodeAction, label: string) => { const reason = employeeNodeActionReason(props, name); return <div className="employee-node-binding__action" key={name}>
    <button type="button" data-action={name} disabled={!!reason} title={reason || undefined} onClick={() => dispatch(name)}>{label}</button>{reason && <small>{reason}</small>}</div> }
  const region = (label: string, content: ReactNode) => <fieldset disabled={editBlocked} className="employee-node-binding__region"><legend>{label}</legend>{content}</fieldset>
  const reloadBlocked = !props.connected || props.busy || ['pending', 'received'].includes(props.result.state) || !props.onReload
  return <section className="employee-node-binding" data-testid="employee-node-binding" data-result-state={props.result.state}>
    <header><h3>{props.node ? `${props.node.name} · AI 员工配置` : '新增 AI 员工步骤'}</h3><p>本节点执行者与真人负责人、审核人分开；不会替代人审，也不会修改公共包。</p></header>
    <div className="employee-node-binding__status" role={['unknown', 'partial', 'failed'].includes(props.result.state) ? 'alert' : 'status'}>
      <p>{props.connected ? '已连接' : '连接已断开'} · {props.dirty ? '有未保存草稿' : '无未保存改动'} · {words[props.result.state]}</p>{props.result.message && <p>{props.result.message}</p>}
      <p>业务流 {props.expected_workflow_revision || '待加载'} / 当前 {props.current_workflow_revision || '待加载'}；节点 {props.node?.expected_revision || '新步骤'} / 当前 {props.node?.current_revision || '新步骤'}；绑定 {props.expected_binding_revision || '待加载'} / 当前 {props.current_binding_revision || '待加载'}</p>
      <button type="button" data-action="reload" disabled={reloadBlocked} onClick={() => { if (!reloadBlocked) props.onReload?.({ project_id: props.project_id, workflow_id: props.workflow_id, node_id: props.node?.id ?? null, instance_id: props.selected_instance_id, preserve_draft: true }) }}>重新读取并保留草稿</button>
    </div>
    <label className="employee-node-binding__field">执行实例<select aria-label="执行实例" value={props.selected_instance_id ?? ''} disabled={selectionLocked} onChange={event => {
      if (selectionLocked) return
      const matches = props.instances.filter(item => item.id === event.target.value)
      if (matches.length === 1 && matches[0].project_id === props.project_id && nonempty(matches[0].revision)) props.onSelectInstance?.({ project_id: props.project_id, instance_id: matches[0].id, revision: matches[0].revision, preserve_draft: true })
    }}><option value="">请选择明确实例，不按包名自动选人</option>{props.selected_instance_id && !instance && <option value={props.selected_instance_id}>实例待核对 · {props.selected_instance_id}</option>}
      {props.instances.filter(item => item.project_id === props.project_id).map((item, index) => <option key={`${item.id}-${index}`} value={item.id} disabled={props.instances.filter(other => other.id === item.id).length !== 1}>
        {item.employee_name} · {item.package_version} · {item.purpose} · {item.id} · {item.revision}{item.ready ? '' : '（未就绪）'}
      </option>)}</select></label>
    <p>实例 revision：{props.expected_instance_revision || '待加载'} / 当前 {props.current_instance_revision || '待加载'}。{instance && ` 所属项目 ${instance.project_id}；用途 ${instance.purpose}。`}</p>
    <label className="employee-node-binding__field">保存作用范围<select aria-label="保存作用范围" value={props.scope ?? ''} disabled={scopeLocked} onChange={event => {
      const next = event.target.value || null
      if (!scopeLocked && (next === null || ((next === 'node_override' || next === 'project_instance') && props.allowed_scopes.includes(next)))) props.onScopeChange?.({ scope: next as EmployeeNodeScope | null, preserve_draft: true })
    }}><option value="">请选择</option><option value="node_override" disabled={!props.allowed_scopes.includes('node_override')}>仅本节点覆盖</option><option value="project_instance" disabled={!props.allowed_scopes.includes('project_instance')}>当前项目实例方法（影响使用它的节点）</option></select></label>
    {props.drop && <div className="employee-node-binding__confirmation"><h4>{props.drop.placement === 'empty' ? '拖入空白：新增步骤' : '拖入已有节点：选择如何使用'}</h4>
      <p>{props.drop.new_step_label || '新步骤名称待填写'}；绑定确认不等于派工、授权或开始运行。</p><div className="employee-node-binding__actions">
        {props.drop.placement === 'node' && action('replace_executor', '替换执行员工')}{props.drop.placement === 'node' && action('add_collaborator', '作协作者')}{action('add_step', '新增独立步骤')}
      </div></div>}
    {region('模型与工作能力', <><label className="employee-node-binding__field">模型能力（含版本）<select aria-label="模型能力" value={JSON.stringify([props.draft.model.id, props.draft.model.version])} onChange={event => {
      if (editBlocked) return
      const entry = props.catalogs.models.find(item => JSON.stringify([item.id, item.version]) === event.target.value && item.available)
      if (entry) change(next => { next.model = { id: entry.id, version: entry.version } })
    }}><option value={JSON.stringify([props.draft.model.id, props.draft.model.version])}>{props.draft.model.id || '请选择'} · {props.draft.model.version || '版本待选择'}</option>{props.catalogs.models.filter(item => item.id !== props.draft.model.id || item.version !== props.draft.model.version).map(item => <option key={JSON.stringify([item.id, item.version])} value={JSON.stringify([item.id, item.version])} disabled={!item.available}>{item.label} · {item.version}</option>)}</select></label>
      {bindings('skill')}{bindings('tool')}<p>选择能力不授予权限；真实可用性、预算和重测要求仍由服务端核对。</p></>)}
    {props.scope === 'project_instance' ? <div className="employee-node-binding__group"><p>项目实例方法保存只更改模型、Skill 与工具；本节点真人责权、审核槽位及输入输出映射保持不变。</p>
      <p>真人负责人：{props.catalogs.people.find(item => item.id === props.draft.owner_id)?.label || props.draft.owner_id || '待配置'}；打回接收人：{props.catalogs.people.find(item => item.id === props.draft.return_to_id)?.label || props.draft.return_to_id || '待配置'}</p>
      {props.draft.reviewer_slots.map(slot => <p key={slot.slot_id}>审核席位 {slot.slot_id}：{props.catalogs.people.find(item => item.id === slot.employee_id)?.label || slot.employee_id || '待配置'} · {props.catalogs.channels.find(item => item.id === slot.channel_id)?.label || slot.channel_id || '渠道待配置'}</p>)}
    </div> : region('真人负责、审核与回转', <>
      {choices('真人负责人', props.draft.owner_id, props.catalogs.people, value => { if (props.scope === 'node_override') change(next => { next.owner_id = value }) })}
      {choices('打回后交给谁', props.draft.return_to_id, props.catalogs.people, value => { if (props.scope === 'node_override') change(next => { next.return_to_id = value }) })}
      {props.draft.reviewer_slots.length === 0 && <p>此节点未声明人工审核槽位；是否允许无审核由服务端业务规则判断。</p>}
      {props.draft.reviewer_slots.map((slot, index) => <div key={slot.slot_id} className="employee-node-binding__group"><h4>审核席位 {slot.slot_id}</h4>
        {choices(`审核人 ${slot.slot_id}`, slot.employee_id, props.catalogs.people, value => { if (props.scope === 'node_override') change(next => { next.reviewer_slots[index].employee_id = value }) })}
        {choices(`交互渠道 ${slot.slot_id}`, slot.channel_id, props.catalogs.channels, value => { if (props.scope === 'node_override') change(next => { next.reviewer_slots[index].channel_id = value }) })}
        <label className="employee-node-binding__field">交付与意见回转 {slot.slot_id}<textarea aria-label={`交付与意见回转 ${slot.slot_id}`} rows={3} value={slot.return_instructions} onChange={event => { if (props.scope === 'node_override') change(next => { next.reviewer_slots[index].return_instructions = event.target.value }) }} /></label>
      </div>)}<p>保留全部既有审核槽位；改变接收渠道不改变签字资格，旧意见不确认新材料。</p>
    </>)}
    {props.scope !== 'project_instance' && region('输入与输出映射', <>{mappings('input_map', '输入映射')}{mappings('output_map', '输出映射')}</>)}
    <footer className="employee-node-binding__actions">{action('save_configuration', props.scope === 'project_instance' ? '保存项目实例方法' : '保存本节点配置')}{action('reference_an_an', '引用此节点给安安')}</footer>
    <p>安安引用已保存的当前节点与实例，不发送未保存草稿；只接收当前项目的节点、实例与上下游受控引用，不发送知识正文、凭据、权限或整份业务流。建议经审核后再应用，不自动发布。</p>
  </section>
}
