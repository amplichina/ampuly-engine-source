import {
  EmployeeIOSchemaError, employeeIOSchemaProblems, employeeIOSchemaState,
  type EmployeeIOSchemaField, type EmployeeIOSchemas, type EmployeeIOSchemaState,
} from '../../lib/aiEmployeeIOSchema'

export interface EmployeeIOSchemaEditorProps {
  value: EmployeeIOSchemas
  disabled: boolean
  onChange(field: EmployeeIOSchemaField, state: EmployeeIOSchemaState): void
  onReset(field: EmployeeIOSchemaField): void
}
/** Controlled draft leaf: no hooks, IO, automatic defaults or save effects. */
export function EmployeeIOSchemaEditor({ value, disabled, onChange, onReset }: EmployeeIOSchemaEditorProps) {
  const problems = employeeIOSchemaProblems(value)
  const emit = (field: EmployeeIOSchemaField, text: string): void => {
    if (disabled) return
    let next: EmployeeIOSchemaState
    try { next = employeeIOSchemaState(text) } catch (error) {
      if (error instanceof EmployeeIOSchemaError) return
      throw error
    }
    onChange(field, next)
  }
  const section = (field: EmployeeIOSchemaField, label: string) => <fieldset key={field}
    className="ai-employee-editor__section" disabled={disabled} aria-label={label}>
    <legend>{label}</legend>
    {value[field].kind === 'missing' ? <p className="ai-employee-editor__muted">还没有填写。直接输入后才会加入草稿，不会自动保存。</p> : null}
    {value[field].kind === 'invalid' && value[field].text === '' ? <p role="alert">原有内容暂时无法完整显示；重新填写并保存后将替换原来的内容。</p> : null}
    <label className="ai-employee-editor__field">{label}内容
      <textarea aria-label={label + '内容'} rows={8} maxLength={131072} value={value[field].text} disabled={disabled}
        onChange={event => emit(field, event.currentTarget.value)} />
    </label>
    {problems.filter(problem => problem.path === field).map((problem, index) => <p key={index}
      className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'}>{problem.message}</p>)}
    <p className="ai-employee-editor__muted">每项最多 128KiB，内容过多时保留修改前的内容。</p>
    <button type="button" disabled={disabled} aria-label={'恢复' + label + '打开时的内容'}
      onClick={() => { if (!disabled) onReset(field) }}>恢复打开时的内容</button>
  </fieldset>
  return <>
    <p className="ai-employee-editor__muted">这里只编辑草稿配置；保存时还需服务端校验，不代表可以运行。</p>
    {section('input_schema', '输入结构')}
    {section('output_schema', '输出结构')}
  </>
}
