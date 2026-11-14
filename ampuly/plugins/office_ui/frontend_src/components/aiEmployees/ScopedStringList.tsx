import {
  EMPLOYEE_SCOPE_MAX_CHARACTERS, EMPLOYEE_SCOPE_MAX_ITEMS, addEmployeeScopeItem, editEmployeeScopeItem,
  employeeScopeList, employeeScopeProblems, moveEmployeeScopeItem, removeEmployeeScopeItem,
} from '../../lib/aiEmployeeScopePatch'
import type { EmployeeFieldProblem, EmployeeScopeField, EmployeeScopeList } from '../../lib/aiEmployeeScopePatch'

export interface ScopedStringListProps {
  field: EmployeeScopeField
  value: EmployeeScopeList
  disabled: boolean
  onChange: (value: EmployeeScopeList) => void
  onReset: () => void
}

/** Controlled rows, no private draft state, defaults, normalization or network. */
export function ScopedStringList({ field, value, disabled, onChange, onReset }: ScopedStringListProps) {
  const label = field === 'scope_in' ? '负责哪些事' : '不负责哪些事'
  const problems = employeeScopeProblems(field, value)
  const issueWords = (item: EmployeeFieldProblem): string => {
    if (item.code === 'employee_package_required') return '这部分还没设置。只保存其他内容时，会继续保留未设置状态。'
    if (item.code === 'employee_package_scope_out_empty') return '可以先保存草稿，之后仍需补充不负责的事。'
    if (item.path === field) return value.kind === 'invalid' ? '这部分需要重新填写，才能保存。' : '至少写一件负责的事。'
    const index = item.path.match(/\[(\d+)\]$/)?.[1]
    const prefix = index === undefined ? '这一项' : `第 ${Number(index) + 1} 项`
    return item.message.includes('完全重复') ? `${prefix}与前面的内容重复，请修改或删除。`
      : `${prefix}的文字有误：不能留空或只填空格，每项最多 ${EMPLOYEE_SCOPE_MAX_CHARACTERS} 个字。`
  }
  const change = (next: () => EmployeeScopeList): void => { if (!disabled) onChange(next()) }
  return <fieldset className="ai-employee-editor__section" disabled={disabled} aria-label={`${label}编辑`} data-scope-field={field}>
    <legend>{label}</legend>
    <p className="ai-employee-editor__muted">最多 {EMPLOYEE_SCOPE_MAX_ITEMS} 项，每项最多 {EMPLOYEE_SCOPE_MAX_CHARACTERS} 个字。可以分行写，顺序和原文会保留；重复内容请修改或删除。</p>
    {value.kind === 'missing' ? <>
      <p role="status">还没有设置。这和设置后留空不同；只改其他内容时，会保持未设置。</p>
      <button type="button" aria-label={`${label}开始填写`} disabled={disabled} onClick={() => change(() => employeeScopeList([]))}>开始填写</button>
    </> : value.kind === 'invalid' ? <>
      <p role="alert">现有内容无法逐项编辑。选择重新填写，会先清空本地这一部分；保存后才会替换原内容。</p>
      <button type="button" aria-label={`${label}重新填写`} disabled={disabled} onClick={() => change(() => employeeScopeList([]))}>重新填写</button>
    </> : <>
      <p role="status">已设置，{value.items.length === 0 ? '目前没有内容' : `共 ${value.items.length} 项`}。</p>
      {value.items.map((item, index) => <section className="ai-employee-editor__row" key={index} data-scope-index={index}>
        <label className="ai-employee-editor__field">{label}第 {index + 1} 项
          <textarea aria-label={`${label}第${index + 1}项`} rows={2} maxLength={EMPLOYEE_SCOPE_MAX_CHARACTERS * 2} value={item}
            disabled={disabled} onChange={event => change(() => editEmployeeScopeItem(value, index, event.target.value))} />
        </label>
        <button type="button" aria-label={`${label}第${index + 1}项上移`} disabled={disabled || index === 0}
          onClick={() => change(() => moveEmployeeScopeItem(value, index, -1))}>上移</button>
        <button type="button" aria-label={`${label}第${index + 1}项下移`} disabled={disabled || index === value.items.length - 1}
          onClick={() => change(() => moveEmployeeScopeItem(value, index, 1))}>下移</button>
        <button type="button" aria-label={`${label}第${index + 1}项删除`} disabled={disabled}
          onClick={() => change(() => removeEmployeeScopeItem(value, index))}>删除</button>
      </section>)}
      <button type="button" aria-label={`${label}添加一项`} disabled={disabled || value.items.length >= EMPLOYEE_SCOPE_MAX_ITEMS}
        onClick={() => change(() => addEmployeeScopeItem(value))}>添加一项</button>
    </>}
    {problems.map((item, index) => <p key={`${item.path}:${index}`} role={item.severity === 'error' ? 'alert' : 'status'}>
      {issueWords(item)}
    </p>)}
    <button type="button" aria-label={`${label}恢复打开时的内容`} disabled={disabled} onClick={() => { if (!disabled) onReset() }}>恢复打开时的内容</button>
    <p className="ai-employee-editor__muted">删掉最后一项只会清空内容，不会取消这项设置。恢复原内容只影响当前编辑，还没保存。</p>
  </fieldset>
}
