import { useSyncExternalStore } from 'react'
import type { EmployeeInstanceKnowledgeController } from '../../lib/employeeInstanceKnowledgeController'

/** Controlled host leaf. No raw SHA/path field and no automatic source adoption. */
export function EmployeeInstanceKnowledgeEditor({ controller, disabled, onError }: {
  controller: EmployeeInstanceKnowledgeController; disabled: boolean; onError: (message: string) => void
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const current = controller.isCurrent(), pending = state.state === 'loading' || state.state === 'saving'
  const run = (operation: () => void) => { try { operation(); onError('') } catch (error) { onError(error instanceof Error ? error.message : '请先核对当前岗位修订。') } }
  const canChoose = !disabled && current && state.state === 'ready' && !!state.descriptor
  return <details className="ai-employee-editor__section" data-testid="employee-instance-knowledge-editor">
    <summary>本项目知识</summary>
    <p>只读取本项目的共享知识。请选择服务端核实的固定版本；不输入文件路径、不自动采用最新版。</p>
    <p className="ai-employee-editor__muted">不会读取全局或客户专属知识。内容更新后旧版本将暂停使用，需重新选择并评测；保存不改变在途工单的冻结版本。</p>
    <button type="button" disabled={disabled || pending || controller.hasBlockingWrite()} onClick={() => run(() => controller.load())}>读取知识来源</button>
    <button type="button" disabled={disabled || pending || controller.hasBlockingWrite()} onClick={() => run(() => controller.revoke())}>停止此岗位读取共享知识</button>
    {current && state.descriptor && <section aria-label="当前知识来源版本">
      <h4>{state.descriptor.label}</h4><p>{Math.ceil(state.descriptor.content_bytes / 1024)} KiB · 固定版本 {state.descriptor.version.slice(0, 19)}…</p>
      <p>{state.selected_version === null ? '此岗位尚未绑定知识。' : state.selected_version === state.descriptor.version ? '此岗位已选择当前内容版本。' : '来源内容已有变化，此岗位原版本不可继续读取。'}</p>
      <label><input type="radio" name="employee-project-knowledge" checked={state.choice === 'project-memory'} disabled={!canChoose}
        onChange={() => run(() => controller.choose('project-memory'))} />使用这个知识版本</label>
      <label><input type="radio" name="employee-project-knowledge" checked={state.choice === 'none'} disabled={!canChoose}
        onChange={() => run(() => controller.choose('none'))} />此岗位不读取共享知识</label>
      <button type="button" disabled={!canChoose || state.choice === null} onClick={() => run(() => controller.save())}>保存本项目知识</button>
    </section>}
    {!current && state.scope && <p>这是先前岗位的快照，不会用于当前项目；请重新读取。</p>}
    {state.message && <p role={state.state === 'failed' || state.state === 'unknown' ? 'alert' : 'status'}>{state.message}</p>}
    {state.state === 'unknown' && <button type="button" disabled={!current || pending} onClick={() => run(() => controller.recover())}>核对原保存结果</button>}
  </details>
}
