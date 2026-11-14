import {
  EmployeeArtifactContractError, employeeArtifactProblems, employeeArtifactState,
  type EmployeeArtifactState, type EmployeeDeliverable,
} from '../../lib/aiEmployeeArtifactContract'

export interface EmployeeArtifactContractEditorProps {
  value: EmployeeArtifactState
  disabled: boolean
  onChange(value: EmployeeArtifactState): void
  onReset(): void
}
/** Controlled leaf only: no local editor state, transport, or save side effects. */
export function EmployeeArtifactContractEditor({ value, disabled, onChange, onReset }: EmployeeArtifactContractEditorProps) {
  const emit = (deliverables: readonly EmployeeDeliverable[], criteria: readonly string[]): void => {
    if (disabled) return
    let next: EmployeeArtifactState
    try { next = employeeArtifactState(deliverables, criteria) } catch (error) {
      if (error instanceof EmployeeArtifactContractError) return // Keep the previous controlled value.
      throw error
    }
    onChange(next)
  }
  const start = (): void => { if (!disabled) emit([{ name: '', format: '', required: false }], ['']) }
  const editDelivery = (index: number, change: Partial<EmployeeDeliverable>): void => {
    if (!disabled && value.kind === 'present') emit(value.deliverables.map((row, here) => here === index ? { ...row, ...change } : row), value.acceptance_criteria)
  }
  const moveDelivery = (index: number, direction: -1 | 1): void => {
    const target = index + direction
    if (disabled || value.kind !== 'present' || target < 0 || target >= value.deliverables.length) return
    const rows = [...value.deliverables]
    ;[rows[index], rows[target]] = [rows[target], rows[index]]
    emit(rows, value.acceptance_criteria)
  }
  const moveCriterion = (index: number, direction: -1 | 1): void => {
    const target = index + direction
    if (disabled || value.kind !== 'present' || target < 0 || target >= value.acceptance_criteria.length) return
    const rows = [...value.acceptance_criteria]
    ;[rows[index], rows[target]] = [rows[target], rows[index]]
    emit(value.deliverables, rows)
  }
  const hints = employeeArtifactProblems(value)
  const messages = (path: string) => hints.filter(problem => problem.path === path).map((problem, index) =>
    <p key={index} className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'}>{problem.message}</p>)
  return <fieldset className="ai-employee-editor__section" disabled={disabled} aria-label="交付物和验收标准">
    <legend>交付什么 / 怎样才算合格</legend>
    {value.kind !== 'present' ? <>
      {value.kind === 'invalid' ? <p role="alert">原有交付要求暂时无法完整显示。重新填写并保存后，将替换原来的交付要求。</p>
        : <p className="ai-employee-editor__muted">还没有填写交付要求。开始填写不会自动保存。</p>}
      <button type="button" disabled={disabled} onClick={start}>{value.kind === 'invalid' ? '重新填写' : '开始填写'}</button>
    </> : <>
      <p className="ai-employee-editor__muted">修改只在保存时提交；填写这些内容不代表已经验收通过。</p>
      <p className="ai-employee-editor__muted">每组最多 500 项，每项最多 16384 个字符；文字过多时保留修改前的内容。</p>
      <h4>交付什么</h4>
      {messages('artifact_contract.deliverables')}
      {value.deliverables.map((delivery, index) => <section key={index} className="ai-employee-editor__section" aria-label={'交付物 ' + (index + 1)}>
        <label className="ai-employee-editor__field">交付物名称
          <textarea aria-label={'交付物 ' + (index + 1) + ' 名称'} rows={2} maxLength={32768} value={delivery.name} disabled={disabled}
            onChange={event => editDelivery(index, { name: event.currentTarget.value })} />
        </label>
        {messages('artifact_contract.deliverables[' + index + '].name')}
        <label className="ai-employee-editor__field">格式
          <textarea aria-label={'交付物 ' + (index + 1) + ' 格式'} rows={1} maxLength={32768} value={delivery.format} disabled={disabled}
            onChange={event => editDelivery(index, { format: event.currentTarget.value })} />
        </label>
        {messages('artifact_contract.deliverables[' + index + '].format')}
        <label className="ai-employee-editor__field">
          <input aria-label={'交付物 ' + (index + 1) + ' 必须交付'} type="checkbox" checked={delivery.required} disabled={disabled}
            onChange={event => editDelivery(index, { required: event.currentTarget.checked })} />必须交付
        </label>
        <button type="button" aria-label={'交付物 ' + (index + 1) + ' 上移'} disabled={disabled || index === 0} onClick={() => moveDelivery(index, -1)}>上移</button>
        <button type="button" aria-label={'交付物 ' + (index + 1) + ' 下移'} disabled={disabled || index === value.deliverables.length - 1} onClick={() => moveDelivery(index, 1)}>下移</button>
        <button type="button" aria-label={'删除交付物 ' + (index + 1)} disabled={disabled}
          onClick={() => { if (!disabled) emit(value.deliverables.filter((_, here) => here !== index), value.acceptance_criteria) }}>删除</button>
      </section>)}
      <button type="button" disabled={disabled || value.deliverables.length >= 500}
        onClick={() => { if (!disabled && value.deliverables.length < 500) emit([...value.deliverables, { name: '', format: '', required: false }], value.acceptance_criteria) }}>添加交付物</button>
      <h4>怎样才算合格</h4>
      {messages('artifact_contract.acceptance_criteria')}
      {value.acceptance_criteria.map((criterion, index) => <section key={index} className="ai-employee-editor__section">
        <label className="ai-employee-editor__field">验收标准 {index + 1}
          <textarea aria-label={'验收标准 ' + (index + 1)} maxLength={32768} rows={2} value={criterion} disabled={disabled}
            onChange={event => { if (!disabled) emit(value.deliverables, value.acceptance_criteria.map((text, here) => here === index ? event.currentTarget.value : text)) }} />
        </label>
        {messages('artifact_contract.acceptance_criteria[' + index + ']')}
        <button type="button" aria-label={'验收标准 ' + (index + 1) + ' 上移'} disabled={disabled || index === 0} onClick={() => moveCriterion(index, -1)}>上移</button>
        <button type="button" aria-label={'验收标准 ' + (index + 1) + ' 下移'} disabled={disabled || index === value.acceptance_criteria.length - 1} onClick={() => moveCriterion(index, 1)}>下移</button>
        <button type="button" aria-label={'删除验收标准 ' + (index + 1)} disabled={disabled}
          onClick={() => { if (!disabled) emit(value.deliverables, value.acceptance_criteria.filter((_, here) => here !== index)) }}>删除</button>
      </section>)}
      <button type="button" disabled={disabled || value.acceptance_criteria.length >= 500}
        onClick={() => { if (!disabled && value.acceptance_criteria.length < 500) emit(value.deliverables, [...value.acceptance_criteria, '']) }}>添加验收标准</button>
    </>}
    <button type="button" disabled={disabled} onClick={() => { if (!disabled) onReset() }}>恢复打开时的内容</button>
  </fieldset>
}
