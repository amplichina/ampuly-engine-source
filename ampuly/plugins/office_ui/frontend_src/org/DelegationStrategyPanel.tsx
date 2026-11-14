import type { OrgRole, RuntimeFrontierSummary, RuntimeSeatInfo, RuntimeTeamInfo, RuntimeWorkItemInfo, RuntimePolicy } from '../types/visual'
import { useI18n } from '../i18n'

interface DelegationStrategyPanelProps {
  roles: OrgRole[]
  runtimeTeams: RuntimeTeamInfo[]
  runtimeSeats: RuntimeSeatInfo[]
  workItems: RuntimeWorkItemInfo[]
  frontier: RuntimeFrontierSummary
  companyProfile: string
  runtimePolicy?: RuntimePolicy
  finalDeciderRoleId?: string | null
  topLevelRoleIds?: string[]
  readOnly?: boolean
  onUpdateOrgStrategy?: (data: { final_decider_role_id?: string | null }) => void
  onUpdateRuntimePolicy?: (policy: Record<string, any>) => void
}

function adaptiveForWorkItem(item: RuntimeWorkItemInfo): Record<string, unknown> | undefined {
  if (item.adaptive && typeof item.adaptive === 'object') return item.adaptive
  const metadata = item.metadata
  if (metadata && typeof metadata === 'object' && metadata.adaptive && typeof metadata.adaptive === 'object') {
    return metadata.adaptive as Record<string, unknown>
  }
  return undefined
}

function missingSignalsForWorkItem(item: RuntimeWorkItemInfo): string[] {
  const adaptive = adaptiveForWorkItem(item)
  const signals = Array.isArray(adaptive?.signals) ? adaptive.signals : []
  return signals
    .filter(signal => signal && typeof signal === 'object')
    .filter(signal => Boolean((signal as Record<string, unknown>).required ?? true) && !Boolean((signal as Record<string, unknown>).satisfied))
    .map(signal => String((signal as Record<string, unknown>).name ?? '').trim())
    .filter(Boolean)
}

function gateOwnerForWorkItem(item: RuntimeWorkItemInfo): string {
  const adaptive = adaptiveForWorkItem(item)
  const stageProfile = adaptive?.work_item_profile
  if (stageProfile && typeof stageProfile === 'object') {
    return String((stageProfile as Record<string, unknown>).gate_owner_role_id ?? '').trim()
  }
  return ''
}

function adaptiveConfidenceLabel(item: RuntimeWorkItemInfo): string {
  const adaptive = adaptiveForWorkItem(item)
  const confidence = typeof adaptive?.confidence === 'number' ? adaptive.confidence : undefined
  return typeof confidence === 'number' ? `${Math.round(confidence * 100)}%` : ''
}

export function DelegationStrategyPanel({
  roles,
  runtimeTeams,
  runtimeSeats,
  workItems,
  frontier,
  companyProfile,
  finalDeciderRoleId,
  topLevelRoleIds,
  readOnly = false,
  onUpdateOrgStrategy,
}: DelegationStrategyPanelProps) {
  const { t, translateMaybe, locale } = useI18n()
  const zh = locale.startsWith('zh')
  const topLevel = roles.filter(r => (topLevelRoleIds ?? []).includes(r.role_id))
  const selectedFinalDecider = finalDeciderRoleId || (topLevel.length === 1 ? topLevel[0]?.role_id : '')
  const hasSelectionError = topLevel.length > 1 && !selectedFinalDecider
  const roleNameMap = new Map(roles.map(r => [r.role_id, r.name]))

  return (
    <div className="wfe-container dsp-panel">
      <div className="wfe-header">
        <h3 className="wfe-title">{t('team.actorRuntime')}</h3>
        <span className="wfe-profile-badge">{companyProfile}</span>
      </div>

      <div className="myorg-collapsible dsp-block">
        <div className="myorg-collapsible-body dsp-block-body">
          <div className="oc-form-row">
            <label>{t('dsp.finalDecider')}</label>
            <select
              value={selectedFinalDecider}
              disabled={readOnly}
              onChange={e => {
                if (readOnly) return
                onUpdateOrgStrategy?.({ final_decider_role_id: e.target.value || null })
              }}
            >
              <option value="">{topLevel.length > 1 ? t('dsp.selectTopRole') : t('dsp.autoSelectTopRole')}</option>
              {topLevel.map(role => (
                <option key={role.role_id} value={role.role_id}>{role.name}</option>
              ))}
            </select>
          </div>
          <div className="dsp-note">
            {t('dsp.seatScoped')}
          </div>
          {hasSelectionError && (
            <div className="org-toast org-toast--warn dsp-warn">
              {t('dsp.multiTopWarning')}
            </div>
          )}
        </div>
      </div>

      {(runtimeTeams.length || runtimeSeats.length || workItems.length) ? (
        <div className="myorg-collapsible dsp-block">
          <div className="myorg-collapsible-body dsp-block-body">
            <div className="oc-form-row">
              <label>{t('common.runtime')}</label>
              <div>
                {translateMaybe('kanban.status', frontier.status || 'running')}
                {frontier.run_id ? ` (${frontier.run_id.slice(0, 8)})` : ''}
              </div>
            </div>
            <div className="dsp-frontier" data-testid="dsp-frontier" role="group" aria-label={t('dsp.frontier')}>
              {([
                ['running', zh ? '运行中' : 'Running', frontier.running_count ?? 0],
                ['ready', zh ? '就绪' : 'Ready', frontier.ready_count ?? 0],
                ['blocked', zh ? '阻塞' : 'Blocked', frontier.blocked_count ?? 0],
                ['waiting', zh ? '等待' : 'Waiting', frontier.waiting_count ?? 0],
              ] as const).map(([key, label, value]) => (
                <div key={key} data-frontier={key} className={value > 0 && (key === 'blocked' || key === 'waiting') ? 'is-held' : ''}>
                  <span>{label}</span><strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="oc-form-row dsp-frontier-line">
              <label>{t('dsp.frontier')}</label>
              <div>
                {t('dsp.frontierStats', {
                  running: frontier.running_count ?? 0,
                  ready: frontier.ready_count ?? 0,
                  blocked: frontier.blocked_count ?? 0,
                  waiting: frontier.waiting_count ?? 0,
                })}
              </div>
            </div>
            <div className="oc-form-row">
              <label>{t('dsp.teams')}</label>
              <div>{runtimeTeams.length}</div>
            </div>
            <div className="oc-form-row">
              <label>{t('dsp.seats')}</label>
              <div>{runtimeSeats.length}</div>
            </div>
            <div className="oc-form-row">
              <label>{t('dsp.workItems')}</label>
              <div>{workItems.length}</div>
            </div>
            {workItems.length > 0 && (
              <div className="dsp-items">
                {workItems.slice(0, 6).map(item => {
                  const adaptive = adaptiveForWorkItem(item)
                  const normalizedState = typeof adaptive?.normalized_state === 'string' ? adaptive.normalized_state : ''
                  const blockedReason = typeof adaptive?.blocked_reason === 'string' ? adaptive.blocked_reason : (item.blocked_reason ?? '')
                  const gateOwner = gateOwnerForWorkItem(item)
                  const missingSignals = missingSignalsForWorkItem(item)
                  const confidence = adaptiveConfidenceLabel(item)
                  const summary = [
                    blockedReason ? t('dsp.waiting', { reason: blockedReason }) : '',
                    gateOwner ? t('dsp.gate', { name: roleNameMap.get(gateOwner) ?? gateOwner }) : '',
                    missingSignals.length ? t('dsp.signals', { list: missingSignals.join(', ') }) : '',
                    confidence ? t('dsp.confidence', { pct: confidence }) : '',
                    normalizedState === 'invalidated' ? t('dsp.invalidated') : '',
                  ].filter(Boolean)
                  return (
                    <div key={item.work_item_id} className="dsp-item" data-work-item={item.work_item_id}>
                      <div>
                        {roleNameMap.get(item.role_id) ?? item.role_id}: {item.title} [{translateMaybe('kanban.status', item.phase)}]
                        {item.kanban_column && (
                          <span className="dsp-dim"> · {translateMaybe('kanban.column', item.kanban_column)}</span>
                        )}
                        {item.batch_id && <span className="dsp-dim"> batch:{item.batch_id}</span>}
                        {normalizedState && normalizedState !== item.phase && (
                          <span className="dsp-state"> state:{normalizedState}</span>
                        )}
                      </div>
                      {summary.length > 0 && (
                        <div className="dsp-item-summary">
                          {summary.join(' • ')}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      ) : null}

    </div>
  )
}
