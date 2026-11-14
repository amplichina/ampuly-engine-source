/**
 * RoleInspector — Figma-style floating property panel (D4).
 *
 * Renders on the right of the StructureEditor when a role is selected.
 * Six collapsible groups:
 *   1. Identity       (expanded by default): name, responsibility, icon
 *   2. Hierarchy      (expanded by default): reports_to, can_spawn
 *   3. Tools          (collapsed): 22-item checklist grouped by prefix
 *   4. Prompts        (collapsed): textarea (one prompt_ref per line)
 *   5. Runtime        (collapsed): execution_strategy, preferred_external_agent
 *   6. Advanced       (collapsed): role_type, skill_refs, artifact_contract_ref
 *
 * Edits are debounced 500ms and batched into a single onUpdateRole call per
 * quiescence window.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { OrgRole, OrgEmployee } from '../types/visual'
import { TASK_AGENT_OPTIONS, executionAgentLabel } from '../lib/externalAgents'
import { ROLE_ICON_KEYS, ROLE_ICONS, resolveRoleIcon, type RoleIconKey } from './roleIcons'
import { useI18n } from '../i18n'

/** Tool union derived from company_runtime_profiles.py _CORPORATE_*_TOOLS. */
const AVAILABLE_TOOLS = [
  'file_read', 'file_write', 'file_edit', 'file_search', 'list_dir',
  'shell_exec',
  'web_search', 'web_fetch',
  'todo_read', 'todo_write',
  'browser_navigate', 'browser_navigate_back', 'browser_snapshot', 'browser_observe',
  'browser_wait_for', 'browser_scroll', 'browser_click', 'browser_type',
  'browser_select_option', 'browser_take_screenshot',
  'browser_evaluate', 'browser_close',
  'image_generate', 'python_exec',
] as const

const TOOL_GROUPS: { label: string; prefix: string; tools: readonly string[] }[] = [
  { label: 'Files',    prefix: 'file_',    tools: AVAILABLE_TOOLS.filter(t => t.startsWith('file_')) },
  { label: 'Shell',    prefix: 'shell_',   tools: AVAILABLE_TOOLS.filter(t => t === 'shell_exec') },
  { label: 'Web',      prefix: 'web_',     tools: AVAILABLE_TOOLS.filter(t => t.startsWith('web_')) },
  { label: 'TODOs',    prefix: 'todo_',    tools: AVAILABLE_TOOLS.filter(t => t.startsWith('todo_')) },
  { label: 'Browser',  prefix: 'browser_', tools: AVAILABLE_TOOLS.filter(t => t.startsWith('browser_')) },
  { label: 'Create',   prefix: 'create_',  tools: AVAILABLE_TOOLS.filter(t => t === 'image_generate' || t === 'python_exec') },
]

const EXTERNAL_AGENTS = TASK_AGENT_OPTIONS.filter(
  agent => agent !== 'native' && agent !== 'jiuwenswarm',
)
const EXECUTION_STRATEGIES = [
  { value: 'auto',     label: 'Auto',     hint: 'System picks native or external based on role config' },
  { value: 'native',   label: 'Native',   hint: 'Run directly in-process via LLM' },
  { value: 'external', label: 'External', hint: 'Delegate to an external agent (codex, cursor, etc.)' },
] as const

/* ── Props ─────────────────────────────────────────────────────── */

interface RoleInspectorProps {
  role: OrgRole
  allRoles: OrgRole[]
  employees: OrgEmployee[]
  readOnly?: boolean
  teamBindingEditable?: boolean
  /** Hosts that only mean "adjust this role" (the workbench drawer) hide the
   *  delete footer; removing a role is an org-page decision. */
  hideDelete?: boolean
  organizationId?: string
  onUpdateRole: (roleId: string, updates: RoleUpdatePatch) => void
  onBindExternalTeam?: (data: {
    boundary_role_id: string
    organization_id?: string
    scope: 'subtree'
  }) => void
  onUnbindExternalTeam?: (data: {
    binding_id?: string
    boundary_role_id?: string
    organization_id?: string
  }) => void
  onDeleteRole: (roleId: string) => void
  onClose: () => void
}

/** Matches the shape that App.tsx's onUpdateRole accepts; `tools` is forwarded
 *  via the same path (backend RoleConfig Pydantic model accepts it). */
export interface RoleUpdatePatch {
  name?: string
  responsibility?: string
  reports_to?: string
  can_spawn?: string[]
  icon?: string | null
  execution_strategy?: string
  preferred_external_agent?: string | null
  prompt_refs?: string[]
  tools?: string[]
}

/* ── RoleInspector ─────────────────────────────────────────────── */

export function RoleInspector({
  role, allRoles, employees, readOnly, teamBindingEditable = true, hideDelete = false, organizationId,
  onUpdateRole, onBindExternalTeam, onUnbindExternalTeam, onDeleteRole, onClose,
}: RoleInspectorProps) {
  const { t, translateMaybe } = useI18n()
  const [name, setName] = useState(role.name)
  const [responsibility, setResponsibility] = useState(role.responsibility)
  const [reportsTo, setReportsTo] = useState(role.reports_to)
  const [iconKey, setIconKey] = useState<string | null>(role.icon ?? null)
  const [canSpawn, setCanSpawn] = useState<Set<string>>(() => new Set(role.can_spawn))
  const [tools, setTools] = useState<Set<string>>(() => new Set(role.tools))
  const [execStrategy, setExecStrategy] = useState<string>(
    role.runtime_policy?.execution_strategy ?? 'auto',
  )
  const [extAgent, setExtAgent] = useState<string | null>(role.preferred_external_agent ?? null)
  const [promptRefs, setPromptRefs] = useState<string>((role.prompt_refs ?? []).join('\n'))
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [teamMutationPending, setTeamMutationPending] = useState(false)

  // Reset local state when selected role changes
  const lastRoleIdRef = useRef(role.role_id)
  useEffect(() => {
    if (lastRoleIdRef.current === role.role_id) return
    lastRoleIdRef.current = role.role_id
    setName(role.name)
    setResponsibility(role.responsibility)
    setReportsTo(role.reports_to)
    setIconKey(role.icon ?? null)
    setCanSpawn(new Set(role.can_spawn))
    setTools(new Set(role.tools))
    setExecStrategy(role.runtime_policy?.execution_strategy ?? 'auto')
    setExtAgent(role.preferred_external_agent ?? null)
    setPromptRefs((role.prompt_refs ?? []).join('\n'))
    setConfirmDelete(false)
  }, [role])

  useEffect(() => {
    setExtAgent(role.preferred_external_agent ?? null)
    setTeamMutationPending(false)
  }, [role.preferred_external_agent, role.external_team_binding_id])

  useEffect(() => {
    if (!teamMutationPending) return
    const timeout = setTimeout(() => setTeamMutationPending(false), 12_000)
    return () => clearTimeout(timeout)
  }, [teamMutationPending])

  /* ── Debounced save: batch fragments, fire once after 500ms quiescence ── */
  const dirtyRef = useRef<RoleUpdatePatch>({})
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const roleIdRef = useRef(role.role_id)
  useEffect(() => { roleIdRef.current = role.role_id }, [role.role_id])

  const flush = useCallback(() => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    const patch = dirtyRef.current
    dirtyRef.current = {}
    if (Object.keys(patch).length === 0) return
    onUpdateRole(roleIdRef.current, patch)
  }, [onUpdateRole])

  const scheduleSave = useCallback((fragment: RoleUpdatePatch) => {
    if (readOnly) return
    dirtyRef.current = { ...dirtyRef.current, ...fragment }
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(flush, 500)
  }, [flush, readOnly])

  // Unmount flush — capture any pending edits so they are not lost
  useEffect(() => {
    return () => {
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
      const patch = dirtyRef.current
      if (Object.keys(patch).length > 0) {
        dirtyRef.current = {}
        onUpdateRole(roleIdRef.current, patch)
      }
    }
  }, [onUpdateRole])

  /* ── Setters ───────────────────────────────────────────────── */
  const handleNameChange = (v: string) => { setName(v); scheduleSave({ name: v }) }
  const handleResponsibilityChange = (v: string) => { setResponsibility(v); scheduleSave({ responsibility: v }) }
  const handleReportsToChange = (v: string) => { setReportsTo(v); scheduleSave({ reports_to: v }) }
  const handleIconChange = (v: string | null) => { setIconKey(v); scheduleSave({ icon: v }) }
  const handleExecStrategyChange = (v: string) => { setExecStrategy(v); scheduleSave({ execution_strategy: v }) }
  const handleExtAgentChange = (v: string | null) => { setExtAgent(v); scheduleSave({ preferred_external_agent: v }) }
  const handlePromptRefsChange = (v: string) => {
    setPromptRefs(v)
    const lines = v.split('\n').map(s => s.trim()).filter(Boolean)
    scheduleSave({ prompt_refs: lines })
  }
  const toggleCanSpawn = (id: string) => {
    const next = new Set(canSpawn)
    if (next.has(id)) next.delete(id); else next.add(id)
    setCanSpawn(next)
    scheduleSave({ can_spawn: Array.from(next) })
  }
  const toggleTool = (toolName: string) => {
    const next = new Set(tools)
    if (next.has(toolName)) next.delete(toolName); else next.add(toolName)
    setTools(next)
    scheduleSave({ tools: Array.from(next) })
  }

  /* ── Derived ───────────────────────────────────────────────── */
  const otherRoles = useMemo(
    () => allRoles.filter(r => r.role_id !== role.role_id),
    [allRoles, role.role_id],
  )
  const employeeCount = useMemo(
    () => employees.filter(e => (e.role_ids?.length ? e.role_ids : [e.role_id]).includes(role.role_id)).length,
    [employees, role.role_id],
  )
  const promptRefCount = useMemo(
    () => promptRefs.split('\n').filter(s => s.trim()).length,
    [promptRefs],
  )
  const isExternalTeamBoundary = Boolean(role.external_team_boundary)
  const isCoveredByExternalTeam = Boolean(role.covered_by_external_team)
  const isCoveredDescendant = isCoveredByExternalTeam && !isExternalTeamBoundary
  const coveredRoles = useMemo(() => {
    const manifestRoles = role.external_team_capability_manifest?.covered_roles ?? []
    if (manifestRoles.length > 0) {
      return manifestRoles.map(item => ({
        role_id: item.role_id,
        name: item.name || item.role_id,
        responsibility: item.responsibility || '',
      }))
    }
    const descendants: OrgRole[] = []
    const seen = new Set<string>()
    const visit = (roleId: string) => {
      if (seen.has(roleId)) return
      seen.add(roleId)
      const current = allRoles.find(item => item.role_id === roleId)
      if (current) descendants.push(current)
      allRoles.filter(item => item.reports_to === roleId).forEach(item => visit(item.role_id))
    }
    visit(role.role_id)
    return descendants
  }, [allRoles, role.external_team_capability_manifest, role.role_id])
  const capabilityManifest = role.external_team_capability_manifest

  const handleExecutionScopeChange = (scope: 'role' | 'subtree') => {
    if (!teamBindingEditable || teamMutationPending) return
    if (scope === 'subtree' && !onBindExternalTeam) return
    if (scope === 'role' && !onUnbindExternalTeam) return
    flush()
    setTeamMutationPending(true)
    if (scope === 'subtree') {
      onBindExternalTeam?.({
        boundary_role_id: role.role_id,
        organization_id: organizationId,
        scope: 'subtree',
      })
      return
    }
    onUnbindExternalTeam?.({
      binding_id: role.external_team_binding_id ?? undefined,
      boundary_role_id: role.external_team_boundary_role_id ?? role.role_id,
      organization_id: organizationId,
    })
  }

  /* ── Delete (2-step confirm) ──────────────────────────────── */
  const handleDelete = () => {
    if (!confirmDelete) { setConfirmDelete(true); return }
    flush()
    onDeleteRole(role.role_id)
  }

  return (
    <aside className="ri-panel" aria-label={t('ri.ariaInspector', { name: role.name })}>
      <header className="ri-panel-header">
        <img src={resolveRoleIcon(iconKey)} alt="" className="ri-panel-icon" />
        <div className="ri-panel-title-wrap">
          <h3 className="ri-panel-title">{name || t('ri.unnamed')}</h3>
          <code className="ri-panel-id">{role.role_id}</code>
        </div>
        <button className="btn btn-ghost btn-sm ri-panel-close" onClick={onClose} title={t('exec.closeEsc')}>✕</button>
      </header>

      <div className="ri-panel-body">
        <InspectorGroup title={t('ri.identity')} defaultExpanded>
          <InspectorField label={t('ri.name')}>
            <input
              className="ri-text-input"
              value={name}
              onChange={e => handleNameChange(e.target.value)}
              disabled={readOnly}
            />
          </InspectorField>
          <InspectorField label={t('ri.responsibility')}>
            <textarea
              className="ri-textarea"
              rows={3}
              value={responsibility}
              onChange={e => handleResponsibilityChange(e.target.value)}
              disabled={readOnly}
            />
          </InspectorField>
          <InspectorField label={t('ri.icon')}>
            <IconPicker value={iconKey} onChange={handleIconChange} readOnly={readOnly} />
          </InspectorField>
        </InspectorGroup>

        <InspectorGroup title={t('ri.hierarchy')} defaultExpanded>
          <InspectorField label={t('ri.reportsTo')}>
            <select
              className="ri-select"
              value={reportsTo}
              onChange={e => handleReportsToChange(e.target.value)}
              disabled={readOnly}
            >
              <option value="owner">{t('ri.owner')}</option>
              {otherRoles.map(r => (
                <option key={r.role_id} value={r.role_id}>{r.name}</option>
              ))}
            </select>
          </InspectorField>
          <InspectorField label={t('ri.canDelegate')}>
            <MultiSelect
              allIds={otherRoles.map(r => r.role_id)}
              labelFor={(id) => otherRoles.find(r => r.role_id === id)?.name ?? id}
              selected={canSpawn}
              onToggle={toggleCanSpawn}
              readOnly={readOnly}
            />
          </InspectorField>
          <InspectorField label={t('ri.employeesAssigned')}>
            <span className="ri-meta-value">{employeeCount}</span>
          </InspectorField>
        </InspectorGroup>

        <InspectorGroup title={t('ri.tools', { count: tools.size })}>
          <ToolChecklist tools={tools} onToggle={toggleTool} readOnly={readOnly} />
        </InspectorGroup>

        <InspectorGroup title={t('ri.prompts', { count: promptRefCount })}>
          <InspectorField label={t('ri.promptRefs')}>
            <textarea
              className="ri-textarea ri-textarea-mono"
              rows={5}
              placeholder={t('ri.promptPlaceholder')}
              value={promptRefs}
              onChange={e => handlePromptRefsChange(e.target.value)}
              disabled={readOnly}
            />
          </InspectorField>
        </InspectorGroup>

        <InspectorGroup title={t('ri.runtimePolicy')} defaultExpanded>
          <InspectorField label={t('ri.executionScope')}>
            <div className="ri-scope-options">
              <label className="ri-scope-option">
                <input
                  type="radio"
                  name={`execution-scope-${role.role_id}`}
                  checked={!isCoveredByExternalTeam}
                  onChange={() => handleExecutionScopeChange('role')}
                  disabled={!teamBindingEditable || teamMutationPending || isCoveredDescendant}
                />
                <span>
                  <b>{t('ri.thisRoleOnly')}</b>
                  <small>{t('ri.thisRoleOnlyHint')}</small>
                </span>
              </label>
              <label className="ri-scope-option">
                <input
                  type="radio"
                  name={`execution-scope-${role.role_id}`}
                  checked={isExternalTeamBoundary}
                  onChange={() => handleExecutionScopeChange('subtree')}
                  disabled={!teamBindingEditable || teamMutationPending || isCoveredDescendant}
                />
                <span>
                  <b>{t('ri.roleAndDescendants')}</b>
                  <small>{t('ri.roleAndDescendantsHint')}</small>
                </span>
              </label>
            </div>
          </InspectorField>

          {isCoveredDescendant && (
            <div className="ri-team-notice">
              {t('ri.coveredNotice', { id: role.external_team_boundary_role_id ?? '' })}
            </div>
          )}

          {isExternalTeamBoundary && (
            <div className="ri-team-card">
              <div className="ri-team-card-title">
                <span>{t('ri.swarmTeam')}</span>
                <code>{role.external_team_binding_id}</code>
              </div>
              <div className="ri-team-card-copy">
                {t('ri.swarmDispatch', { id: role.role_id })}
              </div>
              <div className="ri-team-covered-list">
                {coveredRoles.map(item => (
                  <div key={item.role_id} className="ri-team-covered-role" title={item.responsibility}>
                    <span>{item.name}</span><code>{item.role_id}</code>
                  </div>
                ))}
              </div>
              {capabilityManifest && (
                <div className="ri-team-capabilities">
                  <b>{t('ri.capabilityContract')}</b>
                  <span>{capabilityManifest.capabilities.length > 0
                    ? capabilityManifest.capabilities.join(' · ')
                    : t('ri.capabilityDerived')}</span>
                  {capabilityManifest.deliverables.length > 0 && (
                    <span>{t('ri.deliverables', { items: capabilityManifest.deliverables.join(' · ') })}</span>
                  )}
                  <code title={capabilityManifest.manifest_hash}>
                    manifest {capabilityManifest.manifest_hash.slice(0, 12)}
                  </code>
                </div>
              )}
            </div>
          )}

          {!isCoveredByExternalTeam && <InspectorField label={t('ri.executionStrategy')}>
            <div className="ri-radio-group">
              {EXECUTION_STRATEGIES.map(opt => (
                <label key={opt.value} className="ri-radio" title={translateMaybe('ri.strategyHint', opt.hint)}>
                  <input
                    type="radio"
                    name={`exec-strategy-${role.role_id}`}
                    value={opt.value}
                    checked={execStrategy === opt.value}
                    onChange={() => handleExecStrategyChange(opt.value)}
                    disabled={readOnly}
                  />
                  {translateMaybe('ri.strategy', opt.label)}
                </label>
              ))}
            </div>
          </InspectorField>}
          {!isCoveredByExternalTeam && execStrategy === 'external' && (
            <InspectorField label={t('ri.preferredAgent')}>
              <select
                className="ri-select"
                value={extAgent ?? ''}
                onChange={e => handleExtAgentChange(e.target.value || null)}
                disabled={readOnly}
              >
                <option value="">{t('ri.any')}</option>
                {extAgent === 'jiuwenswarm' && (
                  <option value="jiuwenswarm">{t('ri.swarmOption')}</option>
                )}
                {EXTERNAL_AGENTS.map(a => <option key={a} value={a}>{executionAgentLabel(a)}</option>)}
              </select>
              {extAgent === 'jiuwenswarm' && (
                <span className="ri-field-hint">{t('ri.legacyHint')}</span>
              )}
            </InspectorField>
          )}
          {!isExternalTeamBoundary && readOnly && !isCoveredDescendant && (
            <div className="ri-team-notice">
              {t('ri.corporateFixed')}
            </div>
          )}
        </InspectorGroup>

        <InspectorGroup title={t('ri.advanced')}>
          <InspectorField label={t('ri.roleType')}>
            <span className="ri-meta-value">{role.role_type ?? 'worker'}</span>
          </InspectorField>
          <InspectorField label={t('ri.skills')}>
            <span className="ri-meta-value">
              {role.skill_refs && role.skill_refs.length > 0 ? role.skill_refs.join(', ') : t('ri.none')}
            </span>
          </InspectorField>
          <InspectorField label={t('ri.artifactContract')}>
            <span className="ri-meta-value">{role.artifact_contract_ref ?? t('ri.none')}</span>
          </InspectorField>
        </InspectorGroup>
      </div>

      {!readOnly && !hideDelete && (
        <footer className="ri-panel-footer">
          <button
            className={`btn btn-sm ${confirmDelete ? 'btn-danger' : 'btn-ghost'}`}
            onClick={handleDelete}
            onBlur={() => setConfirmDelete(false)}
          >
            {confirmDelete ? t('ri.confirmDelete') : t('ri.deleteRole')}
          </button>
        </footer>
      )}
    </aside>
  )
}

/* ── Sub-components ────────────────────────────────────────────── */

function InspectorGroup({ title, defaultExpanded = false, children }: {
  title: string
  defaultExpanded?: boolean
  children: ReactNode
}) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  return (
    <section className={`ri-group${expanded ? ' is-expanded' : ''}`}>
      <button className="ri-group-header" onClick={() => setExpanded(e => !e)}>
        <span className="ri-group-caret">{expanded ? '▾' : '▸'}</span>
        <span className="ri-group-title">{title}</span>
      </button>
      {expanded && <div className="ri-group-body">{children}</div>}
    </section>
  )
}

function InspectorField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ri-field">
      <label className="ri-field-label">{label}</label>
      <div className="ri-field-control">{children}</div>
    </div>
  )
}

function IconPicker({ value, onChange, readOnly }: {
  value: string | null
  onChange: (v: string | null) => void
  readOnly?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  return (
    <div className="ri-icon-picker">
      <button
        className="ri-icon-picker-trigger"
        onClick={() => !readOnly && setOpen(o => !o)}
        disabled={readOnly}
      >
        <img src={resolveRoleIcon(value)} alt="" className="ri-icon-picker-current" />
        <span className="ri-icon-picker-label">{value ?? 'generic'}</span>
      </button>
      {open && (
        <div className="ri-icon-picker-popover">
          <button
            className={`ri-icon-option${value === null ? ' is-active' : ''}`}
            onClick={() => { onChange(null); setOpen(false) }}
            title={t('ri.defaultIcon')}
          >
            <img src={ROLE_ICONS.generic} alt="" />
          </button>
          {(ROLE_ICON_KEYS as readonly RoleIconKey[]).filter(k => k !== 'generic').map(key => (
            <button
              key={key}
              className={`ri-icon-option${value === key ? ' is-active' : ''}`}
              onClick={() => { onChange(key); setOpen(false) }}
              title={key}
            >
              <img src={ROLE_ICONS[key]} alt={key} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function MultiSelect({ allIds, labelFor, selected, onToggle, readOnly }: {
  allIds: string[]
  labelFor: (id: string) => string
  selected: Set<string>
  onToggle: (id: string) => void
  readOnly?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const available = allIds.filter(id => !selected.has(id))

  return (
    <div className="ri-multiselect">
      <div className="ri-chips">
        {Array.from(selected).map(id => (
          <span key={id} className="ri-chip">
            {labelFor(id)}
            {!readOnly && (
              <button className="ri-chip-x" onClick={() => onToggle(id)} title={t('ri.remove')}>×</button>
            )}
          </span>
        ))}
        {!readOnly && available.length > 0 && (
          <button className="ri-chip-add" onClick={() => setOpen(o => !o)}>{t('ri.add')}</button>
        )}
        {selected.size === 0 && readOnly && <span className="ri-meta-value">{t('ri.none')}</span>}
      </div>
      {open && !readOnly && (
        <div className="ri-multiselect-popover">
          {available.map(id => (
            <button
              key={id}
              className="ri-multiselect-option"
              onClick={() => { onToggle(id); setOpen(false) }}
            >{labelFor(id)}</button>
          ))}
        </div>
      )}
    </div>
  )
}

function ToolChecklist({ tools, onToggle, readOnly }: {
  tools: Set<string>
  onToggle: (name: string) => void
  readOnly?: boolean
}) {
  const { translateMaybe } = useI18n()
  return (
    <div className="ri-toolcheck">
      {TOOL_GROUPS.map(g => (
        <fieldset key={g.prefix} className="ri-toolcheck-group">
          <legend className="ri-toolcheck-legend">{translateMaybe('ri.toolGroup', g.label)}</legend>
          <div className="ri-toolcheck-items">
            {g.tools.map(t => (
              <label key={t} className="ri-toolcheck-item">
                <input
                  type="checkbox"
                  checked={tools.has(t)}
                  onChange={() => onToggle(t)}
                  disabled={readOnly}
                />
                <span className="ri-toolcheck-name">{t.replace(g.prefix, '') || t}</span>
              </label>
            ))}
          </div>
        </fieldset>
      ))}
    </div>
  )
}
