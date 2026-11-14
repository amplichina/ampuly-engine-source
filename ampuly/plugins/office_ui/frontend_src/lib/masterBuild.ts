// 总控施工模式 (policy, B2): the build card in 总控's conversation, as pure functions.
//
// A build job (master_build.py, B1) writes its messages into the project's 总控 conversation with
// `metadata.master_build` -- the start, a progress note, a question for the owner, the hand-over, a stop -- and sends
// the socket that started or continued it one ack per step, `master_build_activity`. policy (总控只当对话页):
// nothing is added above the conversation; a build shows as messages in it. So each job has ONE live card, on its
// latest message, and the job's earlier messages read as plain lines -- the conversation never fills with stale cards.
//
// Everything read here is the server's data. Ids ride along only to be sent back (a stop, a steer, an answer) or to
// sit in a data attribute; the owner reads words.

import { refusalKind, type AnswerOutcome } from './answerPath'

export const BUILD_STATES = ['running', 'waiting_owner', 'delivered', 'stopped', 'failed', 'interrupted', 'capped'] as const
export type BuildState = typeof BUILD_STATES[number]

/** The states a turn has ended in: the card says why, and offers 接着改. */
export const SETTLED_BUILD_STATES: readonly BuildState[] = ['delivered', 'stopped', 'failed', 'interrupted', 'capped']

/** The three answers a build's question offers (B1 checkpoint_card), in the card's order. Each button sends its own
 *  option and nothing wider: an option the question does not offer is not drawn. */
export const BUILD_ANSWERS = ['approve_once', 'approve_session', 'deny'] as const
export type BuildAnswer = typeof BUILD_ANSWERS[number]

export interface BuildQuestion {
  checkpointId: string
  checkpointType: string
  requesterTaskId: string
  requesterSessionId: string
  /** The question in words (B1 question_words): what the build wants to do, and 允许吗？ */
  question: string
  options: Array<{ id: BuildAnswer; label: string }>
}

export type BuildChangeKind = 'added' | 'modified' | 'removed'

export interface BuildChange {
  path: string
  change: BuildChangeKind
}

export interface BuildDelivery {
  summary: string
  changedFiles: BuildChange[]
  /** The list is cut (the draft had too many files, or too many changed). */
  truncated: boolean
  openQuestions: string[]
  toolsToDevelop: string[]
  /** Whether the build wrote its notes back (null when the record does not say). */
  notesUpdated: boolean | null
}

export interface BuildCard {
  jobId: string
  state: BuildState
  /** The goal in the owner's words. */
  goal: string
  /** The draft's name. */
  name: string
  draftId: string
  sessionId: string
  turnId: string
  /** A progress note the build posted (build_progress), '' otherwise. */
  progress: string
  /** Why it failed, in the owner's words ('' unless failed). */
  lastError: string
  question: BuildQuestion | null
  delivery: BuildDelivery | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function text(value: unknown, max: number): string {
  return typeof value === 'string' ? value.trim().slice(0, max) : ''
}

/** An id that only goes back to the server or into a data attribute: a short string with no spaces. */
function idOf(value: unknown): string {
  const raw = text(value, 256)
  return raw && !/\s/.test(raw) ? raw : ''
}

function texts(value: unknown, max: number, count: number): string[] {
  if (!Array.isArray(value)) return []
  return value.map(item => text(item, max)).filter(Boolean).slice(0, count)
}

export function isBuildState(value: unknown): value is BuildState {
  return (BUILD_STATES as readonly unknown[]).includes(value)
}

function questionFrom(raw: unknown): BuildQuestion | null {
  if (!isRecord(raw)) return null
  const checkpointId = idOf(raw.checkpoint_id)
  if (!checkpointId) return null
  const offered = Array.isArray(raw.options) ? raw.options.filter(isRecord) : []
  const options = BUILD_ANSWERS.flatMap(id => {
    const option = offered.find(item => item.id === id)
    return option ? [{ id, label: text(option.label, 60) }] : []
  })
  return {
    checkpointId,
    checkpointType: idOf(raw.checkpoint_type),
    requesterTaskId: idOf(raw.requester_task_id),
    requesterSessionId: idOf(raw.requester_session_id),
    question: text(raw.question, 1200),
    options,
  }
}

function deliveryFrom(raw: unknown): BuildDelivery | null {
  if (!isRecord(raw)) return null
  const changedFiles = (Array.isArray(raw.changed_files) ? raw.changed_files : []).flatMap((item): BuildChange[] => {
    if (!isRecord(item)) return []
    const path = text(item.path, 500)
    const change = item.change
    return path && (change === 'added' || change === 'modified' || change === 'removed') ? [{ path, change }] : []
  }).slice(0, 200)
  return {
    summary: text(raw.summary, 1200),
    changedFiles,
    truncated: raw.changed_files_truncated === true,
    openQuestions: texts(raw.open_questions, 400, 20),
    toolsToDevelop: texts(raw.tools_to_develop, 400, 20),
    notesUpdated: typeof raw.notes_updated === 'boolean' ? raw.notes_updated : null,
  }
}

/** The build a message is about (`metadata.master_build`), or null: a message without one, or one this page cannot
 *  read (no job, a state it does not know), is an ordinary message. */
export function buildCardFrom(metadata: unknown): BuildCard | null {
  if (!isRecord(metadata)) return null
  const raw = metadata.master_build
  if (!isRecord(raw)) return null
  const jobId = idOf(raw.job_id)
  const state = raw.state
  if (!jobId || jobId.length > 64 || !isBuildState(state)) return null
  return {
    jobId,
    state,
    goal: text(raw.goal, 4000),
    name: text(raw.name, 200),
    draftId: idOf(raw.draft_id),
    sessionId: idOf(raw.session_id),
    turnId: idOf(raw.turn_id),
    progress: text(raw.progress, 400),
    lastError: text(raw.last_error, 600),
    question: state === 'waiting_owner' ? questionFrom(raw.checkpoint) : null,
    delivery: state === 'delivered' ? deliveryFrom(raw.delivery) : null,
  }
}

interface MessageLike {
  id: string
  sender: string
  metadata?: unknown
}

/** The last message of the conversation proper: the owner's, or 安安's reply -- a build's messages left out. A build
 *  posts on its own time (a build started in 安安's turn posts its first line before 安安 has replied), so its lines
 *  neither answer the owner nor end the wait for 安安's reply. */
export function lastTalk<T extends MessageLike>(messages: readonly T[]): T | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.sender === 'user' || !buildCardFrom(message.metadata)) return message
  }
  return undefined
}

/** Per job, the id of its latest message -- the one that shows the live card. A job's earlier messages are plain lines. */
/** Whether some build of this conversation is still running or waiting on the owner, by each job's latest message.
 * 总控 then opens on the conversation instead of folding it away (review of B2, : a 等你回答 question
 * folded into 「接着上次的对话」 could wait unseen until the 4-hour clock). */
export function hasLiveBuild(messages: readonly MessageLike[]): boolean {
  const latest = new Map<string, BuildState>()
  for (const message of messages) {
    if (message.sender === 'user') continue
    const card = buildCardFrom(message.metadata)
    if (card) latest.set(card.jobId, card.state)
  }
  return [...latest.values()].some(state => !SETTLED_BUILD_STATES.includes(state))
}

export function liveBuildMessages(messages: readonly MessageLike[]): ReadonlyMap<string, string> {
  const live = new Map<string, string>()
  for (const message of messages) {
    if (message.sender === 'user') continue
    const card = buildCardFrom(message.metadata)
    if (card) live.set(card.jobId, message.id)
  }
  return live
}

/** The jobs whose latest message says their turn has ended (delivered, stopped, failed, interrupted, capped), sorted:
 *  App drops their last step, so a job that is continued later does not show the old turn's step. */
export function settledBuildJobs(messages: readonly MessageLike[]): string[] {
  const latest = new Map<string, BuildState>()
  for (const message of messages) {
    if (message.sender === 'user') continue
    const card = buildCardFrom(message.metadata)
    if (card) latest.set(card.jobId, card.state)
  }
  return [...latest].filter(([, state]) => SETTLED_BUILD_STATES.includes(state)).map(([jobId]) => jobId).sort()
}

// ── the step a build is on (master_build_activity) ────────────────────────────────────────────────────────────────

export interface BuildActivity {
  jobId: string
  state: BuildState
  /** Counts the steps of one turn. */
  sequence: number
  /** The raw tool name ('' for a turn that started or a question answered): for the data attribute only. */
  toolName: string
  sessionId: string
  turnId: string
}

/** By job id: the last step of each job that is running. */
export type BuildActivities = Readonly<Record<string, BuildActivity>>

/** One `master_build_activity` ack, read; null when it is not one this page can use. */
export function buildActivityFrom(payload: Record<string, unknown>): BuildActivity | null {
  if (payload.action !== 'master_build_activity' || payload.ok === false) return null
  const jobId = idOf(payload.job_id)
  const state = payload.state
  const sequence = Number(payload.sequence)
  const toolName = typeof payload.tool_name === 'string' ? payload.tool_name : ''
  if (!jobId || jobId.length > 64 || !isBuildState(state)) return null
  if (!Number.isSafeInteger(sequence) || sequence < 0) return null
  if (toolName && (toolName.length > 96 || !/^[a-z][a-z0-9_]*$/.test(toolName))) return null
  return { jobId, state, sequence, toolName, sessionId: idOf(payload.session_id), turnId: idOf(payload.turn_id) }
}

/** The last step per job: kept while the job runs, dropped the moment an ack says it left running (a question, the
 *  hand-over). An older step of the same turn never replaces a newer one. The same object when nothing changes. */
export function keepBuildActivity(current: BuildActivities, next: BuildActivity): BuildActivities {
  const held = current[next.jobId]
  if (next.state !== 'running') {
    if (!held) return current
    const rest = { ...current }
    delete rest[next.jobId]
    return rest
  }
  if (held && held.turnId === next.turnId && held.sequence >= next.sequence) return current
  return { ...current, [next.jobId]: next }
}

/** Drop the steps of jobs whose turn has ended (settledBuildJobs). The same object when nothing changes. */
export function dropSettledActivity(current: BuildActivities, settled: readonly string[]): BuildActivities {
  const gone = settled.filter(jobId => jobId in current)
  if (!gone.length) return current
  const rest = { ...current }
  for (const jobId of gone) delete rest[jobId]
  return rest
}

// What a builder step is, in the owner's words (the masterToolWords style: one short phrase each, the raw name only in
// a data attribute). The build's own tools and the worker's (master_build.py WORKER_TOOLS / BUILD_TOOLS).
const READ: readonly [string, string] = ['读文件', 'reading files']
const EDIT: readonly [string, string] = ['改文件', 'changing files']
const WEB: readonly [string, string] = ['查网页', 'looking things up on the web']
const STEP_WORDS: Readonly<Record<string, readonly [string, string]>> = {
  file_read: READ, grep: READ, glob: READ, file_search: READ, list_dir: READ,
  file_write: EDIT, file_edit: EDIT, apply_patch: EDIT,
  shell_exec: ['跑命令', 'running a command'],
  python_exec: ['跑 Python', 'running Python'],
  web_search: WEB, web_fetch: WEB,
  build_line_overview: ['看现有业务线', 'looking at the line in use'],
  build_check_draft: ['检查草稿', 'checking the draft'],
  build_offline_trial: ['离线试跑', 'trying the draft offline'],
  build_progress: ['报进度', 'reporting progress'],
  build_deliver: ['交付', 'handing over'],
}
const OTHER_STEP: readonly [string, string] = ['做一步', 'working on a step']
const NEXT_STEP: readonly [string, string] = ['想下一步', 'deciding the next step']

/** The step in words; a tool this list does not know is 「做一步」, never its name; no tool (a turn that started, a
 *  question answered) is 「想下一步」. */
export function buildStepWords(toolName: string, zh: boolean): string {
  const words = !toolName ? NEXT_STEP : Object.prototype.hasOwnProperty.call(STEP_WORDS, toolName) ? STEP_WORDS[toolName] : OTHER_STEP
  return words[zh ? 0 : 1]
}

/** The tools that have words of their own. */
export const WORDED_BUILD_TOOLS: readonly string[] = Object.keys(STEP_WORDS)

// ── what the card says ─────────────────────────────────────────────────────────────────────────────────────────────

export function buildStateKey(state: BuildState): `master.build.state.${BuildState}` {
  return `master.build.state.${state}`
}

export function buildAnswerKey(option: BuildAnswer): `master.build.answer.${BuildAnswer}` {
  return `master.build.answer.${option}`
}

export interface BuildCardView {
  /** The state the card says now. */
  state: BuildState
  /** Running only: the step it is on (null before the first step is known). */
  step: BuildActivity | null
  /** The turn a stop or a steer is for ('' when not known yet: a stop then asks for whatever turn is pending). */
  turnId: string
}

/**
 * What the card shows: the state of its message -- except that a question the owner answered here, with the job's
 * running step come in since (the answer set it going again; no message is written for that), shows the running card.
 * A step held for a job whose message waits on a question the owner has not answered here says nothing: a lost ack
 * never hides the question.
 */
export function buildCardView(card: BuildCard, activity: BuildActivity | undefined, answeredHere: boolean): BuildCardView {
  const running = activity && activity.jobId === card.jobId && activity.state === 'running' ? activity : null
  const state: BuildState = card.state === 'waiting_owner' && answeredHere && running ? 'running' : card.state
  return {
    state,
    step: state === 'running' ? running : null,
    turnId: running?.turnId || card.turnId,
  }
}

// ── 改了什么, grouped the way the owner thinks of a line ────────────────────────────────────────────────────────────

export const BUILD_FILE_KINDS = ['skill', 'nodes', 'org', 'test', 'other'] as const
export type BuildFileKind = typeof BUILD_FILE_KINDS[number]

/** Which part of the line a draft file is: 技能 / 节点表 / 组织配置 / 测试 / 其它. */
export function buildFileKind(path: string): BuildFileKind {
  const parts = path.replace(/\\/g, '/').toLowerCase().split('/').filter(Boolean)
  const base = parts[parts.length - 1] ?? ''
  if (parts.slice(0, -1).includes('skills') || base === 'skill.md') return 'skill'
  if (parts.slice(0, -1).some(part => part === 'tests' || part === 'test') || /^test[_.-]|[_.-]test\.|\.spec\./.test(base)) return 'test'
  if (/^(workflow|stages|nodes)\.ya?ml$/.test(base)) return 'nodes'
  if (/^org(anization)?([_-].*)?\.ya?ml$/.test(base) || parts.slice(0, -1).includes('orgs')) return 'org'
  return 'other'
}

/** How a file is named on the card: a skill's file from its skill folder on (「new-skill/SKILL.md」), others as they are. */
export function buildFileLabel(path: string): string {
  const clean = path.replace(/\\/g, '/')
  const parts = clean.split('/')
  const at = parts.map(part => part.toLowerCase()).lastIndexOf('skills')
  return at >= 0 && at < parts.length - 1 ? parts.slice(at + 1).join('/') : clean
}

/** The changed files by kind, in the card's order; kinds with nothing changed are left out. */
export function groupChangedFiles(changes: readonly BuildChange[]): Array<{ kind: BuildFileKind; files: BuildChange[] }> {
  const groups = new Map<BuildFileKind, BuildChange[]>()
  for (const change of changes) {
    const kind = buildFileKind(change.path)
    groups.set(kind, [...(groups.get(kind) ?? []), change])
  }
  return BUILD_FILE_KINDS.flatMap(kind => (groups.get(kind)?.length ? [{ kind, files: groups.get(kind)! }] : []))
}

/** 接着改: the input begins with the prefix (「接着改：」), and whatever the owner had typed stays after it. */
export function continueDraft(current: string, prefix: string): string {
  const typed = current.trim()
  if (!typed) return prefix
  if (typed.startsWith(prefix.trim())) return current
  return `${prefix}${typed}`
}

// ── the card's 停下 / 插话 and its answers ──────────────────────────────────────────────────────────────────────────

/** The card's stop / steer bookkeeping (one live build per project, B1): which job and turn it is for, the request ids
 *  still out ('' none) and where each stands -- the same states and words as 总控's own row. */
export interface BuildControl {
  jobId: string
  turnId: string
  interruptRequestId: string
  interruptRequested: boolean
  interruptError: string | null
  steerRequestId: string
  steerNote: string | null
}

export const IDLE_BUILD_CONTROL: BuildControl = Object.freeze({
  jobId: '', turnId: '', interruptRequestId: '', interruptRequested: false, interruptError: null, steerRequestId: '', steerNote: null,
})

/** The controls of this job's turn; what was asked for another job or another turn is not this turn's (a new turn
 *  starts with its stop and steer fresh, as 总控's row does). */
export function controlFor(control: BuildControl, jobId: string, turnId: string): BuildControl {
  if (control.jobId && control.jobId === jobId && control.turnId === turnId) return control
  return { ...IDLE_BUILD_CONTROL, jobId, turnId }
}

/** A stop sent (`sent`) or refused on the spot (`words`): the steer of the same turn keeps its note. */
export function beginBuildInterrupt(control: BuildControl, jobId: string, turnId: string, requestId: string, sent: boolean, words: string): BuildControl {
  const held = controlFor(control, jobId, turnId)
  return { ...held, jobId, turnId, interruptRequestId: sent ? requestId : '', interruptRequested: false, interruptError: sent ? null : words }
}

/** A steer sent (`sent`) or refused on the spot (`words`): the stop of the same turn keeps where it stands. */
export function beginBuildSteer(control: BuildControl, jobId: string, turnId: string, requestId: string, sent: boolean, words: string): BuildControl {
  const held = controlFor(control, jobId, turnId)
  return { ...held, jobId, turnId, steerRequestId: sent ? requestId : '', steerNote: sent ? null : words }
}

type SteerReasonKey = 'master.build.steerReason.ended' | 'master.build.steerReason.owner' | 'master.build.steerReason.tooLong'
  | 'master.build.steerReason.other'
type SteerWords = (key: 'master.steerAccepted' | 'master.steerNotConfirmed' | 'master.steerAlternative' | 'master.steerUnknown'
  | 'master.build.steerInstead' | SteerReasonKey, params?: Record<string, string | number>) => string

// Why a steer did not reach the build's turn, in words: the handler (harness_turn_steer) and the engine answer in codes,
// and the owner reads none of them. Anything not listed is said in general words.
const STEER_REASONS: Readonly<Record<string, SteerReasonKey>> = {
  nothing_pending: 'master.build.steerReason.ended',
  turn_changed: 'master.build.steerReason.ended',
  session_not_live: 'master.build.steerReason.ended',
  owner_required: 'master.build.steerReason.owner',
  not_authorized: 'master.build.steerReason.owner',
  authenticated_owner_required: 'master.build.steerReason.owner',
  text_too_long: 'master.build.steerReason.tooLong',
}

/** A steer's receipt in the frame 总控's row uses (delivered is never "applied"); why it did not get through is said in
 *  words, never the server's code, and where it can still go is 「接着改」. */
export function steerReceiptWords(payload: Record<string, unknown>, t: SteerWords): string {
  if (payload.ok === true && payload.delivery === 'accepted') return t('master.steerAccepted')
  const code = String(payload.error ?? payload.reason ?? '').trim()
  if (code === 'outcome_unknown') return t('master.steerUnknown')
  const key = Object.prototype.hasOwnProperty.call(STEER_REASONS, code) ? STEER_REASONS[code] : 'master.build.steerReason.other'
  const words = t('master.steerNotConfirmed', { reason: t(key) })
  // Said again with 接着改 it reaches the build -- unless only the owner may, or the sentence is too long anyway.
  const instead = key === 'master.build.steerReason.ended' || key === 'master.build.steerReason.other'
  return instead ? `${words} ${t('master.steerAlternative', { alternative: t('master.build.steerInstead') })}` : words
}

/** What an answer to a build's question came back with, by checkpoint id: `done` keeps the three buttons disabled
 *  until the job's next state arrives (accepted, or the question is no longer waiting); `words` is a refusal. */
export interface BuildAnswerMark {
  optionId: string
  done: boolean
  words: string
}

export type BuildAnswerMarks = Readonly<Record<string, BuildAnswerMark>>

/** One answer's outcome (lib/answerPath), as the card keeps it. A second click while the first is out changes nothing. */
export function buildAnswerMark(outcome: AnswerOutcome, optionId: string): BuildAnswerMark | null {
  if (outcome.kind === 'accepted') return { optionId, done: true, words: '' }
  if (outcome.kind === 'not_sent' && outcome.code === 'duplicate_request_in_flight') return null
  const closed = outcome.code === 'checkpoint_not_found' || (outcome.kind === 'refused' && refusalKind(outcome.code) === 'closed')
  return { optionId, done: closed, words: outcome.words }
}
