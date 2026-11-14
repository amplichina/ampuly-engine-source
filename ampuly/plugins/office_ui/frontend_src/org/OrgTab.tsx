import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  OrgInfoPayload,
  OrgCreateMemberInput,
  OrgSavedCreatePayload,
  SavedOrgSummary,
  ReorgProposalInfo,
  ArchitecturePreset,
  ArchitecturePresetDetail,
} from '../types/visual'
import type { InteractionReplyReceipt } from '../types/chat'
import type { Answering } from '../lib/answerPath'
import { TeamView } from './TeamView'
import { DelegationStrategyPanel } from './DelegationStrategyPanel'
import { ArchitectureMarketplace } from './ArchitectureMarketplace'
import { HumanEmployeeForm, type AddEmployeePayload } from './HumanEmployeeForm'
import { ConfigImportExportPanel } from './ConfigImportExportPanel'
import { OrgCreateModal } from './OrgCreateModal'
import { getRuntimeOrgView } from '../lib/runtimeOrg'
import { useI18n } from '../i18n'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import './org.css'
import './team.css'
import './marketplace.css'
import { OrgIcon, type OrgIconName } from './OrgIcon'
import './config.css'
import './structure.css'

type SubTab = 'team' | 'runtime' | 'architecture'

function humanizeOrgName(value?: string | null): string {
  const normalized = String(value ?? '').trim()
  if (!normalized) return ''
  return normalized
    .replace(/^org[_-]/i, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase())
}

interface OrgTabProps {
  data: OrgInfoPayload | null
  error?: string | null
  connected?: boolean
  /** role_id -> recruited names for the selected session (canvas display only). */
  sessionRecruitmentByRole?: Record<string, string[]> | null
  reorgProposals: ReorgProposalInfo[]
  isCustomMode?: boolean
  onRequestData: () => void
  onAddEmployee?: (payload: AddEmployeePayload) => void
  onImportEmployee?: (employeeId: string) => void
  onRequestReorgList: () => void
  onReorgDecide: (
    proposal: ReorgProposalInfo,
    approved: boolean,
    notes?: string,
  ) => Promise<InteractionReplyReceipt>
  /** By checkpoint id: the answer that is out (lib/answerPath, S3-7): a proposal's card waits while its own is. */
  answering?: Answering
  // Market
  onMarketExport?: (data: { package_id: string; name: string; description: string; version: string }) => void
  onMarketInstall?: (path: string, strategy: string) => void
  onMarketUninstall?: (packageId: string) => void
  // Architecture gallery
  marketPresets?: ArchitecturePreset[]
  marketPreviewData?: ArchitecturePresetDetail | null
  onMarketBrowse?: () => void
  onMarketPreview?: (presetId: string) => void
  onMarketApplyPreset?: (presetId: string, strategy: string) => void
  onMarketClearPreview?: () => void
  // Config import/export
  onConfigExport?: () => void
  onConfigImport?: (yaml: string, dryRun: boolean) => void
  configExportYaml?: string | null
  configImportPreview?: { roles_added: number; roles_removed: number; employees_changed: number } | null
  configImportError?: string | null
  // Saved org architectures (named snapshots) — rendered in the Team tab toolbar
  onSavedOrgsList?: () => void
  onSavedOrgSaveAs?: (name: string, overwrite: boolean) => void
  onSavedOrgCreate?: (organizationName: string, members: OrgCreateMemberInput[]) => void
  onSavedOrgLoad?: (name: string) => void
  onSavedOrgDelete?: (name: string) => void
  savedOrgsList?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  activeSavedOrgVersionAtLoad?: number | null
  orgCreatePending?: boolean
  orgCreateResult?: (OrgSavedCreatePayload & { nonce: number }) | null
  onSelectCorporate?: () => void
  // Org editing
  onAddRole?: (roleId: string, name: string, responsibility: string, reportsTo: string, icon?: string | null) => void
  onBulkAddRoles?: (roles: Array<{ role_id: string; name: string; responsibility: string; reports_to: string }>) => void
  onUpdateRole?: (roleId: string, updates: { name?: string; responsibility?: string; reports_to?: string; can_spawn?: string[]; icon?: string | null; execution_strategy?: string; preferred_external_agent?: string | null; prompt_refs?: string[] }) => void
  onDeleteRole?: (roleId: string) => void
  onBindExternalTeam?: (data: { boundary_role_id: string; organization_id?: string; scope: 'subtree' }) => void
  onUnbindExternalTeam?: (data: { binding_id?: string; boundary_role_id?: string; organization_id?: string }) => void
  onUpdateOrgStrategy?: (data: { final_decider_role_id?: string | null }) => void
  onUpdateRuntimePolicy?: (policy: Record<string, any>) => void
  onResetArchitecture?: () => void
}

export function OrgTab({
  data, error, connected = true, sessionRecruitmentByRole, reorgProposals, isCustomMode,
  onRequestData, onAddEmployee, onImportEmployee, onRequestReorgList, onReorgDecide, answering,
  onMarketExport, onMarketInstall, onMarketUninstall,
  marketPresets, marketPreviewData, onMarketBrowse, onMarketPreview, onMarketApplyPreset, onMarketClearPreview,
  onAddRole, onBulkAddRoles, onUpdateRole, onDeleteRole, onBindExternalTeam, onUnbindExternalTeam, onUpdateOrgStrategy,
  onUpdateRuntimePolicy, onResetArchitecture,
  onConfigExport, onConfigImport, configExportYaml, configImportPreview, configImportError,
  onSavedOrgsList, onSavedOrgSaveAs, onSavedOrgCreate, onSavedOrgLoad, onSavedOrgDelete, savedOrgsList,
  activeSavedOrg, activeSavedOrgVersionAtLoad, orgCreatePending, orgCreateResult, onSelectCorporate,
}: OrgTabProps) {
  const { t } = useI18n()
  const [activeTab, setActiveTab] = useState<SubTab>('team')
  const [createOpen, setCreateOpen] = useState(false)

  const switchTab = useCallback((tab: SubTab) => {
    setActiveTab(tab)
  }, [])
  const [applyingPresetId, setApplyingPresetId] = useState<string | null>(null)
  const versionAtApply = useRef<number>(-1) // track org_version when apply starts
  const [toast, setToast] = useState<{ msg: string; type: 'info' | 'warn' } | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(null)

  const showToast = useCallback((msg: string, type: 'info' | 'warn' = 'info') => {
    setToast({ msg, type })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3000)
  }, [])

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current)
  }, [])

  const onRequestDataRef = useRef(onRequestData)
  const onRequestReorgListRef = useRef(onRequestReorgList)
  const onMarketBrowseRef = useRef(onMarketBrowse)
  const onSavedOrgsListRef = useRef(onSavedOrgsList)
  onRequestDataRef.current = onRequestData
  onRequestReorgListRef.current = onRequestReorgList
  onMarketBrowseRef.current = onMarketBrowse
  onSavedOrgsListRef.current = onSavedOrgsList

  useEffect(() => {
    onRequestDataRef.current()
    onRequestReorgListRef.current()
    onMarketBrowseRef.current?.()
    onSavedOrgsListRef.current?.()
  }, [])

  useEffect(() => {
    if (!orgCreateResult || !orgCreateResult.ok) return
    setCreateOpen(false)
    setActiveTab('team')
    showToast(t('org.createdToast', { name: orgCreateResult.organization_name || orgCreateResult.name }))
  }, [orgCreateResult, showToast, t])

  // In org mode: show only user-owned roles. In company mode: show all roles read-only.
  const allRoles = data?.roles ?? []
  const displayRoles = useMemo(() => isCustomMode ? allRoles.filter(r => !r.is_builtin) : allRoles, [allRoles, isCustomMode])
  const displayEmployees = useMemo(() => data?.employees ?? [], [data?.employees])
  const runtimeView = useMemo(() => getRuntimeOrgView(data), [data])
  const activeAgents = useMemo(() => displayEmployees.filter(e => e.linked_agent_id), [displayEmployees])
  const configuredOrgName = data?.organization_name?.trim()
  const activeOrgLabel = configuredOrgName || humanizeOrgName(activeSavedOrg) || (isCustomMode ? t('org.customOrg') : t('org.corporateCompany'))
  const activeOrgId = (isCustomMode ? (data?.organization_id || activeSavedOrg) : 'corporate') || ''
  const architectureKindLabel = isCustomMode ? t('org.kind.saved') : t('org.kind.corporate')
  const architectureStateLabel = isCustomMode
    ? activeSavedOrg ? t('org.state.editableSaved') : t('org.state.editableDraft')
    : t('org.state.readOnly')
  const runtimeStateLabel = runtimeView.frontier.status || runtimeView.projectRun?.status || runtimeView.projectRun?.lifecycle_status || t('common.ready')

  const installedIds = useMemo(() => new Set((data?.installed_packages ?? []).map(p => p.package_id)), [data?.installed_packages])

  // When applying a preset, wait for org_version to change (every config.save() increments it)
  const orgVersion = data?.org_version ?? 0
  useEffect(() => {
    if (applyingPresetId && versionAtApply.current >= 0 && orgVersion !== versionAtApply.current) {
      setApplyingPresetId(null)
      versionAtApply.current = -1
      setActiveTab('team')
      showToast(t('org.architectureApplied'))
    }
  }, [orgVersion, applyingPresetId, showToast, t])

  const handleApplyPreset = (presetId: string, strategy: string) => {
    versionAtApply.current = orgVersion // snapshot current version
    setApplyingPresetId(presetId)
    onMarketApplyPreset?.(presetId, strategy)
  }

  const handleOrgSelection = (value: string) => {
    if (value === 'corporate') {
      onSelectCorporate?.()
      return
    }
    if (value.startsWith('org:')) {
      const orgName = value.slice(4)
      if (orgName) onSavedOrgLoad?.(orgName)
    }
  }

  if (!data) {
    return (
      <div className="org-tab">
        {!connected && <div className="org-offline-state" role="status">{t('org.offline')}</div>}
        {error ? (
          <div className={`org-error-state org-error-state--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
            <div className="org-error-message"><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong><br />{error}</div>
            <button className="org-retry-btn" onClick={onRequestData} disabled={!connected}>{t('org.retry')}</button>
          </div>
        ) : (
          <div className="org-loading">{t('org.loading')}</div>
        )}
      </div>
    )
  }

  const installedPackages = data.installed_packages ?? []
  const savedOrgOptions = savedOrgsList ?? []
  const selectedOrgValue = isCustomMode && activeSavedOrg ? `org:${activeSavedOrg}` : 'corporate'

  return (
    <div className={`org-tab${connected ? '' : ' org-tab--offline'}`}>
      {!connected && <div className="org-offline-state" role="status">{t('org.offline')}</div>}
      <div className={`org-header${isCustomMode ? ' org-header--custom' : ' org-header--corporate'}`}>
        <div className="org-header-main">
          <div className="org-eyebrow">
            <span>{t('org.company')}</span>
            <span className="org-eyebrow-separator">/</span>
            <span>{architectureKindLabel}</span>
          </div>
          <div className="org-title-row">
            <h2 className="org-title">{activeOrgLabel}</h2>
            <span className="org-version-badge">v{data.org_version}</span>
            <span className={`org-state-badge${isCustomMode ? ' org-state-badge--editable' : ' org-state-badge--readonly'}`}>
              {architectureStateLabel}
            </span>
          </div>
          <div className="org-header-meta">
            <span className="org-meta-pill">{data.company_profile || (isCustomMode ? 'custom' : 'corporate')}</span>
            {activeOrgId && <code className="org-meta-code">{activeOrgId}</code>}
            <span className="org-meta-pill org-meta-pill--runtime">{runtimeStateLabel}</span>
            <span className="org-meta-pill org-meta-pill--saved">{t('common.autoSaved')}</span>
          </div>
        </div>

        <div className="org-control-panel">
          <label className="org-switcher">
            <span className="org-switcher-label">{t('org.organization')}</span>
            <span className="org-switcher-select-wrap">
              <select
                className="org-switcher-select"
                value={selectedOrgValue}
                onChange={e => handleOrgSelection(e.target.value)}
                onFocus={() => connected && onSavedOrgsList?.()}
                onPointerDown={() => connected && onSavedOrgsList?.()}
                disabled={!connected}
                aria-label={t('org.organization')}
              >
                <option value="corporate">{t('org.corporate')}</option>
                {savedOrgOptions.map(org => (
                  <option key={org.name} value={`org:${org.name}`}>
                    {(org.organization_name || org.name).trim() || org.name}
                  </option>
                ))}
              </select>
            </span>
          </label>
          <button type="button" className="org-create-trigger" onClick={() => setCreateOpen(true)} disabled={!connected}>
            <span className="org-create-trigger-icon" aria-hidden>+</span>
            {t('org.newOrganization')}
          </button>
        </div>

        <div className="org-stats-strip">
          <span className="org-stat">
            <OrgIcon name="agents" size={13} className="org-stat-icon" />
            <b>{displayRoles.length}</b> {t('org.roles')}
          </span>
          <span className="org-stat">
            <OrgIcon name="person" size={13} className="org-stat-icon" />
            <b>{displayEmployees.length}</b> {t('org.employees')}
          </span>
          <span className="org-stat">
            <OrgIcon name="runtime" size={13} className="org-stat-icon" />
            <b>{runtimeView.runtimeTeams.length}</b> {t('org.runtimeTeams')}
          </span>
          <span className="org-stat">
            <OrgIcon name="active" size={13} className="org-stat-icon org-stat-icon--active" />
            <b>{activeAgents.length}</b> {t('common.active')}
          </span>
        </div>
      </div>

      <div className="org-scope-notice" data-testid="org-control-plane-scope" role="note">
        <span className="org-scope-notice-mark" aria-hidden="true">◎</span>
        <div className="org-scope-notice-copy">
          <strong>{t('org.scope.title')}</strong>
          <span>{t('org.scope.description')}</span>
        </div>
      </div>

      <div className="org-subtabs">
        {([
          { id: 'team' as SubTab, icon: 'team' as OrgIconName, label: t('org.tab.team'), count: displayRoles.length },
          { id: 'runtime' as SubTab, icon: 'runtime' as OrgIconName, label: t('org.tab.runtime'), count: runtimeView.runtimeTeams.length },
          { id: 'architecture' as SubTab, icon: 'architecture' as OrgIconName, label: t('org.tab.architecture'), count: marketPresets?.length ?? 0 },
        ]).map(tab => (
          <button key={tab.id}
            className={`org-subtab${activeTab === tab.id ? ' org-subtab--active' : ''}`}
            onClick={() => switchTab(tab.id)}>
            <OrgIcon name={tab.icon} size={14} className="org-subtab-icon" />
            <span className="org-subtab-label">{tab.label}</span>
            <span className="org-subtab-count">{tab.count}</span>
          </button>
        ))}
      </div>

      {/* ── Tab content ─────────────────────────────────── */}
      {/* Toast notification */}
      {toast && (
        <div className={`org-toast org-toast--${toast.type}`} onClick={() => setToast(null)}>
          {toast.msg}
        </div>
      )}

      <div
        className="org-tab-content"
        onClickCapture={event => {
          if (!connected) {
            event.preventDefault()
            event.stopPropagation()
          }
        }}
        onChangeCapture={event => {
          if (!connected) {
            event.preventDefault()
            event.stopPropagation()
          }
        }}
      >

        {/* Team tab */}
        {activeTab === 'team' && (
          <>
          {onAddEmployee && (
            <HumanEmployeeForm roles={displayRoles} employees={displayEmployees} readOnly={!isCustomMode} onAdd={onAddEmployee} />
          )}
          <TeamView
            roles={displayRoles}
            employees={displayEmployees}
            sessionRecruitmentByRole={sessionRecruitmentByRole}
            isCustomMode={isCustomMode}
            organizationId={data.organization_id}
            onAddRole={onAddRole ?? (() => {})}
            onBulkAddRoles={onBulkAddRoles}
            onUpdateRole={onUpdateRole ?? (() => {})}
            onDeleteRole={onDeleteRole ?? (() => {})}
            onBindExternalTeam={onBindExternalTeam}
            onUnbindExternalTeam={onUnbindExternalTeam}
            onExport={onMarketExport ?? (() => {})}
            onImportEmployee={onImportEmployee}
            onResetArchitecture={onResetArchitecture}
            onSwitchToTab={(target) => setActiveTab(target)}
            savedOrgsList={savedOrgsList}
            activeSavedOrg={activeSavedOrg ?? null}
            currentOrgVersion={data?.org_version ?? 0}
            versionAtLoad={activeSavedOrgVersionAtLoad ?? null}
            onSavedOrgsList={onSavedOrgsList}
            onSavedOrgSaveAs={onSavedOrgSaveAs}
            onSavedOrgLoad={onSavedOrgLoad}
            onSavedOrgDelete={onSavedOrgDelete}
          />
          </>
        )}

        {/* Runtime tab */}
        {activeTab === 'runtime' && (
          <DelegationStrategyPanel
            roles={displayRoles}
            runtimeTeams={runtimeView.runtimeTeams}
            runtimeSeats={runtimeView.runtimeSeats}
            workItems={runtimeView.workItems}
            frontier={runtimeView.frontier}
            companyProfile={data.company_profile}
            runtimePolicy={data.runtime_policy}
            finalDeciderRoleId={data.final_decider_role_id}
            topLevelRoleIds={data.top_level_role_ids}
            readOnly={!isCustomMode}
            onUpdateOrgStrategy={onUpdateOrgStrategy}
            onUpdateRuntimePolicy={onUpdateRuntimePolicy}
          />
        )}

        {/* Architecture tab */}
        {activeTab === 'architecture' && (
          <>
            <ArchitectureMarketplace
              presets={marketPresets ?? []}
              installedIds={installedIds}
              previewData={marketPreviewData ?? null}
              applyingPresetId={applyingPresetId}
              readOnly={!isCustomMode}
              onPreview={onMarketPreview ?? (() => {})}
              onApplyPreset={handleApplyPreset}
              onClearPreview={onMarketClearPreview ?? (() => {})}
              installedPackages={installedPackages}
              channels={data.channels}
              reorgProposals={reorgProposals}
              isCustomMode={!!isCustomMode}
              onReorgDecide={onReorgDecide}
              answering={answering}
              onMarketInstall={(p, s) => onMarketInstall?.(p, s)}
              onMarketUninstall={(id) => onMarketUninstall?.(id)}
            />
            {isCustomMode && (
              <ConfigImportExportPanel
                onExport={onConfigExport ?? (() => {})}
                onImport={onConfigImport ?? (() => {})}
                configExportYaml={configExportYaml ?? null}
                importPreview={configImportPreview ?? null}
                importError={configImportError ?? null}
              />
            )}
          </>
        )}

      </div>
      <OrgCreateModal
        open={createOpen}
        pending={orgCreatePending}
        result={orgCreateResult ?? null}
        onClose={() => setCreateOpen(false)}
        onCreate={(organizationName, members) => onSavedOrgCreate?.(organizationName, members)}
      />
    </div>
  )
}
