import type { DragEvent } from 'react'
import { aiEmployeeInstanceReadinessReason, aiEmployeeInstanceReference } from './AIEmployeeLibrary'
import type { AIEmployeeInstanceRef, AIEmployeeInstanceView, AIEmployeeRequestState } from './AIEmployeeLibrary'

/** Candidate only: accepting a drop must still open the destination chooser and validate on the server. */
export const AI_EMPLOYEE_CANDIDATE_MIME = 'application/x-ampuly-ai-employee-candidate'
const referenceKeys = ['project_id', 'instance_id', 'instance_revision', 'package_id', 'package_version'] as const
function safeReference(value: unknown): value is AIEmployeeInstanceRef {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const object = value as Record<string, unknown>
  const keys = Object.keys(object)
  return keys.length === referenceKeys.length && keys.every(key => referenceKeys.includes(key as typeof referenceKeys[number]))
    && referenceKeys.every(key => typeof object[key] === 'string' && (object[key] as string).length > 0
      && (object[key] as string).length <= 160 && (object[key] as string).trim() === object[key]
      && !/[\u0000-\u001f\u007f]/.test(object[key] as string))
}
export function encodeAIEmployeeCandidate(reference: AIEmployeeInstanceRef): string | null {
  if (!safeReference(reference)) return null
  // Explicit projection avoids spreading knowledge, credentials, permissions or people into drag data.
  return JSON.stringify({ project_id: reference.project_id, instance_id: reference.instance_id,
    instance_revision: reference.instance_revision, package_id: reference.package_id, package_version: reference.package_version })
}
export function parseAIEmployeeCandidate(payload: string, currentProjectId: string): AIEmployeeInstanceRef | null {
  if (typeof payload !== 'string' || payload.length > 1200 || !currentProjectId) return null
  let value: unknown
  try { value = JSON.parse(payload) } catch { return null }
  // Our encoder writes canonical compact JSON. Reject duplicate/escaped shadow keys rather than normalizing them.
  if (JSON.stringify(value) !== payload) return null
  if (!safeReference(value) || value.project_id !== currentProjectId) return null
  return { project_id: value.project_id, instance_id: value.instance_id, instance_revision: value.instance_revision,
    package_id: value.package_id, package_version: value.package_version }
}
export interface AIEmployeeShelfProps {
  project_id: string | null; project_name?: string
  instances: readonly AIEmployeeInstanceView[]; query: string; selected_instance_id: string | null
  connected: boolean; busy: boolean; result_state: AIEmployeeRequestState
  loading?: boolean; error?: string
  /** Host supplies a target chooser. This does not authorize binding or execution. */
  onUseCandidate?: (reference: AIEmployeeInstanceRef) => void
  onInspect?: (reference: AIEmployeeInstanceRef) => void
  onQueryChange?: (query: string) => void
  onSelectProject?: () => void
  /** Dragging is deliberately disabled until the host installs its safe drop-selection flow. */
  drop_chooser_connected: boolean
  /** Owner-only local design candidates; not an adoption or execution grant. */
  owner_design_mode?: boolean
}
export function aiEmployeeShelfActionReason(props: AIEmployeeShelfProps, instance: AIEmployeeInstanceView): string {
  if (!props.connected) return '连接已断开；恢复后核实实例。'
  if (props.busy || props.result_state === 'pending' || props.result_state === 'received') return '请求正在处理中。'
  if (props.result_state === 'unknown' || props.result_state === 'partial') return '先核实上次操作结果；不会自动重发。'
  if (props.loading || props.error) return '实例尚未完整读取。'
  const matching = props.instances.filter(item => item.project_id === props.project_id && item.instance_id === instance.instance_id)
  if (matching.length !== 1 || matching[0] !== instance) return '实例标识重复或快照已变化，先重新读取。'
  if (!props.onUseCandidate) return '目标选择入口尚未接线。'
  if (props.owner_design_mode) {
    if (!props.project_id || instance.project_id !== props.project_id
        || !encodeAIEmployeeCandidate(aiEmployeeInstanceReference(instance))
        || instance.loaded_revision !== instance.instance_revision) return '实例范围或版本待核实。'
    return ''
  }
  if (!instance.allowed_actions.includes('use')) return '服务端未允许调用此实例。'
  return aiEmployeeInstanceReadinessReason(instance, props.project_id)
}
export function aiEmployeeShelfInspectReason(props: AIEmployeeShelfProps, instance: AIEmployeeInstanceView): string {
  if (!props.connected) return '连接已断开；已加载的实例摘要仍可阅读。'
  if (props.loading || props.error) return '先核实实例读取状态。'
  if (!props.onInspect) return '查看入口尚未接线。'
  if (!props.project_id || instance.project_id !== props.project_id) return '此实例不属于当前项目。'
  const matching = props.instances.filter(item => item.project_id === props.project_id && item.instance_id === instance.instance_id)
  if (matching.length !== 1 || matching[0] !== instance) return '实例标识重复或快照已变化，先重新读取。'
  if (!encodeAIEmployeeCandidate(aiEmployeeInstanceReference(instance)) || instance.loaded_revision !== instance.instance_revision) return '实例标识或 revision 待核实。'
  if (!props.owner_design_mode && !instance.allowed_actions.includes('view')) return '服务端未允许查看此实例。'
  return ''
}
export function inspectAIEmployeeShelfInstance(props: AIEmployeeShelfProps, instance: AIEmployeeInstanceView): boolean {
  if (aiEmployeeShelfInspectReason(props, instance)) return false
  props.onInspect?.(aiEmployeeInstanceReference(instance))
  return true
}
export function invokeAIEmployeeShelfCandidate(props: AIEmployeeShelfProps, instance: AIEmployeeInstanceView): boolean {
  if (aiEmployeeShelfActionReason(props, instance)) return false
  props.onUseCandidate?.(aiEmployeeInstanceReference(instance))
  return true
}
export function startAIEmployeeShelfDrag(props: AIEmployeeShelfProps, instance: AIEmployeeInstanceView, event: Pick<DragEvent, 'dataTransfer' | 'preventDefault'>): boolean {
  if (aiEmployeeShelfActionReason(props, instance) || !props.drop_chooser_connected) { event.preventDefault(); return false }
  const payload = encodeAIEmployeeCandidate(aiEmployeeInstanceReference(instance))
  if (!payload) { event.preventDefault(); return false }
  event.dataTransfer.effectAllowed = 'copy'
  event.dataTransfer.setData(AI_EMPLOYEE_CANDIDATE_MIME, payload)
  return true
}
/** Add inside the existing component sidebar's AI employee tab; leave cards, lanes and canvas layout intact. */
export function AIEmployeeShelf(props: AIEmployeeShelfProps) {
  const query = props.query.trim().toLocaleLowerCase()
  const candidates = props.project_id ? props.instances.filter(item => item.project_id === props.project_id) : []
  const visible = candidates.filter(item => `${item.name} ${item.purpose} ${item.package_id}`.toLocaleLowerCase().includes(query))
  return <section className="ai-employee-shelf" aria-label="搭建台 AI 员工组件">
    <header><h3>AI 员工</h3><p>{props.project_name ? `当前项目：${props.project_name}` : '仅列当前项目的实例'}</p></header>
    {!props.project_id ? <div><p>先选择所属项目，再配置、拖入员工。</p><button type="button" disabled={!props.onSelectProject} onClick={() => props.onSelectProject?.()}>选择项目</button></div> : <>
      <label>搜索岗位或用途<input type="search" value={props.query} disabled={!props.onQueryChange} placeholder="如核对合同、交付周报" onChange={event => props.onQueryChange?.(event.target.value)} /></label>
      {props.loading && <p role="status">正在读取本项目实例…</p>}{props.error && <p role="alert">读取失败：{props.error}</p>}
      {!visible.length && <p>{candidates.length ? '没有匹配的实例。' : '本项目暂无员工实例；先在组件库采用并配置。'}</p>}
      <div className="ai-employee-shelf__list">{visible.map((instance, index) => {
        const reason = aiEmployeeShelfActionReason(props, instance)
        const duplicate = candidates.filter(item => item.instance_id === instance.instance_id).length !== 1
        const readyReason = duplicate ? '实例标识重复，先重新读取。' : aiEmployeeInstanceReadinessReason(instance, props.project_id)
        const inspectReason = aiEmployeeShelfInspectReason(props, instance)
        const dragReason = reason || (!props.drop_chooser_connected ? '画布拖放选择器尚未接线；可先查看或使用调用入口。' : '')
        return <article key={`${instance.instance_id}:${index}`} className="ai-employee-shelf__card" data-ready={!readyReason} data-selected={instance.instance_id === props.selected_instance_id}
          draggable={!dragReason} onDragStart={event => startAIEmployeeShelfDrag(props, instance, event)}>
          <div className="ai-employee-shelf__title"><strong>{instance.name}</strong><span>{readyReason ? '未就绪' : '可用'}</span></div>
          <p>{instance.purpose}</p><small>包 {instance.package_version} · 负责人 {instance.owner_label || '未指定'}</small>
          <p className="ai-employee-shelf__muted">{readyReason || '本项目此 revision 已就绪'}</p>
          <div className="ai-employee-shelf__actions"><button type="button" data-action="inspect" disabled={!!inspectReason} title={inspectReason || undefined}
            onClick={() => inspectAIEmployeeShelfInstance(props, instance)}>查看 / 配置</button>
            <button type="button" data-action="use" disabled={!!reason} title={reason || undefined} onClick={() => invokeAIEmployeeShelfCandidate(props, instance)}>{props.owner_design_mode ? '加入草稿' : '调用'}</button></div>
          {reason && <small>{reason}</small>}<small className="ai-employee-shelf__muted">{dragReason || (props.owner_design_mode ? '加入草稿后配置并测试；拖到节点可选择替换。' : '拖到空白处添加步骤；拖到节点后再选择替换或协作。')}</small>
        </article>
      })}</div>
      <p className="ai-employee-shelf__muted">同包可有多个用途，按实例选择。拖放只携带候选引用，不复制知识或授权，不直接启动任务。</p>
    </>}
  </section>
}
