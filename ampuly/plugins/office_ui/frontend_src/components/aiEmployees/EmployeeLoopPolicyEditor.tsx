import {
  EmployeeLoopPolicyError,
  employeeLoopPolicyProblems,
  employeeLoopPolicyState,
  type EmployeeLoopField,
  type EmployeeLoopPolicyState,
} from '../../lib/aiEmployeeLoopPolicy'

export interface EmployeeLoopPolicyEditorProps {
  value: EmployeeLoopPolicyState
  disabled: boolean
  onChange: (value: EmployeeLoopPolicyState) => void
  onReset: () => void
}

const FIELDS: readonly { key: EmployeeLoopField; label: string; hint: string }[] = [
  { key: 'max_steps', label: '最大执行步数', hint: '整数 1–10000' },
  { key: 'max_model_calls', label: '最大模型调用次数', hint: '整数 1–10000' },
  { key: 'max_wall_clock_s', label: '最长执行时间（秒）', hint: '整数 1–86400' },
  { key: 'max_rework', label: '最大返工次数', hint: '整数 0–100' },
  { key: 'max_retries', label: '最大重试次数', hint: '整数 0–20' },
  { key: 'max_sub_agents', label: '最大子代理数', hint: '可选，整数 0–64' },
  { key: 'max_cost_cny', label: '最大费用（人民币）', hint: '可选，最多 12 位整数和 6 位小数；保留金额原文' },
]
const own = (value: object, key: PropertyKey): boolean => Object.prototype.hasOwnProperty.call(value, key)

export function EmployeeLoopPolicyEditor({ value, disabled, onChange, onReset }: EmployeeLoopPolicyEditorProps) {
  const problems = employeeLoopPolicyProblems(value)
  const emit = (fields: Partial<Record<EmployeeLoopField, string>>) => {
    if (disabled) return
    let next: EmployeeLoopPolicyState
    try {
      next = employeeLoopPolicyState(fields)
    } catch (error) {
      if (error instanceof EmployeeLoopPolicyError) return
      throw error
    }
    // Host-wide budget errors must propagate to the host; they are not leaf parse failures.
    onChange(next)
  }

  return <fieldset className="ai-employee-editor__section" aria-label="自主执行上限" disabled={disabled}>
    <legend>自主执行上限</legend>
    <p className="ai-employee-editor__muted">这里仅保存岗位配置；执行时的强制限额和实际费用需另行核验。</p>
    {value.kind === 'present' ? FIELDS.map(({ key, label, hint }) => {
      const declared = own(value.fields, key)
      const fieldProblems = problems.filter(problem => problem.path === `loop_policy.${key}`)
      return <div className="ai-employee-editor__section" key={key}>
        <label className="ai-employee-editor__field">
          <input type="checkbox" aria-label={`设置${label}`} checked={declared} disabled={disabled} onChange={event => {
            if (disabled || value.kind !== 'present') return
            const fields = { ...value.fields }
            if (event.currentTarget.checked) {
              if (!own(fields, key)) fields[key] = ''
            } else delete fields[key]
            emit(fields)
          }} />
          {label}
        </label>
        <p className="ai-employee-editor__muted">{hint}</p>
        {declared && <input type="text" aria-label={label} inputMode={key === 'max_cost_cny' ? 'decimal' : 'numeric'} maxLength={64} value={value.fields[key] ?? ''} disabled={disabled} onChange={event => {
          if (disabled || value.kind !== 'present' || !own(value.fields, key)) return
          emit({ ...value.fields, [key]: event.currentTarget.value })
        }} />}
        {fieldProblems.map(problem => <p className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'} key={problem.path}>{problem.message}</p>)}
      </div>
    }) : <>
      {problems.map(problem => <p className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'} key={problem.path}>{problem.message}</p>)}
      <button type="button" disabled={disabled} onClick={() => {
        if (!disabled && value.kind !== 'present') emit({})
      }}>{value.kind === 'invalid' ? '重新配置自主执行上限' : '配置自主执行上限'}</button>
    </>}
    <button type="button" disabled={disabled} aria-label="恢复打开时的自主执行上限" onClick={() => {
      if (!disabled) onReset()
    }}>恢复打开时的内容</button>
  </fieldset>
}
