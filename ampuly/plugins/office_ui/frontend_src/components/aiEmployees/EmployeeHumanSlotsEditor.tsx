import {
  EmployeeHumanSlotsError, employeeHumanSlotsState, employeeHumanSlotsProblems,
  EMPLOYEE_HUMAN_ROLES, EMPLOYEE_HUMAN_DECISIONS, EMPLOYEE_HUMAN_CHANNELS,
  employeeHumanRoleLabels, employeeHumanDecisionLabels, employeeHumanChannelLabels,
  type EmployeeHumanSlot, type EmployeeHumanSlotsState,
} from '../../lib/aiEmployeeHumanSlots'

export interface EmployeeHumanSlotsEditorProps {
  value: EmployeeHumanSlotsState
  disabled: boolean
  onChange(value: EmployeeHumanSlotsState): void
  onReset(): void
}
const blank = (): EmployeeHumanSlot => ({ slot: '', required: false, decides: [] })
const reviewer = (): EmployeeHumanSlot => ({ slot: 'reviewer', required: true, decides: ['release', 'reject'] })
const selected = (values: readonly string[], choice: string, checked: boolean): readonly string[] =>
  checked ? values.includes(choice) ? values : [...values, choice] : values.filter(value => value !== choice)
/** Controlled template form; no contacts, runtime grants, hooks, or transport. */
export function EmployeeHumanSlotsEditor({ value, disabled, onChange, onReset }: EmployeeHumanSlotsEditorProps) {
  const emit = (rows: readonly EmployeeHumanSlot[]): void => {
    if (disabled) return
    let next: EmployeeHumanSlotsState
    try { next = employeeHumanSlotsState(rows) } catch (error) {
      if (error instanceof EmployeeHumanSlotsError) return
      throw error
    }
    onChange(next)
  }
  const edit = (index: number, update: (row: EmployeeHumanSlot) => EmployeeHumanSlot): void => {
    if (!disabled && value.kind === 'present') emit(value.items.map((row, here) => here === index ? update(row) : row))
  }
  const addReviewer = (): void => {
    if (disabled || value.items.length >= 7 || value.items.some(row => row.slot === 'reviewer')) return
    emit(value.kind === 'present' ? [...value.items, reviewer()] : [reviewer()])
  }
  const move = (index: number, direction: -1 | 1): void => {
    const target = index + direction
    if (disabled || value.kind !== 'present' || target < 0 || target >= value.items.length) return
    const rows = [...value.items]
    ;[rows[index], rows[target]] = [rows[target], rows[index]]
    emit(rows)
  }
  const hints = employeeHumanSlotsProblems(value)
  const messages = (path: string) => hints.filter(problem => problem.path === path
    || ((path.endsWith('.decides') || path.endsWith('.channel_options')) && problem.path.startsWith(path + '['))).map((problem, index) =>
    <p key={index} className="ai-employee-editor__muted" role={problem.severity === 'error' ? 'alert' : 'status'}>{problem.message}</p>)
  return <fieldset className="ai-employee-editor__section" aria-label="人工职责约定" disabled={disabled}>
    <legend>哪些环节需要人</legend>
    <p className="ai-employee-editor__muted">只是岗位职责约定；具体人员在项目中配置，尚未建立联系或授予操作权限。</p>
    {value.kind !== 'present' ? <>
      {value.kind === 'invalid' ? <p role="alert">原有人工职责暂时无法完整显示。重新配置并保存后，将替换原来的人工职责约定。</p>
        : <p className="ai-employee-editor__muted">还没有填写人工职责。点击添加后仍需明确保存，不会自动安排人员。</p>}
      <button type="button" disabled={disabled} onClick={addReviewer}>添加必需审核人</button>
    </> : <>
      {messages('human_slots')}
      {value.items.map((row, index) => <section key={index} className="ai-employee-editor__section" aria-label={'人工职责 ' + (index + 1)}>
        <label className="ai-employee-editor__field">负责角色
          <select aria-label={'人工职责 ' + (index + 1) + ' 负责角色'} disabled={disabled} value={row.slot === '' ? '' : employeeHumanRoleLabels[row.slot]}
            onChange={event => {
              if (disabled) return
              const label = event.currentTarget.value
              const role = label === '' ? '' : EMPLOYEE_HUMAN_ROLES.find(choice => employeeHumanRoleLabels[choice] === label)
              if (role !== undefined) edit(index, current => ({ ...current, slot: role }))
            }}>
            <option value="">请选择负责角色</option>
            {EMPLOYEE_HUMAN_ROLES.map(role => <option key={role} value={employeeHumanRoleLabels[role]}>{employeeHumanRoleLabels[role]}</option>)}
          </select>
        </label>
        {messages('human_slots[' + index + '].slot')}
        <label className="ai-employee-editor__field">
          <input type="checkbox" aria-label={'人工职责 ' + (index + 1) + ' 必须有人负责'} checked={row.required} disabled={disabled}
            onChange={event => { if (!disabled) edit(index, current => ({ ...current, required: event.currentTarget.checked })) }} />必须有人负责
        </label>
        {messages('human_slots[' + index + '].required')}
        <h4>人能做哪些判断</h4>
        {EMPLOYEE_HUMAN_DECISIONS.map(choice => <label className="ai-employee-editor__field" key={choice}>
          <input type="checkbox" aria-label={'人工职责 ' + (index + 1) + ' ' + employeeHumanDecisionLabels[choice]} checked={row.decides.includes(choice)} disabled={disabled}
            onChange={event => { if (!disabled) edit(index, current => ({ ...current, decides: selected(current.decides, choice, event.currentTarget.checked) })) }} />{employeeHumanDecisionLabels[choice]}
        </label>)}
        {messages('human_slots[' + index + '].decides')}
        <h4>可以在哪里反馈</h4>
        <label className="ai-employee-editor__field">
          <input type="checkbox" aria-label={'人工职责 ' + (index + 1) + ' 设置反馈方式'} checked={Object.hasOwn(row, 'channel_options')} disabled={disabled}
            onChange={event => {
              if (disabled) return
              const enabled = event.currentTarget.checked
              edit(index, current => enabled ? { ...current, channel_options: current.channel_options ?? [] }
                : { slot: current.slot, required: current.required, decides: current.decides })
            }} />设置反馈方式
        </label>
        {Object.hasOwn(row, 'channel_options') ? <>
          {!row.channel_options!.length && <p className="ai-employee-editor__muted">已选择设置反馈方式，目前没有勾选方式。</p>}
          {EMPLOYEE_HUMAN_CHANNELS.map(choice => <label className="ai-employee-editor__field" key={choice}>
            <input type="checkbox" aria-label={'人工职责 ' + (index + 1) + ' ' + employeeHumanChannelLabels[choice]} checked={row.channel_options!.includes(choice)} disabled={disabled}
              onChange={event => { if (!disabled) edit(index, current => ({ ...current, channel_options: selected(current.channel_options!, choice, event.currentTarget.checked) })) }} />{employeeHumanChannelLabels[choice]}
          </label>)}
          {messages('human_slots[' + index + '].channel_options')}
        </> : <p className="ai-employee-editor__muted">尚未设置反馈方式。</p>}
        <button type="button" aria-label={'人工职责 ' + (index + 1) + ' 上移'} disabled={disabled || index === 0} onClick={() => move(index, -1)}>上移</button>
        <button type="button" aria-label={'人工职责 ' + (index + 1) + ' 下移'} disabled={disabled || index === value.items.length - 1} onClick={() => move(index, 1)}>下移</button>
        <button type="button" aria-label={'删除人工职责 ' + (index + 1)} disabled={disabled}
          onClick={() => { if (!disabled) emit(value.items.filter((_, here) => here !== index)) }}>删除</button>
      </section>)}
      <button type="button" disabled={disabled || value.items.length >= 7}
        onClick={() => { if (!disabled && value.items.length < 7) emit([...value.items, blank()]) }}>添加人工职责</button>
      <button type="button" disabled={disabled || value.items.length >= 7 || value.items.some(row => row.slot === 'reviewer')} onClick={addReviewer}>添加必需审核人</button>
    </>}
    <button type="button" disabled={disabled} onClick={() => { if (!disabled) onReset() }}>恢复打开时的内容</button>
  </fieldset>
}
