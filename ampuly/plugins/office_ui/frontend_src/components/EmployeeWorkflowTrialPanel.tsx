import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import {
  EmployeeWorkflowTrialController, employeeWorkflowTrialContextKey, employeeWorkflowTrialDraftError,
  employeeWorkflowTrialEligibility, employeeTrialRecoveryStore, type EmployeeWorkflowTrialAck, type EmployeeWorkflowTrialContext,
  type EmployeeWorkflowTrialSend,
} from '../lib/employeeWorkflowTrial'

export interface EmployeeWorkflowTrialPanelProps extends EmployeeWorkflowTrialContext {
  connected: boolean
  send: EmployeeWorkflowTrialSend
  /** Already validated by the host. Optional scope echoes must be server supplied. */
  ack: EmployeeWorkflowTrialAck | null
  onOpenOrder: (orderId: string) => void
}

/** Additive builder dialog content using existing form classes; no socket, storage or navigation ownership. */
export function EmployeeWorkflowTrialPanel(props: EmployeeWorkflowTrialPanelProps) {
  const sendRef = useRef(props.send)
  sendRef.current = props.send
  const scopeKey = employeeWorkflowTrialContextKey(props)
  // A different project/saved version has no pending request to receive the
  // previous scope's ACK. Never decorate an old ACK with the new scope.
  const controller = useMemo(() => new EmployeeWorkflowTrialController(props,
    (action, payload) => sendRef.current(action, payload), props.connected, {
      recovery: typeof window === 'undefined' ? undefined : {
        load: () => employeeTrialRecoveryStore(window.sessionStorage, props.projectId, props.workflowId).load(),
        save: record => employeeTrialRecoveryStore(window.sessionStorage, props.projectId, props.workflowId).save(record),
        retire: record => employeeTrialRecoveryStore(window.sessionStorage, props.projectId, props.workflowId).retire!(record),
      },
    }), [scopeKey])
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => { controller.setConnected(props.connected) }, [controller, props.connected])
  useEffect(() => { if (props.ack) controller.receive(props.ack) }, [controller, props.ack])
  useEffect(() => {
    if (!state.pending?.waiting) return
    const timer = setTimeout(() => controller.timeout(), Math.max(0, state.pending.deadlineAt - Date.now()))
    return () => clearTimeout(timer)
  }, [controller, state.pending])
  const eligibility = employeeWorkflowTrialEligibility(props)
  const draftError = employeeWorkflowTrialDraftError(props, state)
  const editingLocked = state.startMayHaveBeenSent || !!state.pending?.waiting
  const result = state.result
  const orderId = typeof result?.order_id === 'string' ? result.order_id : ''
  const taskId = typeof result?.launched_task_id === 'string' ? result.launched_task_id : ''
  const showRead = state.phase === 'unknown' || state.phase === 'receipt' || state.phase === 'reading'

  return <section data-testid="employee-workflow-trial-panel" aria-label="员工业务流测试单">
    <p className="settings-note">用已保存的业务流创建一张测试单。原有人工审核保留，环境 ID 不增加权限或替代账号隔离。</p>
    {eligibility && <div className="workflows-hint" role="status">{eligibility}</div>}
    <form className="workflows-form" onSubmit={event => { event.preventDefault(); controller.prepare() }}>
      {props.intakeFields.map(field => <label key={field.id} className="workflows-field">
        <span>{field.label}{field.required ? ' *' : ''}</span>
        {field.multiline
          ? <textarea rows={3} disabled={editingLocked || !!eligibility} value={state.fields[field.id] ?? ''}
            onChange={event => controller.edit({ fields: { ...state.fields, [field.id]: event.target.value } })} />
          : <input type="text" disabled={editingLocked || !!eligibility} value={state.fields[field.id] ?? ''}
            onChange={event => controller.edit({ fields: { ...state.fields, [field.id]: event.target.value } })} />}
      </label>)}
      <label className="workflows-field">
        <span>测试环境 ID *</span>
        <input type="text" placeholder="例如 staging-shop" value={state.environmentId}
          disabled={editingLocked || !!eligibility} onChange={event => controller.edit({ environmentId: event.target.value })} />
      </label>
      <fieldset className="workflows-budget" disabled={editingLocked || !!eligibility}>
        <legend>本次模型调用上限</legend>
        <label className="orders-budget-input">
          <span>最多调用多少次 *</span>
          <input type="text" inputMode="numeric" placeholder="填写正整数" value={state.modelCallLimit}
            onChange={event => controller.edit({ modelCallLimit: event.target.value })} />
        </label>
        <p className="settings-note">创建后可能产生模型费用；调用次数上限不代表费用为 0。</p>
      </fieldset>
      {draftError && !eligibility && !state.startMayHaveBeenSent && <div className="workflows-hint">{draftError}</div>}
      {!props.connected && <div className="workflows-hint">连接已断开，恢复后可查询原请求；不会自动重新发送。</div>}
      {state.message && <p className="workflows-hint" role="status" aria-live="polite">{state.message}</p>}
      <div className="workflows-form-actions">
        <button type="submit" className="workflows-cancel" disabled={!controller.canPrepare()}>
          {state.phase === 'preparing' ? '正在检查条件…' : '检查测试条件'}
        </button>
        <button type="button" className="workflows-invoke" disabled={!controller.canStart()} onClick={() => controller.start()}>
          {state.phase === 'starting' ? '已发出，等待回执…' : '创建测试单（会调用模型）'}
        </button>
        {showRead && <button type="button" className="workflows-cancel" disabled={!controller.canRead()} onClick={() => controller.read()}>
          {state.phase === 'reading' ? '正在查询原测试单…' : '查询原测试单'}
        </button>}
      </div>
    </form>
    {orderId && <div className="workflows-hint" data-testid="employee-trial-order-receipt">
      <p>原测试单：{orderId}</p>
      <p>订单状态：{typeof result?.order_status === 'string' ? result.order_status : '尚未读到'}</p>
      <p>执行任务：{taskId || '尚未确认启动任务'}</p>
      {result?.budget_known === false && <p>预算尚未核实，不按 0 元或免费处理。</p>}
      <p>启动记录不等于执行成功，审核和交付结果以原测试单为准。</p>
      <button type="button" className="workflows-invoke" onClick={() => props.onOpenOrder(orderId)}>查看原测试单</button>
      <button type="button" className="workflows-cancel" onClick={() => controller.newTest()}>已核对原单，明确开始另一轮测试</button>
    </div>}
  </section>
}
