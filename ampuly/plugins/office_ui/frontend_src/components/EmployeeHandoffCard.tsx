import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { useI18n } from '../i18n'
import { projectEmployeeInstance } from '../lib/aiEmployeeAssetAdapter'
import type { EmployeeAssetController } from '../lib/aiEmployeeAssetController'
import {
  handoffRefreshQuery,
  type EmployeeHandoff,
  type EmployeeHandoffState,
} from '../lib/employeeHandoff'

const INSTANCE_ID = /^ei-[a-f0-9]{16}$/

export interface DownstreamEmployeeChoice {
  instance_id: string
  display_name: string
}

/** Project instances first. The producing instance stays selectable when the list has not named it yet. */
export function downstreamChoicesFor(
  handoffInstanceId: string | undefined,
  provided: DownstreamEmployeeChoice[] | undefined,
): DownstreamEmployeeChoice[] {
  const seen = new Set<string>()
  const choices: DownstreamEmployeeChoice[] = []
  const add = (instanceId: string, displayName: string) => {
    if (!INSTANCE_ID.test(instanceId) || seen.has(instanceId)) return
    seen.add(instanceId)
    choices.push({ instance_id: instanceId, display_name: displayName.trim() || instanceId })
  }
  for (const choice of provided ?? []) add(choice.instance_id, choice.display_name)
  if (handoffInstanceId) add(handoffInstanceId, handoffInstanceId)
  return choices
}

export function HandoffContinueControls(props: {
  zh: boolean
  connected: boolean
  handoffId: string
  instruction: string
  onInstruction: (value: string) => void
  choices: DownstreamEmployeeChoice[]
  selectedInstanceId: string
  onSelectInstance: (instanceId: string) => void
  onContinue: (handoffId: string, instruction: string, instanceId: string) => boolean
}) {
  const ready = props.connected && props.choices.some(item => item.instance_id === props.selectedInstanceId)
  return (
    <div className="shell-card-row">
      <label>{props.zh ? '下游员工' : 'Downstream employee'}
        <select
          data-testid="handoff-downstream-instance"
          value={props.selectedInstanceId}
          disabled={!props.connected || props.choices.length === 0}
          onChange={event => props.onSelectInstance(event.target.value)}
        >
          {props.choices.map(choice => (
            <option key={choice.instance_id} value={choice.instance_id}>
              {choice.display_name} · {choice.instance_id}
            </option>
          ))}
        </select>
      </label>
      <input
        aria-label={props.zh ? '下游继续说明' : 'Continue-downstream instruction'}
        placeholder={props.zh ? '下游继续说明（可空，用默认）' : 'Continue instruction (optional)'}
        value={props.instruction}
        onChange={event => props.onInstruction(event.target.value)}
      />
      <button type="button" data-testid="handoff-continue" disabled={!ready}
        onClick={() => {
          if (!ready) return
          const text = props.instruction.trim() || (props.zh ? '按验收成果继续下游' : 'Continue downstream from the accepted handoff')
          props.onContinue(props.handoffId, text, props.selectedInstanceId)
        }}>
        {props.zh ? '下游继续' : 'Continue downstream'}
      </button>
    </div>
  )
}

export function HandoffReworkControls(props: {
  zh: boolean
  connected: boolean
  handoffId: string
  instruction: string
  onInstruction: (value: string) => void
  onRework: (handoffId: string, instruction: string) => boolean
}) {
  return (
    <div className="shell-card-row">
      <input
        aria-label={props.zh ? '返工说明' : 'Rework instruction'}
        placeholder={props.zh ? '返工说明（可空，用默认）' : 'Rework instruction (optional)'}
        value={props.instruction}
        onChange={event => props.onInstruction(event.target.value)}
      />
      <button type="button" data-testid="handoff-rework" disabled={!props.connected}
        onClick={() => {
          if (!props.connected) return
          const text = props.instruction.trim() || (props.zh ? '按退回意见重做' : 'Rework the rejected item')
          props.onRework(props.handoffId, text)
        }}>
        {props.zh ? '发起返工' : 'Start rework'}
      </button>
    </div>
  )
}

export interface EmployeeHandoffCardProps {
  state: EmployeeHandoffState
  connected: boolean
  downstreamInstances?: DownstreamEmployeeChoice[]
  onAsk: (status?: string, offset?: number, taskId?: string) => void
  onDecide: (handoffId: string, decision: 'accept' | 'reject', text: string) => boolean
  onRework?: (handoffId: string, instruction: string) => boolean
  onContinue?: (handoffId: string, instruction: string, instanceId: string) => boolean
  onCheck?: (handoffId: string) => boolean
  onAskNode?: (nodeId: string, status?: string) => void
}

export function EmployeeHandoffCard({
  state, connected, downstreamInstances, onAsk, onDecide, onRework, onContinue, onCheck, onAskNode,
}: EmployeeHandoffCardProps) {
  const { locale } = useI18n()
  const zh = locale !== 'en'
  const [status, setStatus] = useState('')
  const [taskId, setTaskId] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [reason, setReason] = useState<Record<string, string>>({})
  const [picked, setPicked] = useState<Record<string, string>>({})
  const seenRefresh = useRef(state.listRefresh)

  // Ask once when the section opens; the button re-reads.
  useEffect(() => { if (connected) onAsk(undefined, 0) }, [connected])

  // A successful continue bumps listRefresh. Re-read without a status filter so the row shows consumed.
  useEffect(() => {
    if (!connected || state.listRefresh === seenRefresh.current) return
    seenRefresh.current = state.listRefresh
    setStatus('')
    const query = handoffRefreshQuery({ offset: state.offset, taskId })
    if (query) onAsk(query.status, query.offset, query.taskId)
  }, [connected, state.listRefresh, state.offset, taskId, onAsk])

  const title = zh ? '员工交接与验收' : 'Employee handoffs and acceptance'
  return (
    <section className="shell-card" data-testid="employee-handoff-card" aria-label={title}>
      <header className="shell-page-head">
        <h2>{title}</h2>
        <p>{zh
          ? '这里只显示服务端的交接事实。接受要求固定产物仍存在且哈希一致；退回必须写理由，退回不会启动下游。'
          : 'Only the server facts appear here. Accepting requires the fixed artifacts to exist with matching hashes; rejecting needs a reason and starts nothing downstream.'}</p>
      </header>

      <div className="shell-card-row">
        <label>{zh ? '状态过滤' : 'Status filter'}
          <select value={status} onChange={event => { setStatus(event.target.value); onAsk(event.target.value || undefined, 0) }}>
            <option value="">{zh ? '全部' : 'All'}</option>
            <option value="sent">sent</option>
            <option value="accepted">accepted</option>
            <option value="rejected">rejected</option>
            <option value="consumed">consumed</option>
          </select>
        </label>
        <label>{zh ? '按任务过滤' : 'Task filter'}
          <input value={taskId} onChange={event => { setTaskId(event.target.value); onAsk(status || undefined, 0, event.target.value || undefined) }} />
        </label>
        <label>{zh ? '按节点过滤' : 'Node filter'}
          <input data-testid="handoff-node-filter" value={nodeId} onChange={event => {
            const next = event.target.value
            setNodeId(next)
            const named = next.trim()
            if (named && onAskNode) onAskNode(named, status || undefined)
            else onAsk(status || undefined, 0, taskId || undefined)
          }} />
        </label>
        <button type="button" data-testid="handoff-refresh" disabled={!connected}
          onClick={() => onAsk(status || undefined, 0, taskId || undefined)}>
          {zh ? '刷新' : 'Refresh'}
        </button>
        {Object.entries(state.counts).filter(([key]) => key !== 'total').map(([key, value]) => (
          <small key={key} data-testid="handoff-count" data-status={key}>{key}: {value}</small>
        ))}
        <small data-testid="handoff-total">{zh ? '总计' : 'total'}: {state.counts.total ?? state.handoffs.length}</small>
        {state.scanTruncated && <small data-testid="handoff-node-truncated">{zh ? '节点扫描已到上限' : 'Node scan stopped at the cap'}</small>}
        <button type="button" data-testid="handoff-prev" disabled={!connected || state.offset <= 0}
          onClick={() => onAsk(status || undefined, Math.max(0, state.offset - 50), taskId || undefined)}>
          {zh ? '上一页' : 'Previous'}
        </button>
        <button type="button" data-testid="handoff-next" disabled={!connected || !state.hasMore}
          onClick={() => onAsk(status || undefined, state.offset + 50, taskId || undefined)}>
          {zh ? '下一页' : 'Next'}
        </button>
      </div>

      {state.error && <p className="shell-card-error" role="alert" data-testid="handoff-error">{state.error}</p>}
      {state.handoffs.length === 0 && <p className="shell-card-text">{zh ? '还没有交接。' : 'No handoffs yet.'}</p>}
      <ul className="shell-config-list">
        {state.handoffs.map((item: EmployeeHandoff) => {
          const choices = downstreamChoicesFor(item.instance_id, downstreamInstances)
          const preferred = picked[item.handoff_id]
          const selectedInstanceId = preferred && choices.some(choice => choice.instance_id === preferred)
            ? preferred
            : (item.instance_id && choices.some(choice => choice.instance_id === item.instance_id)
              ? item.instance_id
              : choices[0]?.instance_id ?? '')
          return (
          <li key={item.handoff_id} data-testid="handoff-row" data-status={item.status}>
            <div>
              <strong>{item.from_role} → {item.to_role}</strong>
              <small>{item.summary}</small>
              <small>{item.handoff_id} · {item.status} · {zh ? '尝试' : 'attempt'} {item.attempt ?? 1}</small>
              {(item.node_id || item.workflow_id) && <small data-testid="handoff-node">
                {zh ? '节点' : 'node'} {item.node_id || '—'}
                {item.workflow_id ? ` · ${item.workflow_id}` : ''}
                {item.order_id ? ` · ${item.order_id}` : ''}
              </small>}
              <small data-testid="handoff-acceptance">
                {zh ? '验收' : 'acceptance'}: {item.acceptance_state || (item.delivered ? 'accepted' : 'pending')}
                {' · '}{item.delivered ? (zh ? '已交付' : 'delivered') : (zh ? '未交付（完成不等于验收）' : 'not delivered (done is not acceptance)')}
                {item.origin_check?.tool ? ` · ${item.origin_check.tool}` : ''}
                {item.origin_check?.delivery_status ? ` · ${item.origin_check.delivery_status}` : ''}
                {item.origin_check?.check_passed === false ? ` · ${item.origin_check.code || (zh ? '检查未过' : 'check failed')}` : ''}
              </small>
              {item.due_at && <small data-testid="handoff-due">
                {zh ? '期限' : 'due'}: {item.due_at}
                {item.status === 'sent' && new Date(item.due_at).getTime() < Date.now() ? (zh ? ' · 已逾期' : ' · overdue') : ''}
              </small>}
              {(item.package_id || item.package_version) && <small>
                {item.package_id ?? ''} @ {item.package_version ?? '?'} · {zh ? '实例' : 'instance'} {item.instance_id ?? '?'}
                {item.package_fingerprint ? ` · fp ${item.package_fingerprint.slice(0, 12)}` : ''}
              </small>}
              {item.artifacts.length > 0 && <small>
                {zh ? '固定产物' : 'fixed artifacts'}: {item.artifacts.map(ref => String(ref.sha256 ?? '').slice(0, 12)).join(', ')}
                {item.artifacts_verified === false ? (zh ? ' · 未通过核验' : ' · unverified') : ''}
              </small>}
              {item.artifact_materials && item.artifact_materials.length > 0 && <details data-testid="handoff-materials">
                <summary>{zh ? '查看固定成果' : 'View fixed deliverables'}</summary>
                {item.artifact_materials.map((material, index) => <div key={`${material.sha256}-${index}`}>
                  <strong>{material.name || `${zh ? '成果' : 'deliverable'} ${index + 1}`}</strong>
                  <small>{material.sha256.slice(0, 12)}</small>
                  <pre>{material.content}</pre>
                </div>)}
              </details>}
              {item.limits && Object.keys(item.limits).length > 0 && <small>
                {zh ? '本次上限' : 'this run ceilings'}: {Object.entries(item.limits).map(([key, value]) => `${key}=${String(value)}`).join(' · ')}
              </small>}
              {item.budget && Object.keys(item.budget).length > 0 && <small>
                {zh ? '预算' : 'budget'}: {item.budget.state ?? ''} · {zh ? '已用' : 'consumed'} {JSON.stringify(item.budget.consumed ?? {})} · {zh ? '占用' : 'held'} {JSON.stringify(item.budget.held ?? {})}
              </small>}
              {item.decision && <small>{zh ? '验收意见' : 'decision'}: {item.decision} · {item.decided_by ?? ''}</small>}
              <small data-testid="handoff-next">
                {zh ? '下一步' : 'next'}: {
                  item.status === 'sent' ? (zh ? 'owner 验收（服务端重读产物哈希）' : 'owner acceptance (server re-reads hashes)')
                  : item.status === 'accepted' ? (zh ? 'owner 决定下游或返工' : 'owner starts downstream or rework')
                  : item.status === 'rejected' ? (zh ? 'owner 可发起返工' : 'owner may start rework')
                  : item.status === 'consumed' ? (zh ? '下游已消费' : 'consumed downstream')
                  : item.status
                }
              </small>
            </div>
            {item.status === 'accepted' && onContinue && (
              <HandoffContinueControls
                zh={zh}
                connected={connected}
                handoffId={item.handoff_id}
                instruction={reason[item.handoff_id] ?? ''}
                onInstruction={value => setReason({ ...reason, [item.handoff_id]: value })}
                choices={choices}
                selectedInstanceId={selectedInstanceId}
                onSelectInstance={instanceId => setPicked({ ...picked, [item.handoff_id]: instanceId })}
                onContinue={onContinue}
              />
            )}
            {item.status === 'rejected' && onRework && (
              <HandoffReworkControls
                zh={zh}
                connected={connected}
                handoffId={item.handoff_id}
                instruction={reason[item.handoff_id] ?? ''}
                onInstruction={value => setReason({ ...reason, [item.handoff_id]: value })}
                onRework={onRework}
              />
            )}
            {item.status === 'sent' && onCheck && (
              <div className="shell-card-row">
                <button type="button" data-testid="handoff-check" disabled={!connected}
                  onClick={() => onCheck(item.handoff_id)}>
                  {zh ? '核对检查回执' : 'Read the check receipt'}
                </button>
              </div>
            )}
            {item.status === 'sent' && (
              <div className="shell-card-row">
                <input
                  aria-label={zh ? '退回理由或接受说明' : 'Reject reason or accept note'}
                  placeholder={zh ? '退回理由/接受说明' : 'Reject reason / accept note'}
                  value={reason[item.handoff_id] ?? ''}
                  onChange={event => setReason({ ...reason, [item.handoff_id]: event.target.value })}
                />
                <button type="button" data-testid="handoff-accept" disabled={!connected}
                  onClick={() => onDecide(item.handoff_id, 'accept', reason[item.handoff_id] ?? '')}>
                  {zh ? '接受' : 'Accept'}
                </button>
                <button type="button" data-testid="handoff-reject" disabled={!connected || !(reason[item.handoff_id] ?? '').trim()}
                  onClick={() => onDecide(item.handoff_id, 'reject', reason[item.handoff_id] ?? '')}>
                  {zh ? '退回' : 'Reject'}
                </button>
              </div>
            )}
          </li>
          )
        })}
      </ul>
    </section>
  )
}

export function EmployeeHandoffHost({
  controller, projectId, ...card
}: EmployeeHandoffCardProps & { controller: EmployeeAssetController; projectId: string }) {
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    if (!projectId || !snapshot.connected) return
    try {
      if (snapshot.project_id !== projectId) controller.selectProject(projectId)
      const view = controller.getSnapshot().projects[projectId]?.instances
      if (!view || (view.state !== 'ready' && view.state !== 'loading')) controller.loadInstances()
    } catch {
      // The employee shelf reports its own read error. Until a list arrives the
      // picker can still offer the instance named on the handoff.
    }
  }, [controller, projectId, snapshot.connected, snapshot.project_id])
  const view = snapshot.projects[projectId]?.instances
  const rows = view?.state === 'ready' && Array.isArray(view.data?.instances) ? view.data.instances : []
  const downstreamInstances: DownstreamEmployeeChoice[] = []
  for (const raw of rows) {
    try {
      const projected = projectEmployeeInstance(raw, projectId)
      if (projected.instance_id) {
        downstreamInstances.push({ instance_id: projected.instance_id, display_name: projected.name || projected.instance_id })
      }
    } catch {
      // A row the shelf would reject is not offered as a continue target.
    }
  }
  return <EmployeeHandoffCard {...card} downstreamInstances={downstreamInstances} />
}
