import { useEffect, useMemo, useRef, useState } from 'react'
import type { OrgRole, OrgEmployee, SavedOrgSummary } from '../types/visual'
import { StructureEditor } from './StructureEditor'
import { resolveRoleIcon } from './roleIcons'
import { OrgIcon, type OrgIconName } from './OrgIcon'
import { useI18n } from '../i18n'


/* ── Quick Start Wizard ────────────────────────────────────────── */

interface QuickStartProps {
  onComplete: (roles: Array<{ name: string; responsibility: string; reportsTo: string }>) => void
  onSwitchToTab: (target: 'architecture') => void
}

function QuickStartWizard({ onComplete, onSwitchToTab }: QuickStartProps) {
  const { t } = useI18n()
  const [step, setStep] = useState(1)
  const [members, setMembers] = useState<Array<{ name: string; resp: string; parent: string }>>([
    { name: '', resp: '', parent: 'owner' },
  ])

  const addMember = () => setMembers([...members, { name: '', resp: '', parent: 'owner' }])
  const updateMember = (i: number, field: string, val: string) => {
    const next = [...members]; (next[i] as any)[field] = val; setMembers(next)
  }
  const removeMember = (i: number) => {
    if (members.length <= 1) return
    setMembers(members.filter((_, idx) => idx !== i))
  }

  const validMembers = members.filter(m => m.name.trim())
  const memberNames = validMembers.map(m => m.name.trim()).filter(Boolean)

  const handleFinish = () => {
    onComplete(
      validMembers.map(m => ({ name: m.name.trim(), responsibility: m.resp.trim(), reportsTo: m.parent })),
    )
  }

  return (
    <div className="qs-wizard">
      <div className="qs-header">
        <OrgIcon name="rocket" size={18} className="qs-header-icon" />
        <div>
          <h3 className="qs-header-title">{t('team.buildYourTeam')}</h3>
          <p className="qs-header-sub">{t('team.qsSubPrefix')} <button className="qs-link-btn" onClick={() => onSwitchToTab('architecture')}>{t('team.useTemplate')}</button></p>
        </div>
      </div>

      <div className="qs-progress">
        {[
          { n: 1, icon: 'team' as OrgIconName, label: t('team.stepMembers') },
          { n: 2, icon: 'check' as OrgIconName, label: t('arch.preview') },
        ].map(s => (
          <div key={s.n} className={`qs-step${step === s.n ? ' qs-step-active' : step > s.n ? ' qs-step-done' : ''}`}>
            <OrgIcon name={s.icon} size={14} className="qs-step-icon" />
            <span>{s.label}</span>
          </div>
        ))}
      </div>

      {step === 1 && (
        <div className="qs-panel">
          <h4>{t('team.whoOnTeam')}</h4>
          <p className="qs-hint">{t('team.qsHint')}</p>
          <div className="qs-members">
            {members.map((m, i) => (
              <div key={i} className="qs-member-row">
                <input className="qs-member-name" value={m.name} placeholder={t('team.roleNamePlaceholder')}
                  onChange={e => updateMember(i, 'name', e.target.value)} />
                <input className="qs-member-resp" value={m.resp} placeholder={t('team.whatTheyDo')}
                  onChange={e => updateMember(i, 'resp', e.target.value)} />
                <select className="qs-member-parent" value={m.parent}
                  onChange={e => updateMember(i, 'parent', e.target.value)}>
                  <option value="owner">{t('team.reportsToYou')}</option>
                  {memberNames.filter(n => n !== m.name.trim()).map(n => (
                    <option key={n} value={n}>{t('team.managedBy', { name: n })}</option>
                  ))}
                </select>
                {members.length > 1 && (
                  <button className="qs-remove-btn" onClick={() => removeMember(i)} title={t('ri.remove')}>
                    <OrgIcon name="trash" size={14} className="qs-remove-icon" />
                  </button>
                )}
              </div>
            ))}
          </div>
          <button className="qs-add-member" onClick={addMember}>
            <OrgIcon name="addPerson" size={14} className="qs-add-icon" /> {t('team.addAnother')}
          </button>
          <div className="qs-nav">
            <span />
            <button className="oc-btn-primary" onClick={() => setStep(2)} disabled={validMembers.length === 0}>
              {t('team.nextPreview')}
            </button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="qs-panel">
          <h4>{t('team.atAGlance')}</h4>
          <div className="qs-preview">
            <div className="qs-preview-section">
              <h5>{t('team.teamMembersCount', { count: validMembers.length })}</h5>
              {validMembers.map((m, i) => (
                <div key={i} className="qs-preview-member">
                  <strong>{m.name}</strong>
                  {m.resp && <span className="qs-preview-resp"> — {m.resp}</span>}
                  <span className="qs-preview-parent">
                    {' '}{m.parent === 'owner' ? t('team.reportsToYouSuffix') : t('team.managedBySuffix', { name: m.parent })}
                  </span>
                </div>
              ))}
            </div>
            <div className="qs-preview-section">
              <h5>{t('team.actorRuntime')}</h5>
              <p>{t('team.actorRuntimeDesc')}</p>
            </div>
          </div>
          <div className="qs-nav">
            <button className="oc-btn-ghost" onClick={() => setStep(1)}>{t('team.back')}</button>
            <button className="oc-btn-primary qs-create-btn" onClick={handleFinish}>{t('team.createTeam')}</button>
          </div>
        </div>
      )}
    </div>
  )
}

/* ── TeamView ──────────────────────────────────────────────────── */

interface TeamViewProps {
  roles: OrgRole[]
  employees: OrgEmployee[]
  /** role_id -> recruited names for the selected session (canvas display only). */
  sessionRecruitmentByRole?: Record<string, string[]> | null
  isCustomMode?: boolean
  organizationId?: string
  onAddRole: (roleId: string, name: string, responsibility: string, reportsTo: string, icon?: string | null) => void
  onBulkAddRoles?: (roles: Array<{ role_id: string; name: string; responsibility: string; reports_to: string }>) => void
  onUpdateRole: (roleId: string, updates: { name?: string; responsibility?: string; reports_to?: string; can_spawn?: string[]; icon?: string | null; execution_strategy?: string; preferred_external_agent?: string | null; prompt_refs?: string[] }) => void
  onDeleteRole: (roleId: string) => void
  onBindExternalTeam?: (data: { boundary_role_id: string; organization_id?: string; scope: 'subtree' }) => void
  onUnbindExternalTeam?: (data: { binding_id?: string; boundary_role_id?: string; organization_id?: string }) => void
  onExport: (data: { package_id: string; name: string; description: string; version: string }) => void
  onImportEmployee?: (employeeId: string) => void
  onResetArchitecture?: () => void
  onSwitchToTab: (target: 'architecture') => void
  // Saved org architectures — passed to StructureEditor for toolbar pill
  savedOrgsList?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  currentOrgVersion?: number
  versionAtLoad?: number | null
  onSavedOrgsList?: () => void
  onSavedOrgSaveAs?: (name: string, overwrite: boolean) => void
  onSavedOrgLoad?: (name: string) => void
  onSavedOrgDelete?: (name: string) => void
}

export function TeamView({
  roles, employees, sessionRecruitmentByRole, isCustomMode, organizationId,
  onAddRole, onBulkAddRoles, onUpdateRole, onDeleteRole, onBindExternalTeam, onUnbindExternalTeam, onExport,
  onImportEmployee,
  onResetArchitecture, onSwitchToTab,
  savedOrgsList, activeSavedOrg, currentOrgVersion, versionAtLoad,
  onSavedOrgsList, onSavedOrgSaveAs, onSavedOrgLoad, onSavedOrgDelete,
}: TeamViewProps) {
  const { t, translateMaybe } = useI18n()
  const [quickStartPending, setQuickStartPending] = useState(false)
  const [showExportForm, setShowExportForm] = useState(false)
  const [exportId, setExportId] = useState('')
  const [exportName, setExportName] = useState('')
  const [exportDesc, setExportDesc] = useState('')
  const [exportVersion, setExportVersion] = useState('1.0.0')

  const empByRole = useMemo(() => {
    const m = new Map<string, OrgEmployee[]>()
    for (const e of employees) {
      const roleIds = e.role_ids?.length ? e.role_ids : [e.role_id]
      for (const roleId of roleIds) {
        if (!roleId) continue
        const list = m.get(roleId) || []; list.push(e); m.set(roleId, list)
      }
    }
    return m
  }, [employees])

  const handleExport = () => {
    if (!exportId.trim() || !exportName.trim()) return
    onExport({ package_id: exportId.trim(), name: exportName.trim(), description: exportDesc, version: exportVersion })
    setShowExportForm(false); setExportId(''); setExportName(''); setExportDesc(''); setExportVersion('1.0.0')
  }

  // When roles arrive after bulk add, clear the quick-start loading state.
  const quickStartTimer = useRef<ReturnType<typeof setTimeout>>(null)
  useEffect(() => {
    if (quickStartPending && roles.length > 0) {
      setQuickStartPending(false)
      if (quickStartTimer.current) { clearTimeout(quickStartTimer.current); quickStartTimer.current = null }
    }
  }, [roles.length, quickStartPending])
  // Timeout: if roles never arrive within 10s, reset to wizard
  useEffect(() => {
    if (quickStartPending) {
      quickStartTimer.current = setTimeout(() => setQuickStartPending(false), 10000)
      return () => { if (quickStartTimer.current) clearTimeout(quickStartTimer.current) }
    }
  }, [quickStartPending])

  const handleQuickStart = (
    newRoles: Array<{ name: string; responsibility: string; reportsTo: string }>,
  ) => {
    const nameToId = new Map<string, string>()
    const usedIds = new Set<string>()
    const bulkRoles: Array<{ role_id: string; name: string; responsibility: string; reports_to: string }> = []

    for (const r of newRoles) {
      let id = r.name.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '').replace(/^-+|-+$/g, '')
      if (!id) continue // skip roles with empty ID (e.g. name was "!!!")
      // Deduplicate: append suffix if ID already used
      let finalId = id
      let suffix = 2
      while (usedIds.has(finalId)) { finalId = `${id}-${suffix++}` }
      usedIds.add(finalId)
      nameToId.set(r.name, finalId)
      const parentId = r.reportsTo === 'owner' ? 'owner' : (nameToId.get(r.reportsTo) || 'owner')
      bulkRoles.push({ role_id: finalId, name: r.name, responsibility: r.responsibility, reports_to: parentId })
    }

    if (bulkRoles.length === 0) return // all roles had invalid names

    if (onBulkAddRoles) {
      setQuickStartPending(true)
      onBulkAddRoles(bulkRoles)
    } else {
      for (const r of bulkRoles) onAddRole(r.role_id, r.name, r.responsibility, r.reports_to)
      setQuickStartPending(true)
    }
  }

  const handleReset = () => {
    if (!confirm(t('team.confirmReset'))) return
    onResetArchitecture?.()
  }

  const assignedRoleCount = useMemo(() => {
    let count = 0
    for (const role of roles) {
      if (role.covered_by_external_team || (empByRole.get(role.role_id) ?? []).length > 0) count += 1
    }
    return count
  }, [roles, empByRole])
  const linkedEmployeeCount = useMemo(
    () => employees.filter(e => e.linked_agent_id).length,
    [employees],
  )
  const vacantRoleCount = Math.max(0, roles.filter(role =>
    !role.covered_by_external_team && (empByRole.get(role.role_id) ?? []).length === 0,
  ).length)

  // Show wizard only in org mode when no roles exist
  if (isCustomMode && roles.length === 0) {
    return (
      <div className="team-view">
        {quickStartPending ? (
          <div className="qs-wizard">
            <div className="qs-loading">
              <span className="spinner-inline" /> {t('team.settingUp')}
            </div>
          </div>
        ) : (
          <QuickStartWizard onComplete={handleQuickStart} onSwitchToTab={onSwitchToTab} />
        )}
      </div>
    )
  }

  return (
    <div className="team-view">
      <div className={`team-command-bar${isCustomMode ? ' team-command-bar--editable' : ' team-command-bar--readonly'}`}>
        <div className="team-command-copy">
          <span className="team-command-eyebrow">{isCustomMode ? t('team.savedOrgWorkspace') : t('team.corporateBaseline')}</span>
          <span className="team-command-title">{isCustomMode ? t('team.editableArch') : t('team.builtinArch')}</span>
        </div>
        <div className="team-command-metrics">
          <span><b>{assignedRoleCount}</b> {t('team.staffedRolesLabel')}</span>
          <span><b>{vacantRoleCount}</b> {t('team.vacantLabel')}</span>
          <span><b>{linkedEmployeeCount}</b> {t('team.inOfficeLabel')}</span>
        </div>
        {isCustomMode && (
          <div className="team-command-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => setShowExportForm(true)}>
              {t('team.exportPackage')}
            </button>
            {onResetArchitecture && (
              <button className="myorg-reset-btn" onClick={handleReset}>
                <OrgIcon name="trash" size={13} className="myorg-reset-icon" /> {t('team.reset')}
              </button>
            )}
          </div>
        )}
      </div>

      {!isCustomMode && (
        <div className="team-readonly-strip">
          {t('team.readonlyStrip')}
        </div>
      )}

      {/* Structure Editor (Canvas + Table + Inspector) */}
      <StructureEditor
        roles={roles}
        employees={employees}
        sessionRecruitmentByRole={sessionRecruitmentByRole}
        isCustomMode={isCustomMode}
        organizationId={organizationId}
        onAddRole={onAddRole}
        onUpdateRole={onUpdateRole}
        onDeleteRole={onDeleteRole}
        onBindExternalTeam={onBindExternalTeam}
        onUnbindExternalTeam={onUnbindExternalTeam}
        savedOrgsList={savedOrgsList ?? null}
        activeSavedOrg={activeSavedOrg ?? null}
        currentOrgVersion={currentOrgVersion ?? 0}
        versionAtLoad={versionAtLoad ?? null}
        onSavedOrgsList={onSavedOrgsList}
        onSavedOrgSaveAs={onSavedOrgSaveAs}
        onSavedOrgLoad={onSavedOrgLoad}
        onSavedOrgDelete={onSavedOrgDelete}
      />
      {/* Export form */}
      {showExportForm && (
        <div className="myorg-form">
          <h4 className="myorg-form-title">{t('team.exportTitle')}</h4>
          <div className="oc-form-row"><label>{t('team.packageId')}</label>
            <input value={exportId} onChange={e => setExportId(e.target.value)} placeholder="my-architecture" /></div>
          <div className="oc-form-row"><label>{t('ri.name')}</label>
            <input value={exportName} onChange={e => setExportName(e.target.value)} placeholder={t('team.namePlaceholder')} /></div>
          <div className="oc-form-row"><label>{t('team.description')}</label>
            <input value={exportDesc} onChange={e => setExportDesc(e.target.value)} placeholder={t('team.descPlaceholder')} /></div>
          <div className="oc-form-row"><label>{t('team.version')}</label>
            <input value={exportVersion} onChange={e => setExportVersion(e.target.value)} /></div>
          <div className="oc-form-actions">
            <button className="oc-btn-primary" onClick={handleExport} disabled={!exportId.trim() || !exportName.trim()}>{t('team.export')}</button>
            <button className="oc-btn-ghost" onClick={() => setShowExportForm(false)}>{t('common.cancel')}</button>
          </div>
        </div>
      )}

      {/* Team Roster — enhanced with actions */}
      <div className="myorg-section">
        <div className="myorg-section-header">
          <OrgIcon name="person" size={15} className="myorg-section-icon" />
          <h3 className="myorg-section-title">{t('team.roster')}</h3>
          <span className="myorg-section-count">{t('team.membersCount', { count: employees.length })}</span>
          <span className="myorg-section-spacer" />
          <span className="myorg-section-note">{t('team.staffedRatio', { a: assignedRoleCount, b: roles.length })}</span>
        </div>
        <div className="team-roster-grid">
          {roles.map(r => {
            const emps = empByRole.get(r.role_id) || []
            const externallyCovered = Boolean(r.covered_by_external_team)
            return (
              <div key={r.role_id} className={`team-roster-card${externallyCovered ? ' team-roster-card--external' : ''}`}>
                <div className="team-roster-card-header">
                  <img src={resolveRoleIcon(r.icon)} alt="" className="team-roster-card-icon" />
                  <span className="team-roster-role-name">{translateMaybe('role.name', r.name)}</span>
                  <span className="team-roster-count">{externallyCovered ? t('team.teamBadge') : (emps.length || t('team.vacantBadge'))}</span>
                </div>
                {externallyCovered ? (
                  <div className="team-roster-external">
                    <b>{r.external_team_boundary ? t('team.swarmBoundary') : t('team.coveredExternal')}</b>
                    <span>{r.external_team_boundary
                      ? t('team.noSeparateRecruitment')
                      : t('team.inheritedFrom', { id: r.external_team_boundary_role_id ?? t('team.teamBoundary') })}</span>
                  </div>
                ) : emps.length > 0 ? emps.map(e => (
                  <div key={e.employee_id} className="team-roster-emp">
                    <OrgIcon name="person" size={18} className="team-roster-emp-avatar" />
                    <div className="team-roster-emp-info">
                      <span className="team-roster-emp-name">{e.name}</span>
                      <span className={`team-roster-seniority team-roster-seniority--${e.seniority}`}>{translateMaybe('team.seniority', e.seniority)}</span>
                      {e.kind === 'human' && <span className="team-roster-kind team-roster-kind--human">{t('team.kind.human')}</span>}
                      {e.kind === 'supervised_ai' && <span className="team-roster-kind team-roster-kind--supervised">{t('team.kind.supervised_ai')}</span>}
                    </div>
                    {e.linked_agent_id ? (
                      <span className="team-roster-badge team-roster-badge--active">{t('team.inOffice')}</span>
                    ) : onImportEmployee && e.role_id ? (
                      <button className="team-roster-deploy-btn" onClick={() => onImportEmployee(e.employee_id)}
                        title={t('team.deployTitle')}>
                        <OrgIcon name="active" size={13} className="team-roster-deploy-icon" /> {t('team.deploy')}
                      </button>
                    ) : null}
                  </div>
                )) : (
                  <div className="team-roster-vacant">
                    <span className="team-roster-vacant-text">{t('team.noMembers')}</span>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
