import { useState } from 'react'
import type { ReactNode } from 'react'
import type { WorkflowSummary } from '../WorkflowsPage'
import type { EmployeeAssetReadView } from '../../lib/aiEmployeeAssetController'

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/
const SHA = /^[a-f0-9]{64}$/
const row = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): string => typeof value === 'string' ? value : ''
export interface StoredBindingTarget {
  project_id: string; workflow_id: string; node_id: string; expected_workflow_revision: string; binding_slot: 'executor'
}
export type StoredBindingSelection = { target: StoredBindingTarget; reason: null } | { target: null;
  reason: 'list_unavailable' | 'scope_unsettled' | 'invalid_list' | 'workflow_missing' | 'revision_missing' | 'node_missing' | 'binding_missing' }

/** Use the successful read's project scope, not optional workflow.project_id.
 * A saved portable binding only makes a read target; it grants no permission.
 */
export function storedBindingSelection(workflows: WorkflowSummary[] | null, replyProjectId: string | null,
  projectId: string | null, workflowId: string, nodeId: string): StoredBindingSelection {
  const absent = (reason: Exclude<StoredBindingSelection['reason'], null>): StoredBindingSelection => ({ target: null, reason })
  if (!projectId || !ID.test(projectId) || replyProjectId !== projectId) return absent('scope_unsettled')
  if (!Array.isArray(workflows)) return absent('list_unavailable')
  if (workflows.length > 500 || workflows.some(flow => !row(flow) || !ID.test(text(flow.id)))
    || new Set(workflows.map(flow => flow.id)).size !== workflows.length) return absent('invalid_list')
  const workflow = workflows.find(flow => flow.id === workflowId)
  if (!workflow) return absent('workflow_missing')
  if (typeof workflow.draft_revision !== 'string' || !SHA.test(workflow.draft_revision)) return absent('revision_missing')
  if (!Array.isArray(workflow.nodes) || workflow.nodes.length > 500
    || workflow.nodes.some(node => !row(node) || !ID.test(text(node.id)))
    || new Set(workflow.nodes.map(node => node.id)).size !== workflow.nodes.length) return absent('invalid_list')
  const node = workflow.nodes.find(item => item.id === nodeId)
  if (!node) return absent('node_missing')
  if (!row(node.employee_binding)) return absent('binding_missing')
  return { reason: null, target: Object.freeze({ project_id: projectId, workflow_id: workflow.id, node_id: node.id,
    expected_workflow_revision: workflow.draft_revision, binding_slot: 'executor' }) }
}
const reasons: Record<Exclude<StoredBindingSelection['reason'], null>, string> = {
  list_unavailable: '已保存流程列表尚未成功读取。', scope_unsettled: '先等本项目的流程列表读取完成。',
  invalid_list: '流程列表字段尚不能核实，请重新读取。', workflow_missing: '选择一条已保存流程。',
  revision_missing: '这条流程尚无可核对的保存版本，请重新读取流程列表。', node_missing: '选择一个已保存节点。',
  binding_missing: '这个已保存节点没有 AI 员工绑定。搭建台中的本地草稿不会代替已保存内容。',
}
export interface AIEmployeeStoredBindingPreviewProps {
  project_id: string | null; connected: boolean; workflows: WorkflowSummary[] | null; workflow_error: string | null
  workflows_read_project_id: string | null; open_builder_workflow_id: string | null; view: EmployeeAssetReadView | undefined
  onPreview: (target: StoredBindingTarget) => void; onSelectionChange: () => void
  selectionSlot?: (target: StoredBindingTarget | null) => ReactNode
}

export function storedBindingReadMatches(target: StoredBindingTarget | null, view: EmployeeAssetReadView | undefined): boolean {
  const request = view?.request
  return !!target && request?.action === 'employee_binding_preview' && request.project_id === target.project_id
    && request.workflow_id === target.workflow_id && request.node_id === target.node_id
    && request.workflow_revision === target.expected_workflow_revision && request.binding_slot === target.binding_slot
}
export function storedBindingReadPending(target: StoredBindingTarget | null, view: EmployeeAssetReadView | undefined): boolean {
  return storedBindingReadMatches(target, view) && view?.state === 'loading'
}
/** Presentation only: never exposes an adopt/launch/rebind affordance. */
export function AIEmployeeBindingResolution({ data }: { data: NonNullable<EmployeeAssetReadView['data']> }) {
  return <div role="status" data-testid="employee-binding-resolution">
    <p>绑定已解析：实例 {text(data.instance_id)} · 实例修订 {String(data.instance_revision)} · 映射修订 {String(data.binding_revision)}</p>
    <p>流程来源：{data.workflow_source === 'runtime' ? '运行目录保存流程' : '分发流程'} · 流程版本：{text(data.workflow_version) || '未提供'}</p>
    <p>解析依据：{data.resolution_basis === 'stored_mapping' ? '已有保存映射' : '唯一候选'}；原来源：{text(data.source)}</p>
    <p>{data.ready === true ? '服务端报告就绪；这仍只是绑定解析，不代表已获准入或可以运行。' : '服务端报告仍有缺口；这只是绑定解析，不代表已获准入或可以运行。'}</p>
    {Array.isArray(data.gaps) && data.gaps.length > 0 && <p>待核对：{data.gaps.filter((gap): gap is string => typeof gap === 'string').join('、')}</p>}
  </div>
}

/** Explicit read only: no socket, polling, writes, instance picker or local draft input. */
export function AIEmployeeStoredBindingPreview({ project_id, connected, workflows, workflow_error,
  workflows_read_project_id, open_builder_workflow_id, view, onPreview, onSelectionChange, selectionSlot }: AIEmployeeStoredBindingPreviewProps) {
  const [selection, setSelection] = useState({ project_id, workflow_id: '', node_id: '' })
  const current = selection.project_id === project_id ? selection : { project_id, workflow_id: '', node_id: '' }
  const listed = !workflow_error && project_id && workflows_read_project_id === project_id && Array.isArray(workflows) && workflows.length <= 500
    ? workflows.filter(flow => row(flow) && ID.test(text(flow.id))) : []
  const workflow = listed.find(flow => flow.id === current.workflow_id)
  const nodes = workflow && Array.isArray(workflow.nodes) && workflow.nodes.length <= 500
    ? workflow.nodes.filter(node => row(node) && ID.test(text(node.id))) : []
  const decision = storedBindingSelection(workflow_error ? null : workflows, workflows_read_project_id,
    project_id, current.workflow_id, current.node_id)
  const target = decision.target
  // Props changes hide an obsolete generation immediately. Do not clear the
  // live controller from a delayed effect: it may already hold a newer read.
  // Gestures clear synchronously; previewBinding supersedes any old request.
  const resultCurrent = connected && storedBindingReadMatches(target, view)
  const data = resultCurrent && view?.state === 'ready' ? view.data : null
  const change = (workflowId: string, nodeId: string): void => {
    onSelectionChange(); setSelection({ project_id, workflow_id: workflowId, node_id: nodeId })
  }
  return <section className="ai-employee-editor__section" aria-label="已保存节点的员工绑定" data-testid="employee-stored-binding-preview">
    <h3>查看已保存节点的员工绑定</h3>
    <p>{selectionSlot ? '先核对已保存节点；可明确选择项目员工并单独保存映射。解析与保存都不代表运行准入。'
      : '只核对保存版本如何对应项目实例；不保存映射，不证明准入，也不会运行。'}</p>
    <p>需要重新读取流程清单时，请使用组件库的刷新入口，再选择保存版本。</p>
    <label className="ai-employee-editor__field">已保存流程<select aria-label="绑定预览流程" value={current.workflow_id}
      disabled={!connected || !project_id || !!workflow_error || workflows_read_project_id !== project_id || !Array.isArray(workflows)}
      onChange={event => change(event.target.value, '')}>
      <option value="">选择流程</option>{listed.map(flow => <option key={flow.id} value={flow.id}>{text(flow.name) || flow.id}</option>)}
    </select></label>
    <label className="ai-employee-editor__field">已保存节点<select aria-label="绑定预览节点" value={current.node_id}
      disabled={!connected || !workflow} onChange={event => change(current.workflow_id, event.target.value)}>
      <option value="">选择节点</option>{nodes.map(node => <option key={node.id} value={node.id}>{text(node.name) || node.id}</option>)}
    </select></label>
    {workflow && <p>保存版本：<code style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>{text(workflow.draft_revision) || '尚未核实'}</code></p>}
    {workflow && open_builder_workflow_id === workflow.id && <p>搭建台正打开这条流程；这里只核对已保存版本，不判断草稿是否改过。</p>}
    {workflow_error ? <p role="alert">流程列表读取失败；不使用旧项目或本地草稿代替。</p>
      : decision.reason && <p role="status">{reasons[decision.reason]}</p>}
    <button type="button" data-action="employee-binding-preview" disabled={!connected || !target || storedBindingReadPending(target, view)}
      onClick={() => { if (connected && target) onPreview(target) }}>解析保存版本</button>
    {resultCurrent && view?.state === 'loading' && <p role="status">正在解析这份已保存绑定。</p>}
    {resultCurrent && (view?.state === 'error' || view?.state === 'stale') && <p role="alert">{view.receipt?.message || '本次绑定读取未核实；请检查权限与保存版本后重新读取，不沿用旧解析。'}</p>}
    {data && <AIEmployeeBindingResolution data={data} />}
    {selectionSlot?.(target)}
  </section>
}
