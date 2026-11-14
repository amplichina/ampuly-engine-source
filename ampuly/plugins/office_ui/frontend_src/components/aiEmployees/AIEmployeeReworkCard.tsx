import { employeeOperationBlocksWrite, employeeOperationWords } from './AIEmployeeProjectPanel'
import type { EmployeeOperationReceipt } from './AIEmployeeProjectPanel'
import { employeeArtifactComplete, employeeArtifactEqual, employeeArtifactReadReference, employeeRunCostWords } from './AIEmployeeRunPanel'
import type { EmployeeArtifactReference, EmployeeRunCost } from './AIEmployeeRunPanel'

/** Private confirmation view. Host projections are authoritative; the browser does not establish qualification. */
export interface AIEmployeeReworkView {
  project_id: string
  /** Dedicated authoritative AE00 identities; never derive these from run_id or runtime node_id. */
  order_task_id: string
  review_node_id: string
  target_node_id: string
  rework_record_id: string
  checkpoint_id: string
  order_id: string
  run_id: string
  node_id: string
  expected_attempt: number | null
  revision: string | null
  current_revision: string | null
  reviewer_slot: string | null
  reviewed_artifact: EmployeeArtifactReference | null
  change_request: { change_request_id: string; content_hash: string; text: string } | null
  /** Latest authoritative identity, not a replacement draft or a newer artifact chosen by this card. */
  current: {
    project_id: string | null
    order_task_id: string | null
    review_node_id: string | null
    target_node_id: string | null
    rework_record_id: string | null
    checkpoint_id: string | null
    attempt: number | null
    reviewer_slot: string | null
    reviewed_artifact: EmployeeArtifactReference | null
    change_request_id: string | null
    change_request_hash: string | null
  }
  order_state: 'running' | 'waiting_rework' | 'paused' | 'stopped' | 'completed' | 'cancelled' | 'unknown'
  dispatch_state: 'waiting_confirmation' | 'dispatched' | 'unknown'
  eligibility: { state: 'eligible' | 'ineligible' | 'unknown'; source: string | null; observed_at: string | null; reason: string | null }
  cumulative_limit: { state: 'within_limit' | 'exceeded' | 'unknown'; reason: string | null }
  cumulative_cost: EmployeeRunCost | null
  /** Supplied by the host for this exact action; no machine-inferred final acceptance sentence. */
  consequence: string | null
  allowed_actions: readonly ('confirm_rework' | 'inspect_status' | 'open_reviewed_artifact')[]
}

export interface AIEmployeeReworkIntent {
  action: 'confirm_rework'
  project_id: string
  order_task_id: string
  review_node_id: string
  target_node_id: string
  order_id: string
  run_id: string
  node_id: string
  rework_record_id: string
  checkpoint_id: string
  expected_attempt: number
  expected_revision: string
  reviewer_slot: string
  reviewed_artifact: { artifact_id: string; version: string; content_hash: string }
  change_request: { change_request_id: string; content_hash: string }
}

export interface AIEmployeeReworkStatusQuery {
  project_id: string
  order_task_id: string
  review_node_id: string
  target_node_id: string
  order_id: string
  run_id: string
  rework_record_id: string
  checkpoint_id: string
  /** Preserve the tracked ID. Unknown outcomes must never mint another confirmation request. */
  request_id: string | null
  expected_revision: string | null
  expected_attempt: number | null
  reviewed_artifact: { artifact_id: string; version: string; content_hash: string } | null
  change_request_hash: string | null
  read_only: true
}

export interface AIEmployeeReworkCardProps {
  project_id: string | null
  record: AIEmployeeReworkView | null
  connected: boolean
  busy: boolean
  query_busy: boolean
  receipt: EmployeeOperationReceipt
  onConfirm?: (intent: AIEmployeeReworkIntent) => void
  onInspectStatus?: (query: AIEmployeeReworkStatusQuery) => void
  onOpenReviewedArtifact?: (request: { project_id: string; order_task_id: string; review_node_id: string; target_node_id: string; rework_record_id: string; checkpoint_id: string; artifact: EmployeeArtifactReference; read_only: true }) => void
}

function reworkTargetReason(props: AIEmployeeReworkCardProps): string {
  const record = props.record
  if (!props.project_id?.trim() || !record || record.project_id !== props.project_id) return '返修快照不属于当前项目。'
  if (!record.order_task_id.trim() || !record.review_node_id.trim() || !record.target_node_id.trim()) return '订单任务、审查节点或返修目标节点尚未明确；不能从运行节点推导。'
  return ''
}

export function employeeReworkConfirmReason(props: AIEmployeeReworkCardProps): string {
  const record = props.record
  const targetReason = reworkTargetReason(props)
  if (targetReason) return targetReason
  if (!props.connected) return '连接已断开；先只读核对，不能重复重派。'
  if (employeeOperationBlocksWrite(props.receipt, props.busy)) return '当前请求正在处理或结果待核实；不会重发确认。'
  if (props.receipt.state === 'succeeded') return '服务端已确认此请求，先回查已有重派记录。'
  if (!record || !record.order_id.trim() || !record.run_id.trim() || !record.node_id.trim()
    || !record.rework_record_id.trim() || !record.checkpoint_id.trim()) return '精确返修记录与关口尚未加载。'
  if (!record.allowed_actions.includes('confirm_rework')) return '服务端未允许确认重派。'
  if (!props.onConfirm) return '确认重派入口尚未接线。'
  if (!record.revision?.trim() || !record.current_revision?.trim()) return '返修 revision 尚未加载。'
  if (record.revision !== record.current_revision) return '返修记录已变化，拒绝提交旧 revision。'
  if (!Number.isInteger(record.expected_attempt) || (record.expected_attempt ?? 0) < 1) return '待返修 attempt 尚未明确。'
  if (record.order_state === 'paused' || record.order_state === 'stopped') return '订单已暂停或停止，本次确认不能继续推进。'
  if (record.order_state !== 'running' && record.order_state !== 'waiting_rework') return '订单当前不处于可返修状态。'
  if (record.dispatch_state === 'dispatched') return '服务端已记录重派；先查看已有结果。'
  if (record.dispatch_state !== 'waiting_confirmation') return '是否已派工待核实，不能再次确认。'
  if (record.eligibility.state !== 'eligible' || !record.eligibility.source?.trim() || !record.eligibility.observed_at?.trim()) {
    return record.eligibility.reason || '当前复核资格待核实或不满足。'
  }
  if (record.cumulative_limit.state !== 'within_limit') return record.cumulative_limit.reason || '累计次数与额度资格待核实或已超限。'
  if (!record.reviewer_slot?.trim() || record.reviewer_slot !== record.current.reviewer_slot) return '冻结复核槽位不匹配。'
  if (record.project_id !== record.current.project_id || record.order_task_id !== record.current.order_task_id
    || record.review_node_id !== record.current.review_node_id || record.target_node_id !== record.current.target_node_id) return '订单任务、审查节点或返修目标节点已经变化。'
  if (record.rework_record_id !== record.current.rework_record_id || record.checkpoint_id !== record.current.checkpoint_id
    || record.expected_attempt !== record.current.attempt) return '这份意见属于旧返修记录、关口或 attempt。'
  if (!employeeArtifactEqual(record.reviewed_artifact, record.current.reviewed_artifact)) return '被审材料的 id、版本或 hash 不匹配；旧意见不能作用于新材料。'
  if (!record.change_request?.change_request_id.trim() || !record.change_request.content_hash.trim() || !record.change_request.text.trim()
    || record.change_request.change_request_id !== record.current.change_request_id
    || record.change_request.content_hash !== record.current.change_request_hash) return '修改单或意见 hash 不匹配。'
  if (!record.consequence?.trim()) return '服务端尚未提供本次确认的具体后果。'
  return ''
}

export function employeeReworkIntent(props: AIEmployeeReworkCardProps): AIEmployeeReworkIntent | null {
  if (employeeReworkConfirmReason(props)) return null
  const record = props.record!
  const artifact = record.reviewed_artifact!
  return {
    action: 'confirm_rework', project_id: record.project_id, order_task_id: record.order_task_id,
    review_node_id: record.review_node_id, target_node_id: record.target_node_id,
    order_id: record.order_id, run_id: record.run_id, node_id: record.node_id,
    rework_record_id: record.rework_record_id, checkpoint_id: record.checkpoint_id,
    expected_attempt: record.expected_attempt!, expected_revision: record.revision!, reviewer_slot: record.reviewer_slot!,
    reviewed_artifact: { artifact_id: artifact.artifact_id, version: artifact.version, content_hash: artifact.content_hash },
    change_request: { change_request_id: record.change_request!.change_request_id, content_hash: record.change_request!.content_hash },
  }
}

export function employeeReworkQueryReason(props: AIEmployeeReworkCardProps): string {
  const targetReason = reworkTargetReason(props)
  if (targetReason) return targetReason
  if (!props.connected) return '连接已断开，恢复后只读核对原请求。'
  if (props.query_busy) return '正在回查原请求。'
  const record = props.record
  if (!record || !record.order_id.trim() || !record.run_id.trim() || !record.rework_record_id.trim() || !record.checkpoint_id.trim()) return '原订单或返修记录尚未明确。'
  if (!record.allowed_actions.includes('inspect_status')) return '服务端未允许读取返修状态。'
  return props.onInspectStatus ? '' : '原请求回查入口尚未接线。'
}

export function employeeReworkStatusQuery(props: AIEmployeeReworkCardProps): AIEmployeeReworkStatusQuery | null {
  if (employeeReworkQueryReason(props)) return null
  const record = props.record!
  const artifact = record.reviewed_artifact
  return {
    project_id: record.project_id, order_task_id: record.order_task_id,
    review_node_id: record.review_node_id, target_node_id: record.target_node_id,
    order_id: record.order_id, run_id: record.run_id, rework_record_id: record.rework_record_id,
    checkpoint_id: record.checkpoint_id, request_id: props.receipt.request_id || null,
    expected_revision: record.revision, expected_attempt: record.expected_attempt,
    reviewed_artifact: employeeArtifactComplete(artifact) ? { artifact_id: artifact.artifact_id, version: artifact.version, content_hash: artifact.content_hash } : null,
    change_request_hash: record.change_request?.content_hash || null, read_only: true,
  }
}

function openReason(props: AIEmployeeReworkCardProps): string {
  const targetReason = reworkTargetReason(props)
  if (targetReason) return targetReason
  if (!props.connected || props.query_busy) return '连接或读取状态尚未就绪。'
  if (!props.record?.allowed_actions.includes('open_reviewed_artifact')) return '服务端未允许读取被审材料。'
  if (!props.record.rework_record_id.trim() || !props.record.checkpoint_id.trim()) return '精确返修记录与关口尚未加载。'
  if (!props.onOpenReviewedArtifact) return '被审材料读取入口尚未接线。'
  return employeeArtifactComplete(props.record.reviewed_artifact) && props.record.reviewed_artifact.controlled_reference?.trim()
    ? '' : '被审材料的版本、hash 或受控引用尚未完整。'
}

/** Emits an intent only. The host authenticates, tracks the original request and performs any atomic re-dispatch. */
export function AIEmployeeReworkCard(props: AIEmployeeReworkCardProps) {
  const record = props.record
  if (!record) return <section className="employee-rework-card employee-operations" data-testid="ai-employee-rework-card">
    <h2>人工返修</h2><p>当前没有已加载的精确返修记录。</p></section>
  if (!props.project_id?.trim() || record.project_id !== props.project_id) return <section className="employee-rework-card employee-operations" data-testid="ai-employee-rework-card">
    <h2>人工返修</h2><p role="status">返修快照不属于当前项目；等待当前项目的返修记录。</p></section>
  const confirmReason = employeeReworkConfirmReason(props)
  const queryReason = employeeReworkQueryReason(props)
  const artifactReason = openReason(props)
  const artifact = record.reviewed_artifact
  const orderWords = { running: '运行中', waiting_rework: '等待返修确认', paused: '已暂停', stopped: '已停止', completed: '已完成', cancelled: '已取消', unknown: '待核实' }
  return <section className="employee-rework-card employee-operations" data-testid="ai-employee-rework-card" data-order-state={record.order_state}
    data-result-state={props.receipt.state} data-dispatch-state={record.dispatch_state}>
    <header><h2>人工返修</h2><p>核对被审版本与修改意见，确认意图交由服务端资格、状态和幂等检查。</p></header>
    <dl className="employee-operations__facts">
      <div><dt>返修记录</dt><dd>{record.rework_record_id || '待加载'}</dd></div>
      <div><dt>订单任务</dt><dd>{record.order_task_id || '待加载'}</dd></div>
      <div><dt>审查节点</dt><dd>{record.review_node_id || '待加载'}</dd></div>
      <div><dt>返修目标节点</dt><dd>{record.target_node_id || '待加载'}</dd></div>
      <div><dt>关口</dt><dd>{record.checkpoint_id || '待加载'}</dd></div>
      <div><dt>订单 / 运行 / 节点</dt><dd>{record.order_id || '未知'} / {record.run_id || '未知'} / {record.node_id || '未知'}</dd></div>
      <div><dt>待返修 attempt</dt><dd>{Number.isInteger(record.expected_attempt) && (record.expected_attempt ?? 0) >= 1 ? record.expected_attempt : '待核实'}</dd></div>
      <div><dt>返修 revision</dt><dd>{record.revision || '待加载'} · 当前 {record.current_revision || '待加载'}</dd></div>
      <div><dt>冻结复核槽位</dt><dd>{record.reviewer_slot || '待核实'}</dd></div>
      <div><dt>订单状态</dt><dd>{orderWords[record.order_state]}</dd></div>
      <div><dt>派工记录</dt><dd>{record.dispatch_state === 'dispatched' ? '服务端已记录重派' : record.dispatch_state === 'waiting_confirmation' ? '等待确认重派' : '是否已派工待核实'}</dd></div>
    </dl>
    <section className="employee-operations__card"><h3>这次被审的材料</h3>
      <p>{artifact?.label || artifact?.artifact_id || '材料待加载'}</p>
      <p>artifact {artifact?.artifact_id || '未知'} · 版本 {artifact?.version || '未知'} · hash {artifact?.content_hash || '未知'}</p>
      <button type="button" data-action="open_reviewed_artifact" disabled={!!artifactReason} title={artifactReason || undefined} onClick={() => {
        if (!openReason(props) && artifact) props.onOpenReviewedArtifact?.({ project_id: record.project_id,
          order_task_id: record.order_task_id, review_node_id: record.review_node_id, target_node_id: record.target_node_id,
          rework_record_id: record.rework_record_id,
          checkpoint_id: record.checkpoint_id, artifact: employeeArtifactReadReference(artifact), read_only: true })
      }}>查看被审的这个版本</button>{artifactReason && <p className="employee-operations__muted">{artifactReason}</p>}
    </section>
    <section className="employee-operations__card"><h3>修改意见</h3>
      <p>修改单 {record.change_request?.change_request_id || '未知'} · 意见 hash {record.change_request?.content_hash || '未知'}</p>
      <p className="employee-rework-card__instructions">{record.change_request?.text || '意见正文尚未加载'}</p>
    </section>
    <p>复核资格：{record.eligibility.state === 'eligible' && record.eligibility.source?.trim() && record.eligibility.observed_at?.trim()
      ? '服务端记录资格满足' : record.eligibility.state === 'ineligible' ? '资格不满足' : '待核实'}
      · 来源：{record.eligibility.source || '未提供'} · 时间：{record.eligibility.observed_at || '未提供'}</p>
    {record.eligibility.reason && <p>{record.eligibility.reason}</p>}
    <p>累计限制：{record.cumulative_limit.state === 'within_limit' ? '服务端记录在累计范围内' : record.cumulative_limit.state === 'exceeded' ? '已超出累计范围' : '待核实'}
      {record.cumulative_limit.reason ? ` · ${record.cumulative_limit.reason}` : ''}</p>
    <p>已记录累计费用：{employeeRunCostWords(record.cumulative_cost)}</p>
    <p className="employee-operations__muted">费用沿用累计账目，确认意图不重置计账。</p>
    <p className="employee-operations__notice">本次确认的后果：{record.consequence || '服务端尚未提供。'}</p>
    {(record.order_state === 'paused' || record.order_state === 'stopped') && <p role="status">订单已暂停或停止。已收到的意见可以保留记录，本卡不会恢复执行或继续推进。</p>}
    <p role={['failed', 'unknown', 'partial'].includes(props.receipt.state) ? 'alert' : 'status'}>
      {employeeOperationWords[props.receipt.state]}{props.receipt.message ? `：${props.receipt.message}` : ''}</p>
    {props.receipt.request_id && <p className="employee-operations__muted">原请求：{props.receipt.request_id}</p>}
    {(props.receipt.state === 'unknown' || props.receipt.state === 'partial') && <p>回查原请求与返修记录，不另发确认或重派。</p>}
    <div className="employee-operations__actions">
      <div><button type="button" data-action="confirm_rework" disabled={!!confirmReason} title={confirmReason || undefined} onClick={() => {
        const intent = employeeReworkIntent(props)
        if (intent) props.onConfirm?.(intent)
      }}>确认重派这次返修</button>{confirmReason && <p className="employee-operations__muted">{confirmReason}</p>}</div>
      <div><button type="button" data-action="inspect_status" disabled={!!queryReason} title={queryReason || undefined} onClick={() => {
        const query = employeeReworkStatusQuery(props)
        if (query) props.onInspectStatus?.(query)
      }}>只读回查原请求</button>{queryReason && <p className="employee-operations__muted">{queryReason}</p>}</div>
    </div>
  </section>
}
