import { useEffect, useState } from 'react'
import type { OrgInfoPayload, VisualSnapshot } from '../types/visual'
import { useI18n } from '../i18n'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import { DeploymentsPanel, type Deployment, type DeploymentsPanelProps, type Rollout } from './DeploymentsPanel'
import { ProcessFactoryPanel, type ProcessFactoryPanelProps } from './ProcessFactoryPanel'
import { ConnectorCandidatesPanel, type ConnectorCandidatesPanelProps } from './ConnectorCandidatesPanel'
import { BrowserSessionsPanel, type BrowserSessionsPanelProps } from './BrowserSessionsPanel'
import { GovernancePolicyPanel, type GovernancePolicyPanelProps } from './GovernancePolicyPanel'

interface PendingHarnessTurn {
  session_id: string; provider: string; project_id: string; run_id: string; order_id: string
  task_id: string; state: string; turn_id: string; started_at: string
  live_here?: boolean; liveness?: string; settle_allowed?: boolean; outcome_status?: string
}

export interface TurnLedgerAction {
  kind: string
  state: 'completed' | 'in_flight' | 'outcome_unknown' | 'unknown_after' | string
  reported_by?: string
  status?: string
  exit_code?: number
  command_preview?: string
  cwd?: string
  tool?: string
  server?: string
  success?: boolean
  changes?: Array<{ path: string; kind?: string; change?: string }>
  changes_truncated?: boolean
  total?: number
  scan_complete?: boolean
  reason?: string
}

export interface WorkspaceNow {
  compared: boolean
  reason?: string
  changes?: Array<{ path: string; change: string }>
  total?: number
  changes_truncated?: boolean
  scan_complete?: boolean
}

export interface TurnLedger {
  session_id: string
  turn_id: string
  provider?: string
  ledger: 'provider_reported' | 'none' | string
  reason?: string
  turn_end?: string
  ledger_complete?: boolean
  unpaired_rows?: number
  actions: TurnLedgerAction[]
  /** Only for a turn that never ended: the workspace compared NOW against the baseline kept with the turn. */
  workspace_now?: WorkspaceNow
}

const CHANGE_TEXT: Record<string, string> = { created: '新增', modified: '修改', deleted: '删除', observed: '出现（基线不完整）' }
function changeList(changes: Array<{ path: string; kind?: string; change?: string }> | undefined): string {
  return (changes ?? []).map(item => `${CHANGE_TEXT[item.change ?? ''] ?? item.kind ?? item.change ?? ''} ${item.path}`).join('、')
}

const LEDGER_STATE_TEXT: Record<string, string> = {
  completed: '已完成（Provider 自述，未经核验）',
  in_flight: '已开始、无完成记录',
  outcome_unknown: '我方工具已领取、未返回——可能已经写入',
  unknown_after: '此后未知',
}

function ledgerActionText(action: TurnLedgerAction): string {
  if (action.state === 'unknown_after') return '回合没有结束记录：最后一条记录之后 Provider 做了什么，没有被写下来。没有记录不等于没有动作。'
  if (action.kind === 'commandExecution') return `命令 ${action.command_preview ?? ''}${typeof action.exit_code === 'number' ? ` · 退出码 ${action.exit_code}` : ''}`
  if (action.kind === 'fileChange') return `改动文件 ${changeList(action.changes)}${action.changes_truncated ? ' ……（已截断）' : ''}`
  if (action.kind === 'workspace_changes') return `回合前后工作区对比（扫描所得，不是 Provider 上报；分不清是谁写的）：${changeList(action.changes) || '无变化'}${action.changes_truncated ? ` ……（共 ${action.total} 处，已截断）` : ''}${action.scan_complete === false ? '（扫描不完整）' : ''}`
  if (action.kind === 'mcpToolCall') return `MCP 工具 ${action.server ?? ''} / ${action.tool ?? ''}`
  if (action.kind === 'our_tool') return `我方工具 ${action.tool ?? ''}${action.state === 'completed' ? (action.success ? ' · 成功' : ' · 失败') : ''}`
  if (action.kind === 'approval') return '审批已回答'
  return action.kind
}

function canSettlePendingTurn(item: PendingHarnessTurn): boolean {
  return item.settle_allowed === true && !item.live_here && ['stale', 'unknown'].includes(item.liveness ?? '')
}

export function pendingTurnSelectionMatches(item: Pick<PendingHarnessTurn, 'session_id' | 'turn_id'>, selection: { session: string; turn: string }): boolean {
  return !!selection.session && !!selection.turn && item.session_id === selection.session && item.turn_id === selection.turn
}

function pendingTurnLocation(item: PendingHarnessTurn): string {
  if (item.live_here || item.liveness === 'here') return '本进程仍在跑，先中断'
  if (item.liveness === 'elsewhere') return '其他进程仍有心跳，不可结清'
  if (item.liveness === 'stale') return '心跳已过期，但尚未证明执行停止'
  return '运行位置未知，不能认定已停止'
}

export interface CapabilityCenterPageProps {
  data: OrgInfoPayload | null
  snapshot: VisualSnapshot | null
  connected: boolean
  loading?: boolean
  error?: string | null
  projectId: string
  /** Show one group of panels and leave out the page's own head and overview: 配置 and 执行区 each show the group
   *  that belongs to them. Absent, the page is what it always was. */
  group?: CapabilityGroup
  onRefresh: () => void
  onReprobe?: () => void
  /** Which environment variable holds which customer's key -- never a value. */
  credentialReferences?: Array<{
    purpose: string
    customer_id: string
    environment_id: string
    env_var: string
    is_set: boolean
  }>
  /** Which customer, on which environment, runs which version. */
  deployments?: Deployment[]
  /** Rollouts in flight, shown per target rather than as one verdict. */
  rollouts?: Rollout[]
  /** True when `rollouts` is the server's newest 50 and older ones exist. */
  rolloutsHasMore?: boolean
  /** Connectors somebody has switched off, with the reason they gave. */
  connectorSwitches?: Array<{ connector: string; enabled: boolean; actor: string; at: string; reason: string; explicit?: boolean }>
  /** Owner-only. Stopping needs a reason; resuming does not. */
  onSwitchConnector?: (connector: string, enabled: boolean, reason: string) => void
  /** 搭建台's 「去打开」 asked to land on the desktop channel's block: scrolled to once it is on screen. */
  focusDesktopChannel?: boolean
  onDesktopChannelFocused?: () => void
  connectorCandidates?: ConnectorCandidatesPanelProps['candidates']
  connectorRegistrations?: ConnectorCandidatesPanelProps['registrations']
  connectorCandidateError?: string | null
  onRefreshConnectorCandidates?: ConnectorCandidatesPanelProps['onRefresh']
  onTestConnectorCandidate?: ConnectorCandidatesPanelProps['onTest']
  onReviewConnectorCandidate?: ConnectorCandidatesPanelProps['onReview']
  onRegisterConnectorCandidate?: ConnectorCandidatesPanelProps['onRegister']
  onRevokeConnectorCandidate?: ConnectorCandidatesPanelProps['onRevoke']
  browserSessions?: BrowserSessionsPanelProps['sessions']
  browserSessionError?: string | null
  browserAccountInspection?: BrowserSessionsPanelProps['accountInspection']
  onInspectBrowserAccount?: BrowserSessionsPanelProps['onInspectAccount']
  browserLimits?: BrowserSessionsPanelProps['limits']
  browserCombination?: BrowserSessionsPanelProps['combination']
  browserTrail?: BrowserSessionsPanelProps['trail']
  browserLookBackNote?: string | null
  browserAuditExportNote?: string | null
  browserScreenshot?: BrowserSessionsPanelProps['screenshot']
  browserLiveFrame?: BrowserSessionsPanelProps['liveFrame']
  browserLiveBinding?: BrowserSessionsPanelProps['liveBinding']
  onBrowserViewStart?: BrowserSessionsPanelProps['onViewStart']
  onBrowserViewStop?: BrowserSessionsPanelProps['onViewStop']
  onBrowserViewInput?: BrowserSessionsPanelProps['onViewInput']
  onBrowserAuditTrail?: BrowserSessionsPanelProps['onAuditTrail']
  onBrowserAuditExport?: BrowserSessionsPanelProps['onAuditExport']
  onBrowserLookBack?: BrowserSessionsPanelProps['onLookBack']
  onBrowserScreenshot?: BrowserSessionsPanelProps['onScreenshot']
  onRefreshBrowserSessions?: BrowserSessionsPanelProps['onRefresh']
  onControlBrowserSession?: BrowserSessionsPanelProps['onControl']
  /** X07: sealed demonstrations become candidates here. undefined = not offered (not the owner). */
  browserCandidates?: BrowserSessionsPanelProps['candidates']
  browserCandidateNote?: string | null
  browserSealedDemo?: BrowserSessionsPanelProps['sealedDemo']
  onBrowserCandidate?: BrowserSessionsPanelProps['onCandidate']
  /** The governance policy as the server reads it. undefined = never asked (not the owner): the summary grid stays. */
  governancePolicy?: GovernancePolicyPanelProps['view']
  governanceError?: string | null
  onRefreshGovernancePolicy?: GovernancePolicyPanelProps['onRefresh']
  onSetGovernancePolicy?: GovernancePolicyPanelProps['onSet']
  pendingTurns?: PendingHarnessTurn[]
  /** What the provider said it did in one pending turn (owner-only read). Null until asked for. */
  turnLedger?: TurnLedger | null
  onTurnActions?: (sessionId: string, turnId: string) => void
  pendingTurnError?: string | null
  onSettleTurn?: (sessionId: string, reason: string, expectedTurnId: string) => void
  onInterruptTurn?: (sessionId: string, expectedTurnId: string) => void
  /** Everything the deployment panel needs to offer its controls. */
  solutionPackages?: Array<{ solution_id: string; version: string }>
  onPlanRollout?: DeploymentsPanelProps['onPlanRollout']
  onAdvanceRollout?: DeploymentsPanelProps['onAdvance']
  onRolloutHealth?: DeploymentsPanelProps['onHealth']
  onPromoteRollout?: DeploymentsPanelProps['onPromote']
  onRollbackRollout?: DeploymentsPanelProps['onRollback']
  rolloutError?: string | null
  /** The process factory: descriptions and what they cannot yet answer. */
  processSpecs?: ProcessFactoryPanelProps['specs']
  selectedSpec?: string
  specReview?: ProcessFactoryPanelProps['review']
  builderRounds?: ProcessFactoryPanelProps['rounds']
  onSelectSpec?: ProcessFactoryPanelProps['onSelect']
  onBuilderTrial?: ProcessFactoryPanelProps['onTrial']
  onBuilderTrialResume?: ProcessFactoryPanelProps['onResume']
  processDraft?: ProcessFactoryPanelProps['draft']
  onSaveSpec?: ProcessFactoryPanelProps['onSave']
  onNewSpec?: ProcessFactoryPanelProps['onNew']
  onSpecCandidate?: ProcessFactoryPanelProps['onCandidate']
  processTemplates?: ProcessFactoryPanelProps['templates']
  templateError?: ProcessFactoryPanelProps['templateError']
  onRefreshTemplates?: ProcessFactoryPanelProps['onRefreshTemplates']
  specSaving?: boolean
  trialState?: ProcessFactoryPanelProps['trialState']
  factoryError?: string | null
  /** Read-only shape check of the channel credentials; sends nothing. */
  onCheckChannels?: () => void
  channelCheck?: {
    muted: boolean
    channels: Array<{
      id: string
      name_zh: string
      name_en: string
      shape_ok: boolean
      problems: number
      fields: Array<{ env: string; label_zh: string; label_en: string; optional: boolean; state: string; detail_zh?: string; detail_en?: string }>
    }>
  } | null
  reprobing?: boolean
}

function stateLabel(value: unknown): string {
  const text = String(value ?? '').trim()
  return text || '—'
}

/** Where 控制面's panels belong in the new shape: connectors and credentials, channels, the browser takeover,
 *  what is deployed, and what keeps a run safe. */
export const CAPABILITY_GROUPS = ['connect', 'channels', 'execution', 'release', 'safety'] as const
export type CapabilityGroup = typeof CAPABILITY_GROUPS[number]

/** The owner's desktop-channel switch (4.7-A06; desktop_connector.py CHANNEL_SWITCH). */
export const DESKTOP_CHANNEL = 'desktop_channel'
/** Where 搭建台's 「去打开」 lands: the desktop channel's own block on this page. */
export const DESKTOP_CHANNEL_ANCHOR = 'desktop-channel'

type ConnectorSwitchEntry = NonNullable<CapabilityCenterPageProps['connectorSwitches']>[number]

/** Open only when somebody explicitly switched it on. A switch nobody set is closed -- the reverse of a send switch,
 *  which nobody set is on (the server's channel_switch_open). The list carries only the switches somebody set. */
export function desktopChannelOpen(entry: ConnectorSwitchEntry | undefined): boolean {
  return !!entry && entry.enabled === true && entry.explicit !== false
}

/** What 「确认打开」 sends: this channel, switched on. Opening takes no reason; closing does, as a stop does. */
export function openDesktopChannel(onSwitch: (connector: string, enabled: boolean, reason: string) => void): void {
  onSwitch(DESKTOP_CHANNEL, true, '')
}

function switchTime(at: string): string {
  return at ? at.slice(0, 16).replace('T', ' ') : '—'
}

export function CapabilityCenterPage({ group, data, snapshot, connected, loading = false, error, projectId, onRefresh, onReprobe, onCheckChannels, channelCheck, credentialReferences, deployments, rollouts, rolloutsHasMore, connectorSwitches, onSwitchConnector, focusDesktopChannel, onDesktopChannelFocused, connectorCandidates, connectorRegistrations, connectorCandidateError, onRefreshConnectorCandidates, onTestConnectorCandidate, onReviewConnectorCandidate, onRegisterConnectorCandidate, onRevokeConnectorCandidate, browserSessions, browserSessionError, browserAccountInspection, onInspectBrowserAccount, browserLimits, browserCombination, browserTrail, browserLookBackNote, browserAuditExportNote, browserScreenshot, browserLiveFrame, browserLiveBinding, onBrowserAuditTrail, onBrowserAuditExport, onBrowserLookBack, onBrowserScreenshot, onBrowserViewStart, onBrowserViewStop, onBrowserViewInput, onRefreshBrowserSessions, onControlBrowserSession, browserCandidates, browserCandidateNote, browserSealedDemo, onBrowserCandidate, governancePolicy, governanceError, onRefreshGovernancePolicy, onSetGovernancePolicy, pendingTurns, pendingTurnError, onSettleTurn, onInterruptTurn, turnLedger, onTurnActions,
  solutionPackages, onPlanRollout, onAdvanceRollout, onRolloutHealth, onPromoteRollout, onRollbackRollout,
  rolloutError, processSpecs, selectedSpec, specReview, builderRounds, onSelectSpec, onBuilderTrial, onBuilderTrialResume,
  processDraft, onSaveSpec, onNewSpec, onSpecCandidate, specSaving, trialState, factoryError,
  processTemplates, templateError, onRefreshTemplates,
  reprobing = false }: CapabilityCenterPageProps) {
  const { t, locale } = useI18n()
  // Stopping a customer channel without saying why becomes a mystery a week
  // later; the server refuses a reasonless stop, and so does this form.
  const [stopping, setStopping] = useState<{ connector: string; reason: string }>({ connector: '', reason: '' })
  // Opening the desktop channel lets flow steps act on this computer: said once more before it is sent.
  const [confirmingDesktop, setConfirmingDesktop] = useState(false)
  const [settling, setSettling] = useState({ session: '', turn: '', reason: '' })
  // The desktop channel has its own block: a switch nobody set means closed there, while in the send switches it
  // means on -- listed among them it would read the wrong way round.
  const desktopSwitch = (connectorSwitches ?? []).find(item => item.connector === DESKTOP_CHANNEL)
  const desktopOpen = desktopChannelOpen(desktopSwitch)
  const sendSwitches = (connectorSwitches ?? []).filter(item => item.connector !== DESKTOP_CHANNEL)
  useEffect(() => {
    if (!focusDesktopChannel || !data) return
    const block = document.getElementById(DESKTOP_CHANNEL_ANCHOR)
    if (!block) return
    block.scrollIntoView({ block: 'start', behavior: 'smooth' })
    block.focus({ preventScroll: true })
    onDesktopChannelFocused?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one landing per request, not a subscription to the callback
  }, [focusDesktopChannel, data])
  // One reason form for every stop on this panel -- a send switch or the desktop channel: the server refuses a
  // reasonless one, and so does this form.
  const stopReasonForm = (name: string, testid: string, placeholder: string, confirm: string) => stopping.connector === name && (
    <span className="capability-reason-form" data-testid={`${testid}-reason`}>
      <input
        className="capability-refresh-btn"
        placeholder={placeholder}
        value={stopping.reason}
        onChange={event => setStopping({ connector: name, reason: event.target.value })}
      />
      <button
        type="button"
        className="workflows-cancel"
        data-testid={`${testid}-confirm`}
        disabled={!connected || !stopping.reason.trim()}
        onClick={() => { onSwitchConnector?.(name, false, stopping.reason); setStopping({ connector: '', reason: '' }) }}
      >
        {confirm}
      </button>
    </span>
  )
  const frontier = data?.frontier
  const policy = data?.runtime_policy
  const channels = data?.channels ?? []
  const connectors = data?.connectors ?? []
  const packages = data?.installed_packages ?? []
  const providers = data?.external_agent_profiles ?? []
  const permissions = data?.capability_permissions ?? []
  const skillCount = snapshot?.skills.total ?? 0
  const groupHidden = (name: CapabilityGroup) => (group && group !== name ? true : undefined)
  const agentCount = snapshot ? Object.keys(snapshot.agents).length : 0

  return (
    <main className="capability-center-page" data-embedded={group ? '' : undefined}>
      <div className="capability-center-header" hidden={group ? true : undefined}>
        <div>
          <div className="eyebrow">{t('capability.eyebrow')}</div>
          <h1>{t('capability.title')}</h1>
          <p>{t('capability.subtitle')}</p>
        </div>
        <button className="capability-refresh-btn" onClick={onRefresh} disabled={!connected || loading}>
          {loading ? t('capability.loading') : t('capability.refresh')}
        </button>
      </div>

      {!connected && <div className="capability-state capability-state--offline" role="status">{t('capability.offline')}</div>}
      {error && <div className={`capability-state capability-state--error capability-state--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}><span><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong><br />{error}</span><button onClick={onRefresh} disabled={!connected || loading}>{t('capability.retry')}</button></div>}
      {loading && !data && <div className="capability-empty">{t('capability.loading')}</div>}
      {!loading && !error && !data && <div className="capability-empty">{t('capability.empty')}</div>}

      {data && <>
      <section className="capability-grid" aria-label={t('capability.overview')} hidden={group ? true : undefined}>
        <article className="capability-card capability-card--hero">
          <div className="capability-card-kicker">{t('capability.runtime')}</div>
          <div className="capability-runtime-row"><strong>{stateLabel(data?.project_run?.status ?? data?.project_run?.lifecycle_status)}</strong><span className={connected ? 'capability-dot capability-dot--ok' : 'capability-dot'} />{connected ? t('capability.live') : t('capability.offline')}</div>
          <div className="capability-meta"><span>{t('capability.project')}</span><code>{projectId || 'default'}</code></div>
          <div className="capability-meta"><span>{t('capability.run')}</span><code>{stateLabel(data?.project_run?.run_id ?? frontier?.run_id)}</code></div>
        </article>
        <article className="capability-card"><div className="capability-card-kicker">{t('capability.assets')}</div><div className="capability-stat-row"><strong>{skillCount}</strong><span>{t('capability.skills')}</span></div><div className="capability-stat-row"><strong>{agentCount}</strong><span>{t('capability.agents')}</span></div><div className="capability-stat-row"><strong>{packages.length}</strong><span>{t('capability.packages')}</span></div></article>
        <article className="capability-card"><div className="capability-card-kicker">{t('capability.frontier')}</div><div className="capability-stat-row"><strong>{frontier?.running_count ?? 0}</strong><span>{t('capability.running')}</span></div><div className="capability-stat-row"><strong>{frontier?.blocked_count ?? 0}</strong><span>{t('capability.blocked')}</span></div><div className="capability-stat-row"><strong>{frontier?.done_count ?? 0}</strong><span>{t('capability.done')}</span></div></article>
      </section>

      <section className="capability-columns">
        <div className="capability-group" data-group="connect" hidden={groupHidden('connect')}>
        <article className="capability-panel"><div className="capability-panel-title"><h2>{t('capability.connectors')}</h2><span>{connectors.length}</span></div>{connectors.length === 0 ? <div className="capability-muted">{t('capability.none')}</div> : <div className="capability-list">{connectors.map(connector => <div className="capability-list-item capability-list-item--stacked" key={connector.connector_id}><div><strong>{connector.name}</strong><small>{connector.connector_type}</small><small className="capability-detail">{connector.actions.length > 0 ? connector.actions.join(' · ') : t('capability.noActions')}</small></div><span className="capability-pill">{connector.actions.length} {t('capability.actions')}</span></div>)}</div>}</article>
        {connectorCandidates && connectorRegistrations && onRefreshConnectorCandidates && (
          <ConnectorCandidatesPanel
            candidates={connectorCandidates}
            registrations={connectorRegistrations}
            connected={connected}
            error={connectorCandidateError}
            onRefresh={onRefreshConnectorCandidates}
            onTest={onTestConnectorCandidate}
            onReview={onReviewConnectorCandidate}
            onRegister={onRegisterConnectorCandidate}
            onRevoke={onRevokeConnectorCandidate}
          />
        )}
        </div>
        <div className="capability-group" data-group="execution" hidden={groupHidden('execution')}>
        {browserSessions && onRefreshBrowserSessions && (
          <BrowserSessionsPanel
            sessions={browserSessions}
            connected={connected}
            error={browserSessionError}
            accountInspection={browserAccountInspection}
            onInspectAccount={onInspectBrowserAccount}
            locale={locale}
            limits={browserLimits}
            combination={browserCombination}
            trail={browserTrail}
            lookBackNote={browserLookBackNote}
            auditExportNote={browserAuditExportNote}
            screenshot={browserScreenshot}
            liveFrame={browserLiveFrame}
            liveBinding={browserLiveBinding}
            onViewStart={onBrowserViewStart}
            onViewStop={onBrowserViewStop}
            onViewInput={onBrowserViewInput}
            onRefresh={onRefreshBrowserSessions}
            onControl={onControlBrowserSession}
            onAuditTrail={onBrowserAuditTrail}
            onAuditExport={onBrowserAuditExport}
            onLookBack={onBrowserLookBack}
            onScreenshot={onBrowserScreenshot}
            candidates={browserCandidates}
            candidateNote={browserCandidateNote}
            sealedDemo={browserSealedDemo}
            onCandidate={onBrowserCandidate}
          />
        )}
        </div>
        <div className="capability-group" data-group="release" hidden={groupHidden('release')}>
        {deployments && (
          <DeploymentsPanel
            deployments={deployments}
            rollouts={rollouts ?? []}
            rolloutsHasMore={rolloutsHasMore === true}
            packages={solutionPackages ?? []}
            connected={connected}
            error={rolloutError}
            onPlanRollout={onPlanRollout}
            onAdvance={onAdvanceRollout}
            onHealth={onRolloutHealth}
            onPromote={onPromoteRollout}
            onRollback={onRollbackRollout}
          />
        )}
        {processSpecs && (
          <ProcessFactoryPanel
            specs={processSpecs}
            selected={selectedSpec}
            review={specReview}
            rounds={builderRounds}
            connected={connected}
            onSelect={onSelectSpec}
            onTrial={onBuilderTrial}
            onResume={onBuilderTrialResume}
            draft={processDraft}
            onSave={onSaveSpec}
            onNew={onNewSpec}
            onCandidate={onSpecCandidate}
            templates={processTemplates}
            templateError={templateError}
            onRefreshTemplates={onRefreshTemplates}
            saving={specSaving}
            trialState={trialState}
            error={factoryError}
          />
        )}
        </div>
        <div className="capability-group" data-group="connect" hidden={groupHidden('connect')}>
        {credentialReferences && credentialReferences.length > 0 && (
          <article className="capability-panel" data-testid="credential-references">
            <div className="capability-panel-title">
              <h2>{t('capability.credentials')}</h2>
              <span>{credentialReferences.length}</span>
            </div>
            <div className="capability-muted">{t('capability.credentialsHint')}</div>
            <div className="capability-list">
              {credentialReferences.map(reference => (
                <div className="capability-list-item" key={`${reference.purpose}-${reference.customer_id}-${reference.environment_id}`}>
                  <div>
                    <strong>{t(`capability.purpose.${reference.purpose}` as Parameters<typeof t>[0])}</strong>
                    <small>
                      {reference.customer_id
                        ? `${reference.customer_id}${reference.environment_id ? ` · ${reference.environment_id}` : ''}`
                        : t('capability.machineDefault')}
                    </small>
                    <small className="capability-detail">{reference.env_var}</small>
                  </div>
                  <span className={`capability-pill capability-pill--${reference.is_set ? 'ok' : 'warn'}`}>
                    {reference.is_set ? t('capability.varSet') : t('capability.varMissing')}
                  </span>
                </div>
              ))}
            </div>
          </article>
        )}
        </div>
        <div className="capability-group" data-group="safety" hidden={groupHidden('safety')}>
        {((pendingTurns ?? []).length > 0 || pendingTurnError) && (
          <article className="capability-panel" data-testid="pending-turns">
            <div className="capability-panel-title"><h2>未结回合</h2><span>{(pendingTurns ?? []).length}</span></div>
            <div className="capability-muted">这些回合尚无结束记录，可能仍在执行，也可能等待核验。不要重发。中断只提出停止请求，不代表已经停止；人工结清会记录为失败、结果未知，并记下原因，不等于回滚或验收通过。</div>
            {pendingTurnError && <p className="capability-detail capability-detail--warn" role="alert" data-testid="pending-turn-error">{pendingTurnError}</p>}
            {(pendingTurns ?? []).map(item => (
              <div className="capability-list-item" key={`${item.session_id}:${item.turn_id}`} data-testid="pending-turn-row">
                <div>
                  <strong>{item.task_id || item.run_id || item.session_id}</strong>
                  <small className="capability-detail">{item.provider} · 会话 {item.session_id} · 回合 {item.turn_id} · 开始于 {item.started_at} · {pendingTurnLocation(item)}</small>
                  {item.outcome_status === 'dispatch_unknown' && <p className="capability-detail capability-detail--warn" data-testid="pending-turn-dispatch-unknown">启动回执未知，可能仍在执行；先核验，勿重发。</p>}
                  {turnLedger && turnLedger.session_id === item.session_id && turnLedger.turn_id === item.turn_id && (
                    <div className="capability-detail" data-testid="pending-turn-ledger">
                      {turnLedger.ledger === 'none'
                        ? <p className="capability-detail capability-detail--warn" data-testid="pending-turn-ledger-none">这个回合没有动作记录：{turnLedger.provider === 'codex_sdk' ? '记录中断之前没有任何动作被写下。' : '该 Provider 在回合中途不上报任何动作，它做了什么只能看工作区本身。'}空列表不代表什么都没做。</p>
                        : (
                          <ol>
                            {turnLedger.actions.map((action, index) => (
                              <li key={index} data-testid="pending-turn-ledger-action" data-state={action.state}>
                                <strong>{LEDGER_STATE_TEXT[action.state] ?? action.state}</strong> · {ledgerActionText(action)}
                              </li>
                            ))}
                          </ol>
                        )}
                      {turnLedger.ledger !== 'none' && (turnLedger.unpaired_rows ?? 0) > 0 && <p className="capability-detail capability-detail--warn">另有 {turnLedger.unpaired_rows} 条旧格式记录无法配对，未列入。</p>}
                      {turnLedger.workspace_now && (
                        <p className="capability-detail capability-detail--warn" data-testid="pending-turn-workspace-now">
                          {turnLedger.workspace_now.compared
                            ? `这个回合没有结束，所以没人在它之后对比过工作区。现在对比回合开始时的基线：${changeList(turnLedger.workspace_now.changes) || '无变化'}${turnLedger.workspace_now.changes_truncated ? ` ……（共 ${turnLedger.workspace_now.total} 处，已截断）` : ''}。注意「现在」不是「回合停下的那一刻」：此后任何人写进这个目录的东西也在其中。`
                            : `无法与回合开始时对比：${turnLedger.workspace_now.reason ?? ''}`}
                        </p>
                      )}
                    </div>
                  )}
                  {pendingTurnSelectionMatches(item, settling) && canSettlePendingTurn(item) && (
                    <span className="capability-reason-form" data-testid="pending-turn-reason">
                      <input
                        className="capability-refresh-btn"
                        placeholder="为什么结清（会写进记录）"
                        value={settling.reason}
                        onChange={event => setSettling({ session: item.session_id, turn: item.turn_id, reason: event.target.value })}
                      />
                      <button
                        type="button"
                        className="workflows-cancel"
                        data-testid="pending-turn-confirm"
                        disabled={!connected || !canSettlePendingTurn(item) || !pendingTurnSelectionMatches(item, settling) || settling.reason.trim().length < 4}
                        onClick={() => { if (canSettlePendingTurn(item) && pendingTurnSelectionMatches(item, settling)) onSettleTurn?.(item.session_id, settling.reason.trim(), settling.turn); setSettling({ session: '', turn: '', reason: '' }) }}
                      >
                        确认结清为失败
                      </button>
                    </span>
                  )}
                </div>
                <span>
                  <span className="capability-pill capability-pill--warn">{item.outcome_status === 'dispatch_unknown' ? '启动回执未知' : '尚未结案'}</span>
                  {onTurnActions && (
                    <button type="button" className="capability-refresh-btn" data-testid="pending-turn-actions" disabled={!connected} onClick={() => onTurnActions(item.session_id, item.turn_id)} title="只读：列出 Provider 自述在这个回合里做过的动作，哪些已完成、哪些没有完成记录。不结清、不重放。">它做了什么</button>
                  )}
                  {onInterruptTurn && (
                    <button type="button" className="capability-refresh-btn" data-testid="pending-turn-interrupt" disabled={!connected} onClick={() => onInterruptTurn(item.session_id, item.turn_id)} title="记录中断请求，由持有回合的运行时尝试处理。请求成功不代表已经停止，也不等于结清。">请求中断</button>
                  )}
                  {onSettleTurn && canSettlePendingTurn(item) && !pendingTurnSelectionMatches(item, settling) && (
                    <button type="button" className="workflows-cancel" data-testid="pending-turn-settle" disabled={!connected} onClick={() => setSettling({ session: item.session_id, turn: item.turn_id, reason: '' })}>由我结清</button>
                  )}
                </span>
              </div>
            ))}
          </article>
        )}
        </div>
        <div className="capability-group" data-group="channels" hidden={groupHidden('channels')}>
        <article className="capability-panel"><div className="capability-panel-title"><h2>{t('capability.channels')}</h2><span>{channels.length}</span>{onCheckChannels && <button className="capability-refresh-btn" data-testid="channel-config-check" onClick={onCheckChannels} disabled={!connected} title={t('capability.checkChannelsHint')}>{t('capability.checkChannels')}</button>}</div>
        {(channels.length > 0 || sendSwitches.length > 0) && (
          <div className="capability-channel-check" data-testid="connector-switches">
            <strong className="capability-block-title">{locale.startsWith('zh') ? '发送开关' : 'Send switches'}</strong>
            {Array.from(new Set([
              ...channels.map(channel => String(channel.name ?? '')),
              ...sendSwitches.map(entry => entry.connector),
            ])).filter(name => Boolean(name) && name !== DESKTOP_CHANNEL).map(name => {
              const entry = sendSwitches.find(item => item.connector === name)
              const off = entry ? !entry.enabled : false
              return (
                <div className="capability-list-item" key={name} data-testid="connector-switch-row">
                  <div>
                    <strong>{name}</strong>
                    {off && (
                      <small className="capability-detail capability-detail--warn">
                        {t('capability.switchedOffBy', { who: entry?.actor || '—', reason: entry?.reason || '—' })}
                      </small>
                    )}
                    {stopReasonForm(name, 'connector-stop', t('capability.stopWhy'), t('capability.stopConfirm'))}
                  </div>
                  <span>
                    {off && <span className="capability-pill capability-pill--warn">{t('capability.switchedOff')}</span>}
                    {onSwitchConnector && (off ? (
                      <button
                        type="button"
                        className="capability-refresh-btn"
                        data-testid="connector-resume"
                        disabled={!connected}
                        onClick={() => onSwitchConnector(name, true, '')}
                      >
                        {t('capability.resumeSending')}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="workflows-cancel"
                        data-testid="connector-stop"
                        disabled={!connected}
                        onClick={() => setStopping({ connector: name, reason: '' })}
                      >
                        {t('capability.stopSending')}
                      </button>
                    ))}
                  </span>
                </div>
              )
            })}
          </div>
        )}
        {channelCheck && (
          <div className="capability-channel-check" data-testid="channel-config-check-result">
            <strong className="capability-block-title">{locale.startsWith('zh') ? '配置形状检查' : 'Config shape check'}</strong>
            {channelCheck.muted && <div className="capability-detail capability-detail--warn">{t('capability.channelsMuted')}</div>}
            {channelCheck.channels.map(channel => (
              <div className="capability-list-item capability-list-item--stacked" key={channel.id}>
                <div>
                  <strong>{locale === 'zh-CN' ? channel.name_zh : channel.name_en}</strong>
                  {channel.fields.filter(f => f.detail_zh || f.detail_en).map(f => (
                    <small className="capability-detail capability-detail--warn" key={f.env}>
                      {(locale === 'zh-CN' ? f.label_zh : f.label_en)}: {(locale === 'zh-CN' ? f.detail_zh : f.detail_en)}
                    </small>
                  ))}
                  {channel.shape_ok && <small className="capability-detail capability-detail--ok">{t('capability.shapeOkHint')}</small>}
                </div>
                <span className={`capability-pill capability-pill--${channel.shape_ok ? 'ok' : 'warn'}`}>
                  {channel.shape_ok ? t('capability.shapeOk') : t('capability.shapeProblems', { n: channel.problems })}
                </span>
              </div>
            ))}
          </div>
        )}{channels.length === 0 ? <div className="capability-muted">{t('capability.none')}</div> : <div className="capability-list">{channels.map(channel => <div className="capability-list-item" key={channel.name}><div><strong>{channel.name}</strong><small>{channel.delivery_mode}</small></div><span className={`capability-pill capability-pill--${channel.ready ? 'ok' : channel.configured ? 'warn' : 'muted'}`}>{channel.ready ? t('capability.ready') : channel.configured ? t('capability.configured') : t('capability.notConfigured')}</span></div>)}</div>}</article>
        {/* 4.7-A06: the desktop channel is closed until the owner opens it here. Always shown, set or not: a closed
            channel nobody ever touched is exactly the one a desktop step in the builder needs opened. */}
        <article className="capability-panel" id={DESKTOP_CHANNEL_ANCHOR} tabIndex={-1} data-testid="desktop-channel" data-state={desktopOpen ? 'open' : 'closed'}>
          <div className="capability-panel-title">
            <h2>{t('capability.desktopChannel')}</h2>
            <span className={`capability-pill capability-pill--${desktopOpen ? 'ok' : 'muted'}`} data-testid="desktop-channel-state">
              {desktopOpen ? t('capability.desktopChannelOn') : t('capability.desktopChannelOff')}
            </span>
          </div>
          <div className="capability-muted">{t('capability.desktopChannelWhat')}</div>
          <div className="capability-list-item">
            <div>
              <small className="capability-detail" data-testid="desktop-channel-last">
                {desktopOpen
                  ? t('capability.desktopChannelOpenedBy', { who: desktopSwitch?.actor || '—', at: switchTime(desktopSwitch?.at ?? '') })
                  : desktopSwitch
                    ? t('capability.desktopChannelClosedBy', { who: desktopSwitch.actor || '—', reason: desktopSwitch.reason || '—' })
                    : t('capability.desktopChannelNeverSet')}
              </small>
              {/* The console is not told whether this computer's desktop helper is connected: said as it is. */}
              <small className="capability-detail capability-detail--warn" data-testid="desktop-channel-helper">{t('capability.desktopChannelHelper')}</small>
              {onSwitchConnector && !desktopOpen && confirmingDesktop && (
                <div className="capability-channel-check" role="group" aria-label={t('capability.desktopChannelConfirmTitle')} data-testid="desktop-channel-confirm">
                  <strong className="capability-block-title">{t('capability.desktopChannelConfirmTitle')}</strong>
                  <small className="capability-detail">{t('capability.desktopChannelConfirmBody')}</small>
                  <span>
                    <button
                      type="button"
                      className="capability-refresh-btn"
                      data-testid="desktop-channel-open-confirm"
                      disabled={!connected}
                      onClick={() => { openDesktopChannel(onSwitchConnector); setConfirmingDesktop(false) }}
                    >
                      {t('capability.desktopChannelConfirmOpen')}
                    </button>
                    <button type="button" className="workflows-cancel" data-testid="desktop-channel-open-cancel" onClick={() => setConfirmingDesktop(false)}>
                      {t('capability.desktopChannelCancel')}
                    </button>
                  </span>
                </div>
              )}
              {desktopOpen && stopReasonForm(DESKTOP_CHANNEL, 'desktop-channel-close', t('capability.desktopChannelCloseWhy'), t('capability.desktopChannelCloseConfirm'))}
            </div>
            <span>
              {onSwitchConnector && (desktopOpen ? (
                <button
                  type="button"
                  className="workflows-cancel"
                  data-testid="desktop-channel-close"
                  disabled={!connected}
                  onClick={() => setStopping({ connector: DESKTOP_CHANNEL, reason: '' })}
                >
                  {t('capability.desktopChannelClose')}
                </button>
              ) : !confirmingDesktop && (
                <button
                  type="button"
                  className="capability-refresh-btn"
                  data-testid="desktop-channel-open"
                  disabled={!connected}
                  onClick={() => setConfirmingDesktop(true)}
                >
                  {t('capability.desktopChannelOpen')}
                </button>
              ))}
              {/* An account that may not switch it (App passes no callback): said, not a button the server refuses. */}
              {!onSwitchConnector && (
                <small className="capability-detail" data-testid="desktop-channel-owner-only">
                  {desktopOpen ? t('capability.desktopChannelOwnerOnlyClose') : t('capability.desktopChannelOwnerOnlyOpen')}
                </small>
              )}
            </span>
          </div>
        </article>
        </div>
        <div className="capability-group" data-group="safety" hidden={groupHidden('safety')}>
        {governancePolicy !== undefined ? <GovernancePolicyPanel view={governancePolicy} connected={connected} error={governanceError} onRefresh={onRefreshGovernancePolicy} onSet={onSetGovernancePolicy} /> : <article className="capability-panel"><div className="capability-panel-title"><h2>{t('capability.governance')}</h2><span>{data?.org_version ? `v${data.org_version}` : '—'}</span></div><div className="capability-policy-grid"><span>{t('capability.autoDispatch')}</span><b>{policy?.parallel?.auto_dispatch ? t('capability.enabled') : t('capability.disabled')}</b><span>{t('capability.maxWorkers')}</span><b>{policy?.parallel?.max_workers ?? '—'}</b><span>{t('capability.humanOverride')}</span><b>{policy?.review?.allow_human_override ? t('capability.enabled') : t('capability.disabled')}</b></div></article>}
        <article className="capability-panel"><div className="capability-panel-title"><h2>权限证据</h2><span>{permissions.length}</span></div>{permissions.length === 0 ? <div className="capability-muted">暂无权限映射</div> : <div className="capability-list">{permissions.map(item => <div className="capability-list-item" key={item.action}><div><strong>{item.action}</strong></div><span className="capability-pill">{item.required_role}</span></div>)}</div>}</article>
        <article className="capability-panel"><div className="capability-panel-title"><h2>{t('capability.securityPosture')}</h2><span className="capability-pill capability-pill--ok">{data.security_posture?.mode ?? 'auto'}</span></div><div className="capability-policy-grid"><span>{t('capability.nativeApproval')}</span><b>{data.security_posture?.native_approval_level ?? 'auto'}</b><span>{t('capability.externalContent')}</span><b>{data.security_posture?.external_content ?? 'untrusted_external'}</b><span>{t('capability.approvalBoundary')}</span><b>{data.security_posture?.tool_approval_is_independent ? t('capability.independent') : t('capability.unverified')}</b></div></article>
        <article className="capability-panel capability-provider-panel"><div className="capability-panel-title"><h2>{t('capability.providers')}</h2><span>{providers.length}</span>{onReprobe && <button className="capability-refresh-btn" data-testid="capability-reprobe" onClick={onReprobe} disabled={!connected || reprobing} title={t('capability.reprobeHint')}>{reprobing ? t('capability.reprobing') : t('capability.reprobe')}</button>}</div>{providers.length === 0 ? <div className="capability-muted">{t('capability.none')}</div> : <div className="capability-list">{providers.map((provider, index) => { const profile = provider as Record<string, unknown>; const conformance = (profile.conformance ?? {}) as Record<string, unknown>; const ready = conformance.production_ready === true; const available = profile.available === true; const blockers = Array.isArray(conformance.blockers) ? conformance.blockers.map(String).filter(Boolean) : []; return <div className="capability-list-item capability-list-item--stacked" key={`${String(profile.agent ?? 'provider')}-${index}`}><div><strong>{String(profile.agent ?? 'provider')}</strong><small>{available ? t('capability.available') : t('capability.unavailable')}</small>{blockers.length > 0 && <small className="capability-detail capability-detail--warn">{t('capability.blockers')}: {blockers.join(' · ')}</small>}{blockers.length === 0 && ready && <small className="capability-detail capability-detail--ok">{t('capability.noBlockers')}</small>}</div><span className={`capability-pill capability-pill--${ready ? 'ok' : 'warn'}`}>{ready ? t('capability.productionReady') : t('capability.unverified')}</span></div> })}</div>}</article>
        </div>
      </section>
      </>}
    </main>
  )
}
