import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { useI18n } from '../i18n'
import {
  COMPONENT_GROUPS,
  COMPONENT_STATES,
  componentRows,
  countComponentStates,
  currentDraftOf,
  draftTargetFor,
  filterComponentRows,
  flowTargetsFor,
  groupComponentRows,
  type BuiltInComponent,
  type CatalogConnector,
  type CatalogSkill,
  type ComponentCatalog,
  type ComponentGroup,
  type ComponentRow,
  type ComponentRowKind,
  type ComponentState,
  type FlowComponent,
  type InternalConnector,
  type InternalSkill,
} from '../lib/componentCatalog'
import type { WorkflowSummary } from './WorkflowsPage'
import { catalogTrialState, trialForListing, type TrialSkill } from '../lib/skillMarket'
import { keyOf, type Tracked } from '../lib/requestTracker'
import { FlowPicker } from './FlowPicker'
import { SkillMarketPanel, type SkillMarketPanelProps } from './SkillMarketPanel'
import { ComponentDetailPanel, type ComponentDetailView } from './ComponentDetailPanel'
import type { ComponentWorkbenchProps, WorkbenchTab } from './ComponentWorkbench'
import { ImprovementProposalActions, type ImprovementProposalActionHandlers } from './ImprovementProposalActions'
import { PROPOSAL_STATUS_WORDS, type ImprovementBenefit, type ImprovementTarget } from '../lib/improvementProposal'

// 组件库 = 一列组件 (frontend redesign step 3, page 5; policy, FRONTEND-IA- 「组件库」 + 「第二轮定稿」
// row 组件库). One row per component, grouped 能力 / 平台 / 行业 / 业务流 (lib/componentCatalog componentRows); each row
// is name · kind · one state word · ONE verb by that state (加进草稿 / 去配置 / 查看), with 查看 / 编辑 / 测试 and the rest
// folded under 更多. The header says which draft 加进草稿 goes to; 让安安起草一个 sends the ordinary drafting request
// through 总控's conversation. The detail panel keeps its four pages and the whole publish chain -- the gates of the
// chain stay in the editor, never on the list. Nothing here marks a component ready without the server's field for it.

/** The list's sections: every group, one group, or the online market (when the console offers it). */
export type ComponentLibrarySection = 'all' | ComponentGroup | 'market' | 'employees'

export interface SkillImprovement {
  candidate_id: string
  candidate_fingerprint: string
  skill_id: string
  version: string
  current_version: string
  status: string
  created_at: string
  tested_at: string
  reviewed_at: string
  evaluation_spec_fingerprint: string
  evaluation_bound: boolean
  activation: null | {
    candidate_id: string
    candidate_fingerprint: string
    skill_id: string
    deployment_fingerprint: string
    status: string
    staged_at: string
    activated_at: string
    rolled_back_at: string
  }
}

export interface ImprovementProposal {
  proposal_id: string
  proposal_fingerprint: string
  status: string
  title: string
  target: { kind?: string; id?: string; current_fingerprint?: string }
  candidate_kind: string
  created_at: string
  created_by: string
  evaluation_spec_fingerprint: string
  evaluation_verdict: string
  /** 4.7-A11: where the candidate is, what real orders said, and the decision's reason (absent on older rows). */
  candidate_id?: string
  candidate_status?: string
  benefit?: ImprovementBenefit | null
  decision_reason?: string
}

/** One received distribution candidate and the owner's decision on it (P5-09). */
export interface DistributionInboxRecord {
  workflow_id: string
  version: string
  candidate_fingerprint: string
  status: 'accepted' | 'rejected' | string
  decided_by?: string
  decided_at?: string
  note?: string
  draft_id?: string
}

export interface ComponentLibraryPageProps {
  catalog: ComponentCatalog | null
  /** The workflow list the console already serves; null until it has arrived. */
  workflows?: WorkflowSummary[] | null
  connected: boolean
  loading?: boolean
  error?: string | null
  onRefresh: () => void
  initialSection?: ComponentLibrarySection
  /** Existing owner asset host; absent means this surface has no AI employee entry. */
  employeeContent?: ReactNode
  /** 员工交接 (4.8): shown above the AI-employee library when that section is open. */
  handoffContent?: ReactNode
  /** The SkillHub market and trial area; absent where the console cannot offer it (then only the local catalog). */
  market?: SkillMarketPanelProps | null
  /** 加进草稿 / 加进别的业务流: open the chosen 业务流 on the builder with this component on the shelf. Absent = no such verb. */
  onUseComponent?: (component: BroughtComponentRef, workflowId: string) => void
  /** 查看: ask the console for one internal skill's read-only detail. Absent = no 查看 for internal skills. */
  onViewComponent?: (componentId: string) => void
  /** The detail asked for last, while it is open. */
  componentDetail?: ComponentDetailView | null
  onCloseComponentDetail?: () => void
  /** 编辑 / 测试 / 去配置 (4.7 A2-4): open one internal skill's detail on its 契约 or 测试 page, with the version chain. */
  onWorkComponent?: (componentId: string, tab: WorkbenchTab) => void
  /** The version chain for the detail open in 编辑 / 测试 mode; null when the detail is only being looked at. */
  componentWorkbench?: ComponentWorkbenchProps | null
  /** 调用测试 on a local-catalog skill: import it from the local resource library into the trial area. */
  onTryCatalogSkill?: (skill: CatalogSkill) => void
  /** Open the trial area on a trial's card. */
  onOpenTrial?: (trial: TrialSkill) => void
  /** Owner-reviewed RSI candidates. The model may propose; only these explicit controls can change the live Skill. */
  skillImprovements?: SkillImprovement[] | null
  improvementProposals?: ImprovementProposal[] | null
  skillImprovementBusy?: string
  skillImprovementError?: string | null
  onRefreshSkillImprovements?: () => void
  onStageSkillImprovement?: (candidate: SkillImprovement) => void
  onActivateSkillImprovement?: (candidate: SkillImprovement) => void
  onRollbackSkillImprovement?: (candidate: SkillImprovement) => void
  /** P5-09, owner only. Export a published, shareable flow as a signed candidate; absent = no export entry. */
  onExportDistribution?: (workflowId: string) => void
  /** Verify a received candidate file and record accept/reject; absent = no inbox panel. */
  onReceiveDistribution?: (path: string, decision: 'accept' | 'reject', note: string) => void
  onRefreshDistributionInbox?: () => void
  distributionInbox?: DistributionInboxRecord[] | null
  /** 4.7-A16: the builder step a skill was brought here from to be edited; with onReturnToStep, the page offers
   *  回到刚才的步骤. Absent when the page was opened any other way. */
  returnToStep?: { stepName: string; flowName: string } | null
  onReturnToStep?: () => void
  /** 4.7-A11: evaluate on real orders, approve, open the candidate, and the run-session trial, per proposal. Absent =
   *  the proposal list stays read only. */
  improvementActions?: ImprovementProposalActionHandlers | null
  /** 4.7-A11 提一个改进 on an internal skill's detail. Absent = no such control. */
  improvementStarter?: ((target: ImprovementTarget) => ReactNode) | null
  /** S3-5: the flow the builder holds (App's nodeDraftState); 加进草稿 goes there when it is a draft or a testing package. */
  currentDraftId?: string | null
  /** S3-5: open a flow on the builder (去配置 on a flow, 去搭建台 in the header, 到搭建台打开 under 更多). Absent = not offered. */
  onOpenFlowOnBuilder?: (workflowId: string) => void
  /** S3-5 让安安起草一个: send the drafting request through the ordinary conversation and open 总控. Absent = no such button. */
  onDraftWithAssistant?: (request: string) => void
  /** S3-5: what is out or unconfirmed (lib/requestTracker): a 查看 / 编辑 that got no receipt says so on the panel. */
  tracked?: Tracked
  /** The row to open on first paint (a row key `${kind}:${id}`); '' for none. */
  initialOpenRow?: string
}

/** A component taken from the library to the builder. */
export interface BroughtComponentRef {
  kind: 'skill'
  id: string
  name: string
}

type Translate = ReturnType<typeof useI18n>['t']
type TranslateMaybe = ReturnType<typeof useI18n>['translateMaybe']
type Key = Parameters<Translate>[0]

/** 加进别的业务流: the 业务流 an internal skill can go to, each saying whether the builder can take it there now. */
export function UsePicker({ skill, workflows, onPick, onClose, t }: {
  skill: InternalSkill; workflows: readonly WorkflowSummary[]; onPick: (workflowId: string) => void; onClose: () => void; t: Translate
}) {
  return <FlowPicker title={t('components.usePick', { name: skill.name })} targets={flowTargetsFor(skill, workflows)} onPick={onPick} onClose={onClose} t={t} />
}

function SkillImprovementPanel({ items, connected, busy, error, onRefresh, onStage, onActivate, onRollback, t }: {
  items: SkillImprovement[] | null; connected: boolean; busy?: string; error?: string | null
  onRefresh?: () => void
  onStage?: (candidate: SkillImprovement) => void
  onActivate?: (candidate: SkillImprovement) => void
  onRollback?: (candidate: SkillImprovement) => void
  t: Translate
}) {
  return (
    <div className="cl-section" data-testid="skill-improvements">
      <div className="cl-card-top">
        <div>
          <h2 className="cl-subhead">{t('components.improvements')}</h2>
          <p className="cl-aside">{t('components.improvementsHint')}</p>
        </div>
        {onRefresh && <button type="button" className="capability-refresh-btn" disabled={!connected || Boolean(busy)} onClick={onRefresh}>{t('components.refresh')}</button>}
      </div>
      {error && <p className="cl-alert" role="alert">{error}</p>}
      {items === null ? <p className="cl-empty">{t('components.loading')}</p> : items.length === 0 ? <p className="cl-empty">{t('components.improvementsEmpty')}</p> : (
        <div className="cl-grid">
          {items.map(item => {
            const activation = item.activation
            const actionBusy = busy === item.candidate_id
            return (
              <article className="cl-card cl-card--internal" key={item.candidate_id} data-testid="skill-improvement" data-status={activation?.status ?? item.status}>
                <div className="cl-card-top">
                  <strong>{item.skill_id}</strong>
                  <span className={`cl-chip ${item.status === 'approved' ? 'cl-chip--ok' : 'cl-chip--muted'}`}>{t(`components.improvementStatus.${item.status}` as Key)}</span>
                </div>
                <small className="cl-meta">v{item.current_version} → v{item.version}</small>
                <div className="cl-chips">
                  <span className={`cl-chip ${item.evaluation_bound ? 'cl-chip--ok' : 'cl-chip--warn'}`}>{item.evaluation_bound ? t('components.evaluationBound') : t('components.evaluationMissing')}</span>
                  {activation && <span className="cl-chip">{t(`components.activationStatus.${activation.status}` as Key)}</span>}
                </div>
                <div className="cl-actions">
                  {!activation && item.status === 'approved' && onStage && <button type="button" className="capability-refresh-btn" disabled={!connected || actionBusy} onClick={() => onStage(item)}>{t('components.stage')}</button>}
                  {activation?.status === 'staged' && onActivate && <button type="button" className="capability-refresh-btn" disabled={!connected || actionBusy} onClick={() => onActivate(item)}>{t('components.activate')}</button>}
                  {activation?.status === 'active' && onRollback && <button type="button" className="capability-refresh-btn" disabled={!connected || actionBusy} onClick={() => onRollback(item)}>{t('components.rollback')}</button>}
                </div>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ImprovementProposalPanel({ items, connected, error, onRefresh, t, actions = null, zh = true }: {
  items: ImprovementProposal[] | null; connected: boolean; error?: string | null
  onRefresh?: () => void; t: Translate
  actions?: ImprovementProposalActionHandlers | null
  zh?: boolean
}) {
  return (
    <div className="cl-section" data-testid="improvement-proposals">
      <div className="cl-card-top">
        <div>
          <h2 className="cl-subhead">{t('components.proposals')}</h2>
          <p className="cl-aside">{t('components.proposalsHint')}</p>
        </div>
        {onRefresh && <button type="button" className="capability-refresh-btn" disabled={!connected} onClick={onRefresh}>{t('components.refresh')}</button>}
      </div>
      {error && <p className="cl-alert" role="alert">{error}</p>}
      {items === null ? <p className="cl-empty">{t('components.loading')}</p> : items.length === 0 ? <p className="cl-empty">{t('components.proposalsEmpty')}</p> : (
        <div className="cl-grid">
          {items.map(item => {
            const targetKind = String(item.target?.kind ?? '')
            const targetId = String(item.target?.id ?? '')
            const evaluated = Boolean(item.evaluation_verdict)
            return (
              <article className="cl-card cl-card--internal" key={item.proposal_id} data-testid="improvement-proposal" data-status={item.status}>
                <div className="cl-card-top">
                  <strong>{item.title || targetId}</strong>
                  <span className={`cl-chip ${evaluated ? 'cl-chip--ok' : 'cl-chip--muted'}`}>{item.status === 'approved' ? PROPOSAL_STATUS_WORDS.approved[zh ? 0 : 1] : t(`components.proposalStatus.${item.status}` as Key)}</span>
                </div>
                <small className="cl-meta">{targetKind || '—'} · {targetId || '—'}{item.candidate_kind ? ` · ${item.candidate_kind}` : ''}</small>
                <div className="cl-chips">
                  <span className={`cl-chip ${item.evaluation_spec_fingerprint ? 'cl-chip--ok' : 'cl-chip--warn'}`}>
                    {item.evaluation_spec_fingerprint ? t('components.evaluationFrozen') : t('components.evaluationNotFrozen')}
                  </span>
                  {evaluated && <span className={`cl-chip ${item.evaluation_verdict === 'passed' ? 'cl-chip--ok' : 'cl-chip--warn'}`}>
                    {t(`components.evaluationVerdict.${item.evaluation_verdict}` as Key)}
                  </span>}
                </div>
                <small className="cl-line">{t('components.proposalNoAutoApply')}</small>
                {actions && <ImprovementProposalActions zh={zh} row={item} props={actions} />}
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** Received distribution candidates: verify a file, decide once, and see what was decided. An accepted
 *  candidate lands only as a draft that still has to walk the ordinary release gates. */
function DistributionInboxPanel({ inbox, connected, onReceive, onRefresh, t }: {
  inbox: DistributionInboxRecord[] | null
  connected: boolean
  onReceive: (path: string, decision: 'accept' | 'reject', note: string) => void
  onRefresh?: () => void
  t: Translate
}) {
  const [path, setPath] = useState('')
  const [note, setNote] = useState('')
  const ready = connected && path.trim().length > 0
  return (
    <section className="cl-section" data-testid="distribution-inbox">
      <div className="cl-card-top">
        <strong>{t('components.distribution.inboxTitle')}</strong>
        {onRefresh && <button type="button" className="cl-link" disabled={!connected} onClick={onRefresh}>{t('components.refresh')}</button>}
      </div>
      <small className="cl-meta">{t('components.distribution.inboxHint')}</small>
      <div className="cl-distribution-form">
        <input aria-label={t('components.distribution.pathLabel')} placeholder={t('components.distribution.pathLabel')} value={path} onChange={event => setPath(event.target.value)} />
        <input aria-label={t('components.distribution.noteLabel')} placeholder={t('components.distribution.noteLabel')} value={note} onChange={event => setNote(event.target.value)} />
        <button type="button" data-testid="distribution-accept" disabled={!ready} onClick={() => onReceive(path.trim(), 'accept', note.trim())}>{t('components.distribution.accept')}</button>
        <button type="button" data-testid="distribution-reject" disabled={!ready} onClick={() => onReceive(path.trim(), 'reject', note.trim())}>{t('components.distribution.reject')}</button>
      </div>
      {inbox === null
        ? <p className="cl-empty">{t('components.loading')}</p>
        : inbox.length === 0
          ? <p className="cl-empty">{t('components.distribution.inboxEmpty')}</p>
          : (
            <ul className="cl-distribution-list">
              {inbox.map(record => (
                <li key={`${record.workflow_id}:${record.candidate_fingerprint}`} data-testid="distribution-record" data-status={record.status}>
                  <strong>{record.workflow_id}</strong> · v{record.version} · {record.candidate_fingerprint.slice(0, 12)}
                  {' · '}{t(`components.distribution.status.${record.status === 'accepted' ? 'accepted' : 'rejected'}` as Key)}
                  {record.draft_id ? ` · ${t('components.distribution.landedAs', { draft: record.draft_id })}` : ''}
                  {record.decided_by ? ` · ${record.decided_by}` : ''}
                </li>
              ))}
            </ul>
          )}
    </section>
  )
}

// ── what opens under a row ─────────────────────────────────────────────────────────────────────────────────────

function RiskBadge({ level, t }: { level: string; t: Translate }) {
  return <span className={`cl-risk cl-risk--${level}`}>{t(`components.risk.${level}` as Key)}</span>
}

/** An internal skill: its purpose, what its contract is missing (on the row, where it can be read and copied --
 *  a title attribute is invisible on a touch screen), where it came from, and the flows that use it. */
function InternalSkillDetail({ skill, t, picker }: { skill: InternalSkill; t: Translate; picker: ReactNode }) {
  return (
    <>
      {skill.summary && <p className="cl-summary">{skill.summary}</p>}
      {!skill.contract_complete && <small className="cl-line cl-note" data-testid="internal-skill-why">{t('components.list.whyNeedsSetup')}</small>}
      {skill.problems.length > 0 && (
        <div className="cl-chips" data-testid="internal-skill-problems">
          {skill.problems.map(problem => <span className="cl-chip cl-chip--warn" key={problem}>{problem}</span>)}
        </div>
      )}
      {skill.admission?.market && (
        <small className="cl-line">{t('components.admittedFrom', { market: skill.admission.market, by: skill.admission.admitted_by || '—' })}</small>
      )}
      <div className="cl-chips">
        {skill.used_by.length === 0
          ? <span className="cl-chip cl-chip--muted">{t('components.unused')}</span>
          : skill.used_by.map(flow => (
            <span className="cl-chip" key={flow.id} title={flow.id}>{flow.name} · {t(`components.flowStatus.${flow.status}` as Key)}</span>
          ))}
      </div>
      {picker}
    </>
  )
}

function BuiltInDetail({ item, zh, t }: { item: BuiltInComponent; zh: boolean; t: Translate }) {
  return (
    <>
      <p className="cl-summary">{zh ? item.summary_zh : item.summary_en}</p>
      <small className="cl-line">{t('components.list.builtInHow')}</small>
    </>
  )
}

function RegisteredConnectorDetail({ item, t }: { item: InternalConnector; t: Translate }) {
  return (
    <>
      <small className="cl-meta">v{item.version} · {item.status}</small>
      <small className="cl-line">{item.enabled ? t('components.list.registeredOn') : t('components.list.registeredOff')}</small>
    </>
  )
}

function ConnectorDetail({ item, registered, domainName, t, tm }: { item: CatalogConnector; registered: InternalConnector | null; domainName: string; t: Translate; tm: TranslateMaybe }) {
  const { capability, governance } = item
  const effects = capability.side_effects
  return (
    <>
      <div className="cl-card-top">
        <small className="cl-meta">
          {domainName} · {item.group} · {tm('components.access', item.access_mode)}
          {item.inferred.includes('access_mode') && <em className="cl-inferred" title={t('components.inferredHint')}>{t('components.inferred')}</em>}
        </small>
        <RiskBadge level={governance.risk_level} t={t} />
      </div>
      <p className="cl-summary">{capability.summary}</p>
      {registered && <small className="cl-line">{registered.enabled ? t('components.list.registeredOn') : t('components.list.registeredOff')}</small>}
      <div className="cl-chips">
        {capability.readonly === true && <span className="cl-chip cl-chip--ok">{t('components.readonly')}</span>}
        {effects && effects.map(effect => <span className="cl-chip cl-chip--effect" key={effect}>{tm('components.effect', effect)}</span>)}
        {(capability.readonly === null || capability.readonly === undefined) && !effects?.length && (
          <span className="cl-chip cl-chip--muted">{t('components.capabilityUnknown')}</span>
        )}
      </div>
      <div className="cl-chips">
        {governance.reasons.map(reason => <span className="cl-chip cl-chip--reason" key={reason}>{tm('components.reason', reason)}</span>)}
        {governance.extra_human_review && <span className="cl-chip cl-chip--warn">{t('components.extraReview')}</span>}
      </div>
      <div className="cl-config" data-testid="connector-admission-plan">
        <span>{t('components.admissionRoute')}</span>
        <code className="cl-key">{tm('components.admissionRoute', item.admission_plan.route)}</code>
        <span className={item.admission_plan.state === 'blocked' ? 'cl-chip cl-chip--warn' : 'cl-chip cl-chip--ok'}>
          {tm('components.admissionState', item.admission_plan.state)}
        </span>
        <span>{t('components.evidenceCount', { count: item.admission_plan.required_evidence.length })}</span>
        {item.lifecycle && item.lifecycle.stage !== 'catalogued' && (
          <span className="cl-chip cl-chip--ok">{tm('components.lifecycle', item.lifecycle.stage)}</span>
        )}
      </div>
      {item.admission_plan.blockers.length > 0 && (
        <div className="cl-chips">
          {item.admission_plan.blockers.map(blocker => (
            <span className="cl-chip cl-chip--warn" key={blocker}>{tm('components.admissionBlocker', blocker)}</span>
          ))}
        </div>
      )}
      <small className="cl-line">{t('components.admissionEnforcement')}</small>
      {item.config_schema.length > 0 && (
        <div className="cl-config">
          <span>{t('components.config')}</span>
          {item.config_schema.map(key => (
            <code key={key.key} className={key.secret ? 'cl-key cl-key--secret' : 'cl-key'} title={key.secret ? t('components.secretHint') : undefined}>
              {key.key}{key.secret ? ` · ${t('components.secret')}` : ''}
            </code>
          ))}
        </div>
      )}
      {(item.install.command || item.install.url || item.install.where) && (
        // The install command is shown as a record, not offered as an action: the catalog installs and runs nothing.
        <code className="cl-install" title={t('components.installHint')}>{item.install.command || item.install.url || item.install.where}</code>
      )}
      {item.pricing.hint && <small className="cl-line">{t('components.pricing', { hint: item.pricing.hint })}</small>}
      {item.notes.map(note => <small className="cl-line cl-note" key={note}>{note}</small>)}
    </>
  )
}

function CatalogSkillDetail({ item, trial, t, tm }: { item: CatalogSkill; trial?: TrialSkill; t: Translate; tm: TranslateMaybe }) {
  const { governance, requires } = item
  const trialState = catalogTrialState(trial, item.source.skill_md_sha256)
  return (
    <>
      <div className="cl-card-top">
        <small className="cl-meta">{item.category}</small>
        <RiskBadge level={governance.risk_level} t={t} />
      </div>
      {item.summary && <p className="cl-summary">{item.summary}</p>}
      <div className="cl-chips">
        {governance.reasons.map(reason => (
          <span className="cl-chip cl-chip--reason" key={reason}>
            {tm('components.skillReason', reason)}
            {reason === 'per_use_payment' && item.pricing.price_hint ? ` ${item.pricing.price_hint}` : ''}
            {reason === 'bundled_code' && item.code_files ? t('components.codeFiles', { count: item.code_files }) : ''}
            {reason === 'needs_credentials' ? ` ${requires.env.join(' ')}` : ''}
            {reason === 'external_programs' ? ` ${requires.bins.join(' ')}` : ''}
          </span>
        ))}
      </div>
      <div className="cl-chips">
        {governance.admission_blockers.length === 0
          ? <span className="cl-chip cl-chip--ok">{t('components.admissionReady')}</span>
          : governance.admission_blockers.map(blocker => <span className="cl-chip cl-chip--warn" key={blocker}>{tm('components.blocker', blocker)}</span>)}
        {trial && <span className="cl-chip">{t(`market.trialStatus.${trial.status}` as Key)} · v{trial.version}</span>}
      </div>
      {(trialState === 'outdated' || trialState === 'outdated_admitted') && (
        <small className="cl-line" data-testid="catalog-trial-outdated">{t(trialState === 'outdated' ? 'market.catalogChanged' : 'market.catalogChangedAdmitted')}</small>
      )}
    </>
  )
}

function FlowDetail({ flow, t }: { flow: FlowComponent; t: Translate }) {
  return (
    <>
      <small className="cl-meta">{t('components.flowSteps', { steps: flow.steps, gates: flow.humanGates })}</small>
      {flow.summary && <p className="cl-summary">{flow.summary}</p>}
      <dl className="cl-flow-io">
        <dt>{t('components.flowInputs')}</dt>
        <dd>{flow.inputs.length ? flow.inputs.join(' · ') : '—'}</dd>
        <dt>{t('components.flowOutputs')}</dt>
        <dd>{flow.outputs.length ? flow.outputs.join(' · ') : (flow.calls.length ? t('components.flowOutputsViaCall') : '—')}</dd>
        {flow.calls.length > 0 && <><dt>{t('components.flowCalls')}</dt><dd>{flow.calls.join(' · ')}</dd></>}
        {flow.calledBy.length > 0 && <><dt>{t('components.flowCalledBy')}</dt><dd>{flow.calledBy.join(' · ')}</dd></>}
      </dl>
      {flow.status !== 'published' && <small className="cl-line cl-note">{t('components.list.whyCandidateFlow')}</small>}
      {flow.status === 'published' && !flow.invokable && <small className="cl-line cl-note">{t('components.list.whyNotInvokable')}</small>}
    </>
  )
}

// ── the list ───────────────────────────────────────────────────────────────────────────────────────────────────

/** The test ids each kind of row keeps from the cards it replaced (an internal skill's data-testid="internal-skill",
 *  a built-in's data-testid="builtin-component", ...), so the probes and pins that named them still find them. */
const ROW_TESTID: Record<ComponentRowKind, string> = {
  skill: 'internal-skill', built_in: 'builtin-component', connector: 'internal-connector',
  catalog_connector: 'catalog-connector', catalog_skill: 'catalog-skill', flow: 'flow-component',
}
/** The kind word on the row; a built-in says what it is (the browser universal connector is a 连接器), its subgroup says 内置. */
const KIND_WORD: Record<ComponentRowKind, 'skill' | 'built_in' | 'connector' | 'flow'> = {
  skill: 'skill', built_in: 'connector', connector: 'connector', catalog_connector: 'connector', catalog_skill: 'skill', flow: 'flow',
}
const kindWordOf = (row: ComponentRow) => (row.source.kind === 'built_in' && row.source.item.kind === 'skill' ? 'skill' : KIND_WORD[row.kind])

interface Verb { label: string; primary: boolean; disabled: boolean; why: string; onPress: () => void }
interface MoreItem { id: string; testid: string; label: string; disabled?: boolean; active?: boolean; onPress: () => void }

export function ComponentLibraryPage({
  catalog, workflows = null, connected, loading = false, error, onRefresh, initialSection = 'all', employeeContent = null, handoffContent = null,
  market = null, onUseComponent, onViewComponent, componentDetail = null, onCloseComponentDetail, onWorkComponent, componentWorkbench = null, onTryCatalogSkill, onOpenTrial,
  onExportDistribution, onReceiveDistribution, onRefreshDistributionInbox, distributionInbox,
  skillImprovements, improvementProposals, skillImprovementBusy, skillImprovementError, onRefreshSkillImprovements,
  onStageSkillImprovement, onActivateSkillImprovement, onRollbackSkillImprovement,
  returnToStep = null, onReturnToStep, improvementActions = null, improvementStarter = null,
  currentDraftId = null, onOpenFlowOnBuilder, onDraftWithAssistant, tracked = {}, initialOpenRow = '',
}: ComponentLibraryPageProps) {
  const { t, locale, translateMaybe: tm } = useI18n()
  const zh = locale.startsWith('zh')
  // 录入内部库 from the builder shelf or a market card: the market's trial area, where that card is.
  const [section, setSection] = useState<ComponentLibrarySection>(market?.focus ? 'market' : initialSection === 'employees' && !employeeContent ? 'all' : initialSection)
  useEffect(() => { if (section === 'employees' && !employeeContent) setSection('all') }, [section, employeeContent])
  const focusSeq = market?.focus?.seq ?? 0
  useEffect(() => { if (focusSeq) setSection('market') }, [focusSeq])
  const [query, setQuery] = useState('')
  const [stateFilter, setStateFilter] = useState<ComponentState | ''>('')
  const [openRow, setOpenRow] = useState(initialOpenRow)
  const [picking, setPicking] = useState('')
  const [drafting, setDrafting] = useState(false)
  const [draftWhat, setDraftWhat] = useState('')

  const internal = catalog?.internal
  const builtIns = internal?.built_in ?? []
  const rows = useMemo(() => componentRows(catalog, workflows), [catalog, workflows])
  const draft = useMemo(() => currentDraftOf(workflows, currentDraftId), [workflows, currentDraftId])
  const filtering = query.trim().length > 0 || stateFilter !== ''
  const shown = useMemo(() => filterComponentRows(rows, { query, group: section === 'all' || section === 'market' || section === 'employees' ? '' : section, state: stateFilter }), [rows, query, section, stateFilter])
  const groups = useMemo(() => groupComponentRows(shown), [shown])
  const totals = useMemo(() => groupComponentRows(rows), [rows])
  const stateCounts = useMemo(() => countComponentStates(rows), [rows])
  const domainNames = useMemo(() => new Map((catalog?.domains ?? []).map(domain => [
    domain.id, locale === 'en' && domain.name_en ? domain.name_en : domain.name,
  ])), [catalog, locale])
  const totalOf = (group: ComponentGroup) => totals.find(item => item.group === group)?.rows.length ?? 0
  const tabs: ComponentLibrarySection[] = ['all', ...(employeeContent ? ['employees' as const] : []), ...COMPONENT_GROUPS.map(([group]) => group), ...(market ? ['market' as const] : [])]
  const countOf = (tab: ComponentLibrarySection) => tab === 'all' ? rows.length : tab === 'market' || tab === 'employees' ? null : totalOf(tab)

  // The detail's read got no receipt: the panel says so instead of reading for ever.
  const detailEntry = componentDetail ? (tracked[keyOf('component_detail', componentDetail.id)] ?? tracked[keyOf('component_versions', componentDetail.id)]) : undefined
  const receiptNote = detailEntry && (detailEntry.state === 'timeout' || detailEntry.state === 'unknown') ? detailEntry.words : null

  const toggleRow = (key: string) => setOpenRow(current => (current === key ? '' : key))

  // ONE verb by state. 加进草稿 goes to the draft in the header, or says why it cannot; 去配置 opens the skill's 契约 page
  // or the flow on the builder; 查看 is the console's read-only detail for an internal skill and the row's own detail
  // for everything else. A verb whose way is not wired on this console (not the owner's) degrades to 查看.
  const verbOf = (row: ComponentRow): Verb => {
    const view: Verb = { label: t('components.list.verb.view'), primary: false, disabled: false, why: '', onPress: () => toggleRow(row.key) }
    if (row.verb === 'add_to_draft' && row.source.kind === 'skill' && onUseComponent) {
      const skill = row.source.item
      const label = t('components.list.verb.add_to_draft')
      if (workflows === null) return { label, primary: true, disabled: true, why: t('components.loading'), onPress: () => undefined }
      const target = draftTargetFor(skill, workflows, currentDraftId)
      if (!draft || !target) return { label, primary: true, disabled: true, why: t('components.draft.noneShort'), onPress: () => undefined }
      if (target.state !== 'ok') return { label, primary: true, disabled: true, why: t(`components.useState.${target.state}` as Key), onPress: () => undefined }
      return { label, primary: true, disabled: false, why: '', onPress: () => onUseComponent({ kind: 'skill', id: skill.id, name: skill.name }, draft.id) }
    }
    if (row.verb === 'configure') {
      if (row.source.kind === 'skill' && onWorkComponent) return { ...view, label: t('components.list.verb.configure'), onPress: () => onWorkComponent(row.id, 'contract') }
      if (row.source.kind === 'flow' && onOpenFlowOnBuilder) return { ...view, label: t('components.list.verb.configure'), onPress: () => onOpenFlowOnBuilder(row.id) }
    }
    if (row.source.kind === 'skill' && onViewComponent) return { ...view, onPress: () => onViewComponent(row.id) }
    return view
  }

  // 更多: everything the owner could do before, one entry each. Nothing decides anything here.
  const moreOf = (row: ComponentRow): MoreItem[] => {
    const items: MoreItem[] = []
    if (row.source.kind === 'skill') {
      const skill = row.source.item
      if (onViewComponent) items.push({ id: 'view', testid: 'internal-view', label: t('components.view'), active: componentDetail?.id === skill.id && !componentWorkbench, onPress: () => onViewComponent(skill.id) })
      if (onWorkComponent) {
        items.push({ id: 'edit', testid: 'internal-edit', label: t('components.edit'), onPress: () => onWorkComponent(skill.id, 'contract') })
        items.push({ id: 'test', testid: 'internal-test', label: t('components.test'), onPress: () => onWorkComponent(skill.id, 'test') })
      }
      if (onUseComponent && workflows) items.push({ id: 'use', testid: 'internal-use', label: t('components.list.usePickOther'), active: picking === row.key, onPress: () => { setOpenRow(row.key); setPicking(current => (current === row.key ? '' : row.key)) } })
    }
    if (row.source.kind === 'catalog_skill') {
      // 调用测试 (policy): the local library's skills are tried the way the market's are. Imported afresh when
      // never tried or discarded (its files may have changed, which is when 重新试用 refuses); a paid one is not imported.
      const item = row.source.item
      const trial = market ? trialForListing(market.trials, item.id, item.version, 'catalog') : undefined
      const busy = market?.busy[`catalog:${item.id}`]
      const trialState = catalogTrialState(trial, item.source.skill_md_sha256)
      const canImport = trialState === 'none' || trialState === 'discarded'
      const libraryReady = catalog?.library?.ready !== false
      if (trial && onOpenTrial) items.push({ id: 'trial', testid: 'catalog-open-trial', label: t('market.openTrial'), onPress: () => onOpenTrial(trial) })
      if (canImport && onTryCatalogSkill) items.push({
        id: 'try', testid: 'catalog-try', disabled: !connected || item.pricing.paid || Boolean(busy) || !libraryReady,
        label: busy === 'downloading' ? t('market.importing') : item.pricing.paid ? t('market.paidNoDownload') : t('market.use'),
        onPress: () => onTryCatalogSkill(item),
      })
    }
    if (row.source.kind === 'flow') {
      const flow = row.source.item
      if (onOpenFlowOnBuilder) items.push({ id: 'builder', testid: 'flow-open-builder', label: t('components.list.openOnBuilder'), onPress: () => onOpenFlowOnBuilder(flow.id) })
      // The server exports only a version a customer explicitly authorized; anything else comes back as a refusal.
      if (onExportDistribution && flow.status === 'published') items.push({ id: 'export', testid: 'flow-distribution-export', label: t('components.distribution.export'), disabled: !connected, onPress: () => onExportDistribution(flow.id) })
    }
    return items
  }

  const detailOf = (row: ComponentRow): ReactNode => {
    const source = row.source
    switch (source.kind) {
      case 'skill': return (
        <InternalSkillDetail skill={source.item} t={t} picker={onUseComponent && workflows && picking === row.key
          ? <UsePicker skill={source.item} workflows={workflows} t={t} onClose={() => setPicking('')}
            onPick={workflowId => { setPicking(''); onUseComponent({ kind: 'skill', id: source.item.id, name: source.item.name }, workflowId) }} />
          : null} />
      )
      case 'built_in': return <BuiltInDetail item={source.item} zh={zh} t={t} />
      case 'connector': return <RegisteredConnectorDetail item={source.item} t={t} />
      case 'catalog_connector': return <ConnectorDetail item={source.item} registered={source.registered} domainName={domainNames.get(source.item.domain) ?? source.item.domain} t={t} tm={tm} />
      case 'catalog_skill': return <CatalogSkillDetail item={source.item} t={t} tm={tm} trial={market ? trialForListing(market.trials, source.item.id, source.item.version, 'catalog') : undefined} />
      case 'flow': return <FlowDetail flow={source.item} t={t} />
    }
  }

  const renderRow = (row: ComponentRow) => {
    const verb = verbOf(row)
    const more = moreOf(row)
    const open = openRow === row.key
    return (
      <li key={row.key} className="cl-row" data-testid={ROW_TESTID[row.kind]} data-row="component" data-kind={row.kind} data-id={row.id} data-state={row.state} data-verb={row.verb} data-open={open ? 'true' : 'false'}>
        <div className="cl-row__line">
          <button type="button" className="cl-row__main" aria-expanded={open} onClick={() => toggleRow(row.key)}>
            <strong className="cl-row__name">{row.name[zh ? 0 : 1]}</strong>
            <small className="cl-row__sub">{t(`components.list.kind.${kindWordOf(row)}` as Key)}{row.subgroupName[0] ? ` · ${row.subgroupName[zh ? 0 : 1]}` : ''}{row.version ? ` · v${row.version}` : ''}</small>
          </button>
          <span className="cl-row__state" data-state={row.state}>{t(`components.list.state.${row.state}` as Key)}</span>
          <button type="button" className={`shell-btn cl-row__verb${verb.primary ? ' shell-btn--primary' : ''}`} data-testid="component-verb" data-verb={row.verb}
            disabled={verb.disabled} title={verb.why || undefined} onClick={verb.onPress}>{verb.label}</button>
          {more.length > 0 && (
            <details className="cl-row__more" data-testid="component-more">
              <summary>{t('ownerCard.more')}</summary>
              <div className="cl-row__more-list">
                {more.map(item => (
                  <button key={item.id} type="button" data-testid={item.testid} data-more={item.id} disabled={item.disabled} aria-pressed={item.active} onClick={item.onPress}>{item.label}</button>
                ))}
              </div>
            </details>
          )}
        </div>
        {verb.disabled && verb.why && <small className="cl-row__why" data-testid="component-verb-why">{verb.why}</small>}
        {open && <div className="cl-row__detail" data-testid="component-row-detail">{detailOf(row)}</div>}
      </li>
    )
  }

  const groupsShown = groups.filter(group => (section === 'all' || section === group.group) && (catalog || group.group === 'flows'))

  return (
    <section className="component-library cl-list" data-testid="component-library">
      {section === 'employees' && handoffContent}
      {/* 4.7-A16: brought here from a step on the builder -- the way back to that step, with its unsaved edits. */}
      {returnToStep && onReturnToStep && (
        <div className="cl-return" data-testid="component-return-to-step">
          <button type="button" className="capability-refresh-btn" onClick={onReturnToStep}>
            {zh ? '← 回到刚才的步骤' : '← Back to the step'}
          </button>
          <small className="cl-meta">{returnToStep.flowName} · {returnToStep.stepName}</small>
        </div>
      )}
      <header className="cl-head">
        <div className="cl-head__text">
          <h1>{t('components.title')}</h1>
          {/* 现在在改: the draft 加进草稿 goes to -- the flow the builder holds, when it is a draft; else where to get one. */}
          <p className="cl-head__draft" data-testid="component-current-draft" data-draft={draft?.id ?? ''}>
            {draft
              ? <>{t('components.draft.editing')}<b>{draft.name}</b> · {t(`components.flowStatus.${draft.status}` as Key)} · v{draft.version}{onOpenFlowOnBuilder && <> · <button type="button" className="cl-link" data-testid="component-draft-open" onClick={() => onOpenFlowOnBuilder(draft.id)}>{t('components.draft.toBuilder')}</button></>}</>
              : t('components.draft.none')}
          </p>
        </div>
        <div className="cl-head__actions">
          {onDraftWithAssistant && (
            <button type="button" className="shell-btn shell-btn--primary" data-testid="component-draft-ask" aria-expanded={drafting} onClick={() => setDrafting(current => !current)}>
              {t('components.draft.ask')}
            </button>
          )}
          <button type="button" className="capability-refresh-btn" disabled={!connected || loading} onClick={onRefresh}>
            {loading ? t('components.loading') : t('components.refresh')}
          </button>
        </div>
      </header>
      {drafting && onDraftWithAssistant && (
        // 让安安起草一个: the same sentence 让安安起草这个组件 sends from a draft card (4.7 A05), with what the owner typed as
        // the thing to draft. 安安 may only draft an inert candidate from it; the owner sees the reply on 总控.
        <form className="cl-draft-ask" data-testid="component-draft-form" onSubmit={event => {
          event.preventDefault()
          const what = draftWhat.trim()
          if (!what || !connected) return
          onDraftWithAssistant(t('components.draft.askText', { what }))
          setDrafting(false); setDraftWhat('')
        }}>
          <label>{t('components.draft.askWhat')}<input value={draftWhat} onChange={event => setDraftWhat(event.target.value)} /></label>
          <button type="submit" className="shell-btn shell-btn--primary" data-testid="component-draft-send" disabled={!connected || !draftWhat.trim() || tracked['master_send']?.state === 'busy'}>{t('components.draft.askSend')}</button>
          <button type="button" className="shell-btn" onClick={() => setDrafting(false)}>{t('components.useClose')}</button>
          <small className="cl-line">{t('components.draft.askNote')}</small>
        </form>
      )}
      {error && <p className="cl-alert" role="alert" data-testid="component-library-error">{error}</p>}
      {catalog && catalog.problems.length > 0 && (
        <p className="cl-alert" role="alert">{t('components.problems', { count: catalog.problems.length })} {catalog.problems[0]}</p>
      )}
      <div className="cl-sections" role="tablist" aria-label={t('components.title')}>
        {tabs.map(tab => (
          <button type="button" role="tab" key={tab} aria-selected={section === tab} className={section === tab ? 'active' : ''} data-section={tab} onClick={() => setSection(tab)}>
            <span>{tab === 'employees' ? (zh ? 'AI 员工' : 'AI employees') : t(`components.list.group.${tab}` as Key)}</span>
            {countOf(tab) !== null && <b>{countOf(tab)}</b>}
          </button>
        ))}
      </div>

      {section === 'market' && market && <SkillMarketPanel {...market} />}
      {section === 'employees' && employeeContent}

      {section !== 'market' && section !== 'employees' && (
        <div className="cl-filters cl-filters--list">
          <input type="search" aria-label={t('components.list.search')} placeholder={t('components.list.search')} value={query} onChange={event => setQuery(event.target.value)} />
          <select aria-label={t('components.list.allStates')} value={stateFilter} onChange={event => setStateFilter(event.target.value as ComponentState | '')}>
            <option value="">{t('components.list.allStates')}</option>
            {COMPONENT_STATES.map(state => <option key={state} value={state}>{t(`components.list.state.${state}` as Key)} · {stateCounts[state]}</option>)}
          </select>
          <span className="cl-count" data-testid="component-library-count">{t('components.showing', { shown: shown.length, total: rows.length })}</span>
        </div>
      )}

      {section !== 'market' && section !== 'employees' && !catalog && !error && <p className="cl-empty">{loading ? t('components.loading') : t('components.empty')}</p>}

      {section !== 'market' && section !== 'employees' && groupsShown.map(group => (
        <section key={group.group} className="cl-group" data-testid={`component-group-${group.group}`} data-group={group.group}>
          <h2 className="cl-subhead">{t(`components.list.group.${group.group}` as Key)} <b>{filtering ? `${group.rows.length} / ${totalOf(group.group)}` : totalOf(group.group)}</b></h2>
          <p className="cl-group__note">{t(`components.list.groupNote.${group.group}` as Key)}</p>
          {group.group === 'platform' && builtIns.length > 0 && <p className="cl-group__note">{t('components.list.builtInNote', { count: builtIns.length })}</p>}
          {group.group === 'industry' && onTryCatalogSkill && catalog?.library && !catalog.library.ready && (
            <p className="cl-line cl-note" data-testid="catalog-library-note">{t(`components.library.${catalog.library.reason}` as Key, { library: catalog.library.library })}</p>
          )}
          {group.group === 'flows' && workflows === null
            ? <p className="cl-empty">{t('components.loading')}</p>
            : group.rows.length === 0
              ? <p className="cl-empty">{totalOf(group.group) === 0 ? t(`components.list.groupEmpty.${group.group}` as Key) : t('components.noMatch')}</p>
              : group.subgroups.map(sub => (
                <div key={sub.key || '_'} className="cl-subgroup" data-subgroup={sub.key}>
                  {sub.name[0] && <h3 className="cl-subgroup__head">{sub.name[zh ? 0 : 1]}</h3>}
                  <ul className="cl-rows">{sub.rows.map(renderRow)}</ul>
                </div>
              ))}
          {group.group === 'platform' && catalog && (catalog.planned.length > 0 || catalog.tooling.length > 0) && (
            <footer className="cl-aside">
              {catalog.tooling.length > 0 && <p><b>{t('components.tooling')}</b> {catalog.tooling.map(item => item.name).join(' · ')}</p>}
              {catalog.planned.length > 0 && <p><b>{t('components.planned')}</b> {catalog.planned.map(item => item.name).join(' · ')}</p>}
            </footer>
          )}
          {group.group === 'ability' && (improvementProposals !== undefined || skillImprovements !== undefined) && (
            // RSI governance stays inside the library, folded: evidence first, owner-only deployment actions second.
            <details className="cl-fold" data-testid="component-improvements">
              <summary>{t('components.list.improvementsFold')}<b>{(improvementProposals?.length ?? 0) + (skillImprovements?.length ?? 0)}</b></summary>
              <div>
                {improvementProposals !== undefined && <ImprovementProposalPanel
                  items={improvementProposals ?? null} connected={connected} error={skillImprovementError}
                  onRefresh={onRefreshSkillImprovements} t={t} actions={improvementActions} zh={zh}
                />}
                {skillImprovements !== undefined && <SkillImprovementPanel
                  items={skillImprovements ?? null} connected={connected} busy={skillImprovementBusy} error={skillImprovementError}
                  onRefresh={onRefreshSkillImprovements} onStage={onStageSkillImprovement}
                  onActivate={onActivateSkillImprovement} onRollback={onRollbackSkillImprovement} t={t}
                />}
              </div>
            </details>
          )}
          {group.group === 'flows' && onReceiveDistribution && (
            <DistributionInboxPanel inbox={distributionInbox ?? null} connected={connected} onReceive={onReceiveDistribution} onRefresh={onRefreshDistributionInbox} t={t} />
          )}
        </section>
      ))}

      {section !== 'employees' && componentDetail && onCloseComponentDetail && (
        <ComponentDetailPanel view={componentDetail} onClose={onCloseComponentDetail} t={t} workbench={componentWorkbench ?? undefined} zh={zh} receiptNote={receiptNote}
          extra={improvementStarter && componentDetail.detail ? improvementStarter({ kind: 'skill', id: componentDetail.id, label: componentDetail.detail.name || componentDetail.id }) : null} />
      )}
    </section>
  )
}
