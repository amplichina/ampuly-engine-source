import {
  EmployeeInstanceModelConfigError, copyEmployeeInstanceModelEdit, copyEmployeeInstanceModelSnapshot,
  employeeInstanceModelConfigProblems, employeeInstanceModelTier, selectEmployeeInstanceModel,
  type EmployeeInstanceModelEdit, type EmployeeInstanceModelGap, type EmployeeInstanceModelSnapshot,
  type EmployeeInstanceModelTier,
} from '../../lib/aiEmployeeInstanceModelConfig'

export interface EmployeeInstanceModelEditorProps {
  value: EmployeeInstanceModelEdit
  candidates: EmployeeInstanceModelSnapshot | null
  disabled: boolean
  onChange: (value: EmployeeInstanceModelEdit) => void
  onLoad: () => void
  onReset: () => void
}
const GAP_LABELS: Readonly<Record<EmployeeInstanceModelGap, string>> = Object.freeze({
  model_unknown: '目录未识别', model_retired: '已退役', model_overseas_off: '未启用',
  model_not_routable: '没有可用路由', model_needs_params: '需要另行配置参数', model_needs_trial: '需要独立试验',
})

/** Controlled local editor: no client, request, automatic default, save or permission grant. */
export function EmployeeInstanceModelEditor({ value, candidates, disabled, onChange, onLoad, onReset }: EmployeeInstanceModelEditorProps) {
  const current = copyEmployeeInstanceModelEdit(value)
  const directory = candidates === null ? null : copyEmployeeInstanceModelSnapshot(candidates)
  const problems = employeeInstanceModelConfigProblems(current, directory)
  const emit = (prepare: () => EmployeeInstanceModelEdit) => {
    if (disabled) return
    let next: EmployeeInstanceModelEdit
    try { next = prepare() }
    catch (error) { if (error instanceof EmployeeInstanceModelConfigError) return; throw error }
    onChange(next)
  }
  const tierEditable = !disabled && current.selection_source !== null
  return <details className="ai-employee-editor__section" data-testid="employee-instance-model-editor">
    <summary>本项目模型设置</summary>
    <fieldset className="ai-employee-editor__section" aria-label="本项目模型设置" disabled={disabled}>
      <legend>明确选择岗位实例的模型配置</legend>
      <p className="ai-employee-editor__muted">这里只保存模型配置；变更后需要重新评测，不代表模型可用、评测通过或获准运行。实际费用未知。</p>
      {current.config.kind === 'present' && <p className="ai-employee-editor__muted" data-testid="employee-instance-model-current">
        原有或本地配置：供应方 {current.config.fields.provider ?? '未设置'}；模型 {current.config.fields.model ?? '未设置'}；档位 {current.config.fields.tier ?? '未单独设置'}。
      </p>}
      {problems.map(problem => <p className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'} key={problem.path}>{problem.message}</p>)}
      <div className="ai-employee-editor__row">
        <button type="button" disabled={disabled} aria-label="读取本项目模型候选" onClick={() => { if (!disabled) onLoad() }}>读取模型候选</button>
        <button type="button" disabled={disabled} aria-label="恢复打开时的本项目模型配置" onClick={() => { if (!disabled) onReset() }}>恢复打开时的配置</button>
      </div>
      {directory === null ? <p className="ai-employee-editor__muted">尚未读取候选，不会自动选择模型。</p> : <>
        <p className="ai-employee-editor__muted">候选仅为原目录的配置声明，工具与视觉标记不等于已评测。刷新目录不会自动重绑原本地选择。</p>
        <table aria-label="本项目模型候选">
          <thead><tr><th>供应方</th><th>模型</th><th>目录声明</th><th>选择</th></tr></thead>
          <tbody>{directory.candidates.map(row => <tr key={row.model}>
            <td>{row.provider}</td><td>{row.model}</td>
            <td>{row.status === 'vendor_routed_alias' ? '供应方路由别名；' : ''}工具：{row.capabilities.tools ? '声明支持' : '未声明'}；视觉：{row.capabilities.vision ? '声明支持' : '未声明'}；{row.selectable ? '可请求配置' : GAP_LABELS[row.gaps[0]]}</td>
            <td><button type="button" disabled={disabled || !row.selectable} aria-label={`选择模型 ${row.provider} / ${row.model}`}
              onClick={() => {
                if (disabled || !row.selectable) return
                emit(() => selectEmployeeInstanceModel(current, directory, row.provider, row.model))
              }}>选择此模型</button></td>
          </tr>)}</tbody>
        </table>
        {directory.candidates.length === 0 && <p className="ai-employee-editor__muted">本次目录没有候选。</p>}
      </>}
      <label className="ai-employee-editor__field">模型请求档位（可选）
        <select aria-label="本项目模型请求档位" value={current.config.fields.tier ?? ''} disabled={!tierEditable}
          onChange={event => {
            if (!tierEditable) return
            const tier = event.currentTarget.value
            emit(() => employeeInstanceModelTier(current, tier === '' ? null : tier as EmployeeInstanceModelTier))
          }}>
          <option value="">不单独设置档位</option><option value="light">light</option>
          <option value="standard">standard</option><option value="strong">strong</option>
        </select>
      </label>
      <p className="ai-employee-editor__muted">选模型时保留原有档位；请在明确选择后自行调整或移除。档位不推导实际模型、价格或权限。</p>
    </fieldset>
  </details>
}
