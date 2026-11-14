import { useState, useSyncExternalStore } from 'react'
import { EmployeeInstanceAdoptionError, instanceAdoptionKey } from '../../lib/aiEmployeeInstanceAdoptionController'
import type { EmployeeInstanceAdoptionController, InstanceAdoptionCandidate, InstanceAdoptionResult } from '../../lib/aiEmployeeInstanceAdoptionController'

export interface EmployeeInstanceAdoptionPanelProps {
  controller: EmployeeInstanceAdoptionController; project_id: string | null
  create: InstanceAdoptionCandidate | null; adopt: InstanceAdoptionCandidate | null; editable: boolean
  onReadInstance: (instanceId: string) => void
}
function ConfirmAdoption({ controller, target, editable }: { controller: EmployeeInstanceAdoptionController; target: InstanceAdoptionCandidate; editable: boolean }) {
  const [open, setOpen] = useState(false), [consent, setConsent] = useState(false), [name, setName] = useState(''), [note, setNote] = useState(''), [error, setError] = useState('')
  const create = target.kind === 'create'
  return <section className="ai-employee-editor__section" aria-label={create ? '采用岗位到本项目' : '采用项目岗位新版本'}>
    <h3>{create ? '采用到本项目' : '升级本项目岗位'}</h3>
    <p>{target.package_id} · 固定版本 {target.package_version} · 指纹 {target.package_fingerprint}</p>
    {!create && <p>实例 {target.instance_id} · 当前修订 {target.instance_revision}。已查看的差异只用于本次明确目标。</p>}
    <p>{create ? '创建本项目独立岗位，然后在“配置本项目员工”中选择模型、人员与限额。不会自动启用执行。'
      : '采用后保留本项目私有配置，生成新修订；在途订单继续使用原来的固定版本。模型与能力配置需要重新核验。'}</p>
    {!open ? <button type="button" disabled={!editable} onClick={() => { setOpen(true); setConsent(false) }}>{create ? '创建项目岗位…' : '确认升级版本…'}</button>
      : <fieldset disabled={!editable}>
        <legend>{create ? '确认创建项目岗位' : '确认采用指定版本'}</legend>
        {create && <label className="ai-employee-editor__field">本项目岗位名称<input aria-label="采用岗位名称" value={name} maxLength={256} onChange={event => { setName(event.target.value); setConsent(false) }} /></label>}
        <label className="ai-employee-editor__field">采用说明<input aria-label="采用说明" value={note} maxLength={4000} onChange={event => { setNote(event.target.value); setConsent(false) }} /></label>
        <label><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} />
          {create ? '我确认仅创建此项目岗位，不启动执行；未配置的人员、模型与授权仍需补齐。' : '我确认采用上面的固定版本，保留私有配置；不改变在途订单，不自动放行执行。'}</label>
        <button type="button" disabled={!consent || create && !name.trim()} onClick={() => {
          if (!editable || !consent) return
          try { if (create) controller.create(target, name, note); else controller.adopt(target, note); setOpen(false); setConsent(false); setError('') }
          catch (e) { setError(e instanceof EmployeeInstanceAdoptionError ? e.message : '采用尚未核实，保留当前内容。') }
        }}>{create ? '确认创建' : '确认采用'}</button>
        <button type="button" onClick={() => { setOpen(false); setConsent(false); setError('') }}>取消</button>
      </fieldset>}
    {error && <p role="alert">{error}</p>}
  </section>
}

/** Uses the existing editor styles; no new navigation, canvas or execution controls. */
export function EmployeeInstanceAdoptionPanel({ controller, project_id, create, adopt, editable, onReadInstance }: EmployeeInstanceAdoptionPanelProps) {
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot), [error, setError] = useState('')
  const scoped = (c: InstanceAdoptionCandidate | null): c is InstanceAdoptionCandidate => !!c && c.project_id === project_id
  const allowed = editable && snapshot.connected && !controller.hasBlockingWrite()
  const read = (result: InstanceAdoptionResult): void => { try { onReadInstance(result.instance_id); setError('') } catch { setError('请回到原项目，再读取实例就绪检查。') } }
  return <section aria-label="项目岗位采用" data-testid="employee-instance-adoption-panel">
    {scoped(create) && <ConfirmAdoption key={instanceAdoptionKey(create)} controller={controller} target={create} editable={allowed} />}
    {scoped(adopt) && <ConfirmAdoption key={instanceAdoptionKey(adopt)} controller={controller} target={adopt} editable={allowed} />}
    {!scoped(create) && !scoped(adopt) && <p className="ai-employee-library__muted">采用岗位：先查看已发布的固定版本；升级现有岗位：先选择目标版本并比较差异。只有服务端确认的当前发布依据和项目所有者权限才显示确认入口。</p>}
    {!snapshot.connected && <p role="status">连接已断开；保留原采用请求，不自动重发。</p>}
    {snapshot.receipts.map(receipt => <section key={receipt.request_id} className="ai-employee-editor__status" role="status">
      <p>原项目 {receipt.candidate.project_id} · {receipt.candidate.package_id} {receipt.candidate.package_version} · 请求 {receipt.request_id}</p>
      <p>{receipt.message}</p>
      {receipt.state === 'unknown' && <button type="button" disabled={!snapshot.connected || project_id !== receipt.candidate.project_id || receipt.checking}
        onClick={() => { try { controller.lookupOriginal(receipt.request_id); setError('') } catch { setError('只能在原项目连接恢复后，只读核对同一份原请求。') } }}>只读核对原采用请求</button>}
      {receipt.state === 'success' && receipt.result && <button type="button" disabled={!snapshot.connected || project_id !== receipt.candidate.project_id}
        onClick={() => { if (receipt.result) read(receipt.result) }}>读取实例并检查就绪</button>}
    </section>)}
    {error && <p role="alert">{error}</p>}
  </section>
}
