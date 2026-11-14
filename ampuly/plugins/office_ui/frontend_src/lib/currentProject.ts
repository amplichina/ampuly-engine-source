// 屏幕上的项目 -- which state belongs to the project on screen (frontend redesign step 2 = architecture-review
// candidate 6, first half; policy, FRONTEND-IA- 「按架构审查调整」 row 6 · 屏幕上的项目 / 请求追踪).
//
// One module, three jobs:
//
//   * it lists the state that belongs to the project on screen (PROJECT_SCOPED_KEYS) and empties all of it in ONE
//     place, `beginSwitch`, the moment a switch to another project begins. Until now the reset was spread over three
//     sites (the click, the server's project_switched, the sync that settled it) and five things were never reset at
//     all -- the orders, the flows, the open gate, the cards' refusals, the order's timeline -- so the previous
//     project's rows sat under the new project's name until a reply happened to replace them;
//   * it counts switches (`generation`) and remembers the one under way (`pendingProjectId`, `switchSeq`);
//   * `accepts` is the one gate every reply handler asks: the reply names the project on screen, no switch to another
//     project is under way, and the reply's view_generation / switch_seq match when it carries them. A reply that
//     fails it is dropped and counted (`dropped`, for the dev tools; no toast). `acceptsOrBareRefusal` is its one
//     variant, for a read whose page would otherwise wait for ever on a refusal that names no project (the
//     dispatcher's own 「unauthorized」, the client's 「requires project_id」); `acceptsSwitchReply` is for the replies
//     that settle a switch, which name the project being switched to before it is the one on screen.
//
// The active id stays ProjectStore's (through the synchronous mirror App keeps); this module reads it through the
// getter it is handed and never sets it. Pure of React: App hands in the resets and reads the fields.

export const PROJECT_SCOPED_KEYS = [
  // 客户预览 of one order (customerPreviewState).
  'customerPreview',
  // 总控's running turn, its interrupt / steer bookkeeping and the turn ledger.
  'masterTurn',
  // 总控's tool activity strip.
  'masterTools',
  // 总控's build cards (施工模式 B2): each running job's last step, the card's stop / steer, the answers to its questions.
  'masterBuild',
  // The harness's pending turns and their error.
  'pendingTurns',
  // 待审批: the cards, how many are held, the load state (asked again once the switch settles).
  'blockers',
  // What the last answer on each card came back with, in words.
  'blockerDecisionErrors',
  // The answer path's out / unconfirmed answers (lib/answerPath).
  'answering',
  // Session-detail refreshes waiting to go out.
  'sessionDetailRefreshes',
  // 业务工单: the rows, paging, truncation and the last 分发授权单 answer.
  'orders',
  // order_metrics: cost / saving / promise figures and usage by order.
  'orderMetrics',
  // One order's timeline and model plan (the drawer).
  'orderTimeline',
  // The flows and their load error.
  'workflows',
  // The open gate's materials view and its error.
  'gateDetail',
  // Allowances and the calls whose outcome nobody knows.
  'runBudgets',
  // Browser sessions, their error, the candidate note and the sealed demonstration.
  'browserSessions',
  // What this console said about the previous project's governance actions.
  'governanceError',
  // A restore preflight report and its error.
  'restorePreflight',
  // The process spec open in 能力中心 (selected spec, draft, review, rounds).
  'processSpec',
  // 执行区's open session.
  'executionPanel',
  // The external team-activity cache (ExternalTeamActivityStore).
  'teamActivity',
  // Comms state and the last comms message.
  'comms',
  // A flow's preflight report.
  'preflight',
  // Node trials, their pin plan, the open trial file, a refused save and an admission refusal.
  'nodeTrials',
  // 交付物: the index and the open preview.
  'artifacts',
  // 项目中心's business boundary and connection lanes (project_profile_* / project_connections_*, S3-2B).
  'projectPanorama',
] as const

export type ProjectScopedKey = (typeof PROJECT_SCOPED_KEYS)[number]

/** How each scoped key is emptied; `nextProjectId` is the project being switched to (a cache keyed by project keeps
 *  that project's records). App hands the whole record in through `own`, so the type says every key is covered. */
export type ScopedResets = Record<ProjectScopedKey, (nextProjectId: string) => void>

/** The page requests a switch throws away and App issues again once the switch settles (the answers to asks made
 *  while it was pending are dropped by `accepts`): the pauses, the owner's browser sessions, and the quiet re-ask of
 *  the page's lists (lib/restoredPageLists). */
export const REISSUE_ON_SETTLE = ['blockers', 'browserSessions', 'pageLists'] as const

export type ReissueRequest = (typeof REISSUE_ON_SETTLE)[number]

export type ScopedPayload = Record<string, unknown> | null | undefined

export function normalizeProjectId(value: unknown): string {
  const projectId = typeof value === 'string' ? value.trim() : ''
  return projectId || 'default'
}

/** The project a reply names, under any of the keys the server and the pushes use. '' when it names none. */
export function projectIdOf(payload: ScopedPayload): string {
  if (!payload || typeof payload !== 'object') return ''
  for (const key of ['project_id', 'projectId', 'active_project_id', 'activeProjectId']) {
    const value = payload[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  const data = payload.data
  if (data && typeof data === 'object') {
    for (const key of ['project_id', 'projectId']) {
      const value = (data as Record<string, unknown>)[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
  }
  return ''
}

/** The view generation a reply was asked under, when it carries one. */
export function viewGenerationOf(payload: ScopedPayload): number | null {
  if (!payload || typeof payload !== 'object') return null
  const value = payload.view_generation ?? payload.viewGeneration
  if (typeof value === 'number') return value
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

/** The switch sequence a reply answers, when it carries one. */
export function switchSeqOf(payload: ScopedPayload): string {
  if (!payload || typeof payload !== 'object') return ''
  const value = payload.switch_seq ?? payload.switchSeq
  return typeof value === 'string' ? value.trim() : ''
}

export interface CurrentProjectDeps {
  /** The project on screen (ProjectStore's active id, through App's synchronous mirror). */
  activeProjectId(): string
  /** A fresh switch sequence; the clock and a nonce unless a test hands in its own. */
  newSwitchSeq?(): string
}

export interface SwitchBegun {
  projectId: string
  switchSeq: string
  generation: number
  /** False when the "switch" is to the project already on screen (the first load settles its own project that
   *  way): nothing is thrown away then, and the answers already in flight for it still land. */
  reset: boolean
  reissue: readonly ReissueRequest[]
}

export interface SwitchSettled {
  /** Whether this sync applied a switch (the one begun, or the server moving this connection). */
  applied: boolean
  reissue: readonly ReissueRequest[]
}

function defaultSwitchSeq(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
}

export class CurrentProject {
  /** Bumped once per switch begun; a reply asked under an older one is dropped. */
  generation = 0
  /** The sequence of the switch under way, kept after it settles so the pushes that answer it still match. */
  switchSeq = ''
  /** The project a switch is under way to, until the sync for it settles; null otherwise. */
  pendingProjectId: string | null = null
  /** Replies dropped by the gates since the console loaded (dev tools only; nothing is said to the owner). */
  dropped = 0
  readonly droppedByAction: Record<string, number> = {}
  private resets: Partial<ScopedResets> = {}

  constructor(private readonly deps: CurrentProjectDeps) {}

  /** App hands in how every scoped key is emptied; `beginSwitch` runs them all. */
  own(resets: ScopedResets): void {
    this.resets = resets
  }

  activeProjectId(): string {
    return normalizeProjectId(this.deps.activeProjectId())
  }

  // ── a switch ─────────────────────────────────────────────────────────────

  /** A switch to `projectId` begins: a new sequence, the next generation, the project marked pending, and -- when
   *  it is not the project already on screen -- every scoped state emptied, here and nowhere else. Returns what App
   *  sends with switch_project and the requests to issue again once the switch settles. */
  beginSwitch(projectId: string): SwitchBegun {
    const next = normalizeProjectId(projectId)
    const reset = next !== this.activeProjectId()
    this.switchSeq = (this.deps.newSwitchSeq ?? defaultSwitchSeq)()
    this.generation += 1
    this.pendingProjectId = next
    if (reset) for (const key of PROJECT_SCOPED_KEYS) this.resets[key]?.(next)
    return { projectId: next, switchSeq: this.switchSeq, generation: this.generation, reset, reissue: REISSUE_ON_SETTLE }
  }

  /** The sync for `projectId` came: the switch is over. Called BEFORE App moves the active id, so it can still say
   *  whether this sync applied a switch (the project pending, or one the server moved this connection to). */
  settle(projectId: string): SwitchSettled {
    const settled = normalizeProjectId(projectId)
    const applied = this.pendingProjectId === settled || this.activeProjectId() !== settled
    this.pendingProjectId = null
    return { applied, reissue: applied ? REISSUE_ON_SETTLE : [] }
  }

  /** The server refused the switch: the console stays on the project it was on, and nothing is pending. */
  cancelSwitch(): void {
    this.pendingProjectId = null
    this.switchSeq = ''
  }

  // ── the gates ────────────────────────────────────────────────────────────

  /** The one gate for a reply about the project on screen: it names that project, no switch to another project is
   *  under way (a "switch" to the project already on screen holds nothing back), and its generation / sequence
   *  match when it carries them. Anything else is dropped and counted. */
  accepts(payload: ScopedPayload): boolean {
    const active = this.activeProjectId()
    const named = projectIdOf(payload)
    const pending = this.pendingProjectId
    const ok = !!named && named === active
      && (pending === null || pending === active)
      && this.matchesGeneration(payload) && this.matchesSeq(payload)
    if (!ok) this.countDrop(payload)
    return ok
  }

  /** `accepts`, or a refusal that names no project: the dispatcher's own, which no project gate can judge and which
   *  a page waiting on a list still has to be shown. */
  acceptsOrBareRefusal(payload: ScopedPayload): boolean {
    if (payload && typeof payload === 'object' && !payload.ok && !projectIdOf(payload)) return true
    return this.accepts(payload)
  }

  /** The gate for what settles a switch (the sync, the index push, project_switched): it may name the project being
   *  switched to as well as the one on screen, and its generation / sequence must match when it carries them. */
  acceptsSwitchReply(payload: ScopedPayload): boolean {
    const named = projectIdOf(payload)
    const ok = !!named && (named === this.activeProjectId() || named === this.pendingProjectId)
      && this.matchesGeneration(payload) && this.matchesSeq(payload)
    if (!ok) this.countDrop(payload)
    return ok
  }

  // ── internals ────────────────────────────────────────────────────────────

  private matchesGeneration(payload: ScopedPayload): boolean {
    const generation = viewGenerationOf(payload)
    return generation === null || generation === this.generation
  }

  private matchesSeq(payload: ScopedPayload): boolean {
    const seq = switchSeqOf(payload)
    return !seq || !this.switchSeq || seq === this.switchSeq
  }

  private countDrop(payload: ScopedPayload): void {
    this.dropped += 1
    const raw = payload && typeof payload === 'object' ? payload.action ?? payload.type : undefined
    const action = typeof raw === 'string' && raw ? raw : '(unnamed)'
    this.droppedByAction[action] = (this.droppedByAction[action] ?? 0) + 1
  }
}
