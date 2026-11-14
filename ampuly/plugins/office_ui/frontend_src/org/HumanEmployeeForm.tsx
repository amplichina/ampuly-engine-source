import { useState } from 'react'
import type { OrgEmployee, OrgRole } from '../types/visual'
import { useI18n } from '../i18n'

export interface AddEmployeePayload {
  name: string
  role_id: string
  kind: 'human' | 'supervised_ai'
  supervisor_employee_id?: string
  contact?: string
}

interface HumanEmployeeFormProps {
  roles: OrgRole[]
  employees: OrgEmployee[]
  readOnly: boolean
  onAdd: (payload: AddEmployeePayload) => void
}

// Put a person into the directory. No template, no prompt, no skills: what a
// person does is decided by the workflow node they are assigned to.
export function HumanEmployeeForm({ roles, employees, readOnly, onAdd }: HumanEmployeeFormProps) {
  const { t, translateMaybe } = useI18n()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [roleId, setRoleId] = useState('')
  const [kind, setKind] = useState<'human' | 'supervised_ai'>('human')
  const [supervisor, setSupervisor] = useState('')
  const [contact, setContact] = useState('')
  const people = employees.filter(e => e.kind === 'human')
  const canSubmit = name.trim() && roleId && (kind === 'human' || supervisor)

  if (!open) {
    return (
      <div className="human-employee-bar">
        <span>{t('org.addEmployee.lead')}</span>
        <button className="myorg-inline-btn" disabled={readOnly} onClick={() => setOpen(true)}>{t('org.addEmployee.open')}</button>
      </div>
    )
  }
  return (
    <form
      className="human-employee-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (!canSubmit) return
        onAdd({ name: name.trim(), role_id: roleId, kind, supervisor_employee_id: kind === 'supervised_ai' ? supervisor : '', contact: contact.trim() })
        setName(''); setContact(''); setOpen(false)
      }}
    >
      <label><span>{t('org.addEmployee.name')} *</span><input value={name} onChange={e => setName(e.target.value)} /></label>
      <label>
        <span>{t('org.addEmployee.role')} *</span>
        <select value={roleId} onChange={e => setRoleId(e.target.value)}>
          <option value="">—</option>
          {roles.map(r => <option key={r.role_id} value={r.role_id}>{translateMaybe('role.name', r.name)}</option>)}
        </select>
      </label>
      <label>
        <span>{t('org.addEmployee.kind')}</span>
        <select value={kind} onChange={e => setKind(e.target.value as 'human' | 'supervised_ai')}>
          <option value="human">{t('team.kind.human')}</option>
          <option value="supervised_ai">{t('team.kind.supervised_ai')}</option>
        </select>
      </label>
      {kind === 'supervised_ai' && (
        <label>
          <span>{t('org.addEmployee.supervisor')} *</span>
          <select value={supervisor} onChange={e => setSupervisor(e.target.value)}>
            <option value="">—</option>
            {people.map(p => <option key={p.employee_id} value={p.employee_id}>{p.name}</option>)}
          </select>
        </label>
      )}
      <label><span>{t('org.addEmployee.contact')}</span><input value={contact} onChange={e => setContact(e.target.value)} /></label>
      <div className="human-employee-actions">
        <button type="button" className="myorg-inline-btn" onClick={() => setOpen(false)}>{t('org.addEmployee.cancel')}</button>
        <button type="submit" className="myorg-inline-btn myorg-inline-btn--primary" disabled={!canSubmit}>{t('org.addEmployee.submit')}</button>
      </div>
    </form>
  )
}
