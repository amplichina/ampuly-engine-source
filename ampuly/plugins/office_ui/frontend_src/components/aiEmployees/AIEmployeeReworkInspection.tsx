import { acceptInspectionAck, buildReworkInspectionRequest,
  type ReworkInspectionRequest } from '../../lib/aiEmployeeReworkInspection'

export interface AIEmployeeReworkInspectionProps {
  request: ReworkInspectionRequest | null
  /** Real {type:'ack', payload:...} envelope, or envelopeFromAckPayload(onAckPayload). */
  ack?: unknown
  state: 'idle' | 'pending' | 'received' | 'unknown'
  connected: boolean
  /** Manual intent only. The host authorizes, creates/correlates a read and sends it. */
  onInspect?: () => void
}

const GAP_WORDS: Record<string, string> = {
  authoritative_attempt_source_unavailable: '尚无权威返工轮次来源，不能冻结待确认轮次。',
  permanent_request_mapping_unavailable: '尚无永久请求对应关系，不能证明原请求已持久登记。',
  rework_record_missing: '当前选定任务没有这项返工记录；不改读其他任务或最新版。',
}
const FIELD_WORDS: Record<string, string> = {
  record_id: '返工记录身份', checkpoint_id: '检查点身份', review_node_id: '审查节点身份',
  node_id: '返工目标节点身份', request_id: '原记录请求身份', payload_sha256: '原请求内容摘要',
  revision_note_hash: '修改意见摘要', attempt: '记录轮次', reviewed_artifact_id: '被审材料身份',
  reviewed_artifact_version: '被审材料版本', reviewed_content_hash: '被审材料内容摘要', dispatched: '派发观察字段',
}
function gapWords(code: string): string {
  if (Object.hasOwn(GAP_WORDS, code)) return GAP_WORDS[code]
  const suffix = code.endsWith('_missing') ? '_missing' : '_invalid'
  const name = code.slice(0, -suffix.length)
  return `${Object.hasOwn(FIELD_WORDS, name) ? FIELD_WORDS[name] : '记录字段'}${suffix === '_missing' ? '缺失' : '格式不符合要求'}；未补造。`
}
const words = (value: string | number | null): string | number => value === null ? '原记录未提供或无效' : value
const hashStyle = { overflowWrap: 'anywhere' as const }

/** Observation-only leaf, deliberately separate from the legacy confirmation card. */
export function AIEmployeeReworkInspection(props: AIEmployeeReworkInspectionProps) {
  let request: ReworkInspectionRequest | null = null
  try { if (props.request) request = buildReworkInspectionRequest(props.request) } catch { /* fixed copy below */ }
  const connected = props.connected === true
  const knownState = ['idle', 'pending', 'received', 'unknown'].includes(props.state)
  const decision = request && connected && props.state === 'received'
    ? acceptInspectionAck(request, props.ack) : null
  const inspection = decision?.kind === 'success' ? decision.inspection : null
  const status = !request ? '当前订单、任务与读取请求尚未固定。'
    : !connected ? '连接已断开；不展示旧读取结果。'
      : !knownState ? '读取状态尚不能核实；不使用旧观察。'
      : props.state === 'pending' ? '正在核对原记录；不展示上一读取结果。'
        : props.state === 'unknown' ? '读取结果未确认；重新核对前不使用旧观察。'
          : decision?.kind === 'failed' ? decision.message
            : decision?.kind === 'invalid' ? '回执字段尚不能核实；未展示原载荷。'
              : decision?.kind === 'ignored' ? '这份回执不属于当前读取；等待当前范围的原记录。'
                : '原返工记录尚未读取。'
  return <section className="employee-rework-card employee-operations" data-testid="ai-employee-rework-inspection">
    <header><h2>核对原返工记录</h2><p>仅观察已选订单任务的原记录，不确认、不重派，也不读取材料正文。</p></header>
    <p className="employee-operations__notice">尚未接通确认重派。记录齐全、摘要或“已记录派发”都不是批准或执行成功证明。</p>
    {inspection ? <>
      <dl className="employee-operations__facts">
        <div><dt>项目 / 订单</dt><dd>{inspection.project_id} / {inspection.order_id}</dd></div>
        <div><dt>订单启动任务</dt><dd>{inspection.order_run_task_id}（不是员工运行编号）</dd></div>
        <div><dt>本次读取请求</dt><dd>{request?.request_id}</dd></div>
        <div><dt>原记录请求</dt><dd>{words(inspection.request_id)}</dd></div>
        <div><dt>返工记录 / 检查点</dt><dd>{words(inspection.record_id)} / {words(inspection.checkpoint_id)}</dd></div>
        <div><dt>审查节点 / 返工目标节点</dt><dd>{words(inspection.review_node_id)} / {words(inspection.node_id)}</dd></div>
        <div><dt>原记录轮次</dt><dd>{words(inspection.record_attempt)}（不是冻结待确认轮次）</dd></div>
        <div><dt>派发观察</dt><dd>{inspection.dispatch_observation === 'dispatch_recorded' ? '原记录记为已派发；未证明派发成功'
          : inspection.dispatch_observation === 'not_dispatched' ? '原记录记为未派发'
            : inspection.dispatch_observation === 'no_record' ? '当前任务没有原返工记录' : '原记录无法确定派发情况'}</dd></div>
      </dl>
      <section className="employee-operations__card"><h3>被审材料锚点</h3>
        {inspection.reviewed_artifact ? <p style={hashStyle}>{inspection.reviewed_artifact.artifact_id} · 原版本 {inspection.reviewed_artifact.version}
          {' · 内容摘要 '}{inspection.reviewed_artifact.content_hash}</p>
          : <p>原材料的身份、版本与内容摘要不完整；未补空材料或改用最新版。</p>}
      </section>
      <p className="employee-operations__muted" style={hashStyle}>原请求内容摘要：{words(inspection.payload_sha256)}<br />修改意见摘要：{words(inspection.revision_note_hash)}</p>
      <section className="employee-operations__card"><h3>仍缺少的依据</h3>
        <ul>{inspection.missing_evidence.map(code => <li key={code}>{gapWords(code)}</li>)}</ul>
      </section>
      <p className="employee-operations__muted" style={hashStyle}>本次公开观察摘要：{inspection.projection_revision}<br />只标识有限投影内容，不是数据库修订、授权或准入证明。</p>
    </> : <p role={decision?.kind === 'failed' || decision?.kind === 'invalid' ? 'alert' : 'status'}>{status}</p>}
    {props.onInspect && <div className="employee-operations__actions"><button type="button" data-action="human_review_rework_inspect"
      disabled={!request || !connected || !knownState || props.state === 'pending'} onClick={() => {
        if (request && connected && knownState && props.state !== 'pending') props.onInspect?.()
      }}>重新核对</button></div>}
  </section>
}
