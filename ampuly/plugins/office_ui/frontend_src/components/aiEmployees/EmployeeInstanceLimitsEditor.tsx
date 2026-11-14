import {
  EmployeeInstanceLimitsError, employeeInstanceLimitsProblems, employeeInstanceLimitsState,
  type EmployeeInstanceLimitField, type EmployeeInstanceLimitsState,
} from '../../lib/aiEmployeeInstanceLimits'

export interface EmployeeInstanceLimitsEditorProps {
  value: EmployeeInstanceLimitsState
  disabled: boolean
  onChange: (value: EmployeeInstanceLimitsState) => void
  onReset: () => void
}
const FIELDS: readonly { key: EmployeeInstanceLimitField; label: string; hint: string }[] = [
  { key: 'max_steps', label: '最大执行步数', hint: '整数 1–10000' },
  { key: 'max_model_calls', label: '最大模型调用次数', hint: '整数 1–10000' },
  { key: 'max_wall_clock_s', label: '最长执行时间（秒）', hint: '整数 1–86400' },
  { key: 'max_rework', label: '最大返工次数', hint: '整数 0–100' },
  { key: 'max_retries', label: '最大重试次数', hint: '整数 0–20' },
  { key: 'max_sub_agents', label: '最大子代理数', hint: '整数 0–64' },
  { key: 'max_cost_cny', label: '最大费用（人民币）', hint: '最多 12 位整数和 6 位小数；金额文本按原样保存' },
]
const own = (value: object, key: PropertyKey) => Object.prototype.hasOwnProperty.call(value, key)

/** Controlled local configuration only. No transport, permission or runtime grant. */
export function EmployeeInstanceLimitsEditor({ value, disabled, onChange, onReset }: EmployeeInstanceLimitsEditorProps) {
  const problems = employeeInstanceLimitsProblems(value)
  const emit = (fields: Partial<Record<EmployeeInstanceLimitField, string>>) => {
    if (disabled) return
    let next: EmployeeInstanceLimitsState
    try { next = employeeInstanceLimitsState(fields) }
    catch (error) { if (error instanceof EmployeeInstanceLimitsError) return; throw error }
    onChange(next)
  }
  return <details className="ai-employee-editor__section" data-testid="employee-instance-limits-editor">
    <summary>本项目执行限额</summary>
    <fieldset className="ai-employee-editor__section" aria-label="本项目执行限额" disabled={disabled}>
      <legend>只调整本项目的限额</legend>
      <p className="ai-employee-editor__muted">本项目只可收紧固定岗位包上限；取消某项设置将继承包与平台限制，不代表免费。这里保存的是配置，实际执行约束和费用需分别核验。</p>
      {value.kind === 'present' ? FIELDS.map(({ key, label, hint }) => {
        const declared = own(value.fields, key)
        const issues = problems.filter(problem => problem.path === `limits.${key}`)
        return <div className="ai-employee-editor__field" key={key}>
          <label><input type="checkbox" aria-label={`设置本项目${label}`} checked={declared} disabled={disabled}
            onChange={event => {
              if (disabled || value.kind !== 'present') return
              const fields = { ...value.fields }
              if (event.currentTarget.checked) { if (!own(fields, key)) fields[key] = '' }
              else delete fields[key]
              emit(fields)
            }} />{label}</label>
          <p className="ai-employee-editor__muted">{declared ? hint : '未单独设置，继承固定岗位包与平台限制。'}</p>
          {declared && <input type="text" aria-label={`本项目${label}`} inputMode={key === 'max_cost_cny' ? 'decimal' : 'numeric'}
            maxLength={64} value={value.fields[key] ?? ''} disabled={disabled} onChange={event => {
              if (disabled || value.kind !== 'present' || !own(value.fields, key)) return
              emit({ ...value.fields, [key]: event.currentTarget.value })
            }} />}
          {issues.map(problem => <p className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'} key={problem.path}>{problem.message}</p>)}
        </div>
      }) : <>
        {problems.map(problem => <p className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'} key={problem.path}>{problem.message}</p>)}
        <button type="button" disabled={disabled} onClick={() => {
          if (!disabled && value.kind !== 'present') emit({})
        }}>{value.kind === 'invalid' ? '重新配置本项目限额' : '设置本项目限额'}</button>
      </>}
      <button type="button" disabled={disabled} aria-label="恢复打开时的本项目限额" onClick={() => {
        if (!disabled) onReset()
      }}>恢复打开时的限额</button>
    </fieldset>
  </details>
}

