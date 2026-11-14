// 总控 · 项目与对话栏 (policy, the module contract section 7): a project has
// many 总控 conversations, each its own channel, listed in a column beside the conversation. Pure: App holds the lists
// and the open conversation; this module reads the server's replies and says what to show, what to send and where.
//
// The wire (the orchestrator's contract, P1 builds it on the server): a thread is
//   {thread_id, project_id, channel_id, title, created_at, last_activity_at, archived, legacy, running_build, waiting_owner}
// The first conversation of a project (legacy) is the channel `master:{project_id}`; a later one `master:{pid}:{tid}`.
// master_thread_list / _create / _rename / _archive answer in acks of the same name; master_send takes a thread_id.
//
// This replaces the 9-28 fold (lib/masterVisit: 「接着上次的对话」 as one line under the input): the conversations are
// really apart now, so 总控 opens on a new one and the earlier ones are a click away in the column.

export interface MasterThread {
  threadId: string
  projectId: string
  /** The chat channel holding this conversation's messages. */
  channelId: string
  /** '' = none yet (the first owner message names it on the server). */
  title: string
  /** Epoch milliseconds. */
  createdAt: number
  lastActivityAt: number
  archived: boolean
  /** The project's first conversation, from before conversations were split (「以前的对话」). */
  legacy: boolean
  /** A build of this conversation is working now. */
  runningBuild: boolean
  /** A build of this conversation waits for the owner's answer. */
  waitingOwner: boolean
}

/** A thread id as the server makes them (master_threads.valid_thread_id): nothing else is ever sent or put in an address. */
const THREAD_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/

export function validThreadId(value: unknown): value is string {
  return typeof value === 'string' && THREAD_ID.test(value)
}

/** The channel of a project's conversation: the first one's is `master:{pid}`, a later one's `master:{pid}:{tid}`. */
export function masterChannelId(projectId: string, threadId = ''): string {
  return threadId ? `master:${projectId}:${threadId}` : `master:${projectId}`
}

/** Whether a channel holds one of this project's 总控 conversations (its first or a later one). */
export function isProjectMasterChannel(channelId: string, projectId: string): boolean {
  const first = masterChannelId(projectId)
  return channelId === first || (channelId.startsWith(`${first}:`) && validThreadId(channelId.slice(first.length + 1)))
}

/** A time the server gave, in epoch milliseconds. The contract says milliseconds; a value that can only be seconds
 *  (before 1973 in milliseconds) is read as seconds, so an older server's rows still sort and read right. */
export function epochMs(value: unknown): number {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number) || number <= 0) return 0
  return number < 1e11 ? Math.round(number * 1000) : Math.round(number)
}

/** One thread from a reply, or null when it is not one. The channel is the one the contract names for it; a channel
 *  that names another project or another conversation is never read (the right one is used instead). */
export function threadFrom(raw: unknown, fallbackProjectId = ''): MasterThread | null {
  if (!raw || typeof raw !== 'object') return null
  const row = raw as Record<string, unknown>
  const threadId = String(row.thread_id ?? '').trim()
  if (!validThreadId(threadId)) return null
  const projectId = String(row.project_id ?? '').trim() || fallbackProjectId.trim()
  if (!projectId || projectId.length > 128) return null
  const legacy = row.legacy === true
  const expected = legacy ? masterChannelId(projectId) : masterChannelId(projectId, threadId)
  const channel = String(row.channel_id ?? '').trim()
  return {
    threadId,
    projectId,
    channelId: channel === expected ? channel : expected,
    title: String(row.title ?? '').replace(/\s+/g, ' ').trim().slice(0, 120),
    createdAt: epochMs(row.created_at),
    lastActivityAt: epochMs(row.last_activity_at) || epochMs(row.created_at),
    archived: row.archived === true,
    legacy,
    runningBuild: row.running_build === true,
    waitingOwner: row.waiting_owner === true,
  }
}

/** Each project's conversations as the column last heard them; a project not in it has not been read yet. */
export type ThreadsByProject = Readonly<Record<string, readonly MasterThread[]>>

/** A master_thread_list reply taken in: every project the reply lists is replaced by what it says -- the project it
 *  names (even with nothing in it) and any other project its rows belong to. A refusal changes nothing. */
export function mergeThreadList(current: ThreadsByProject, payload: Record<string, unknown>): ThreadsByProject {
  if (payload.ok !== true || !Array.isArray(payload.threads)) return current
  const named = String(payload.project_id ?? '').trim()
  const listed = (payload.threads as unknown[]).map(item => threadFrom(item, named)).filter((item): item is MasterThread => item !== null)
  const next: Record<string, readonly MasterThread[]> = { ...current }
  const replaced = new Set<string>()
  if (named) { next[named] = []; replaced.add(named) }
  for (const thread of listed) {
    if (!replaced.has(thread.projectId)) { next[thread.projectId] = []; replaced.add(thread.projectId) }
    next[thread.projectId] = [...next[thread.projectId].filter(item => item.threadId !== thread.threadId), thread]
  }
  return next
}

/** One thread a create / rename / archive answered with, put in its project's list (replacing the one with its id). */
export function upsertThread(current: ThreadsByProject, thread: MasterThread): ThreadsByProject {
  const list = current[thread.projectId] ?? []
  const at = list.findIndex(item => item.threadId === thread.threadId)
  const next = at >= 0 ? list.map((item, index) => (index === at ? thread : item)) : [...list, thread]
  return { ...current, [thread.projectId]: next }
}

/** A message landed in a conversation: its last activity moves, so the column's order follows without asking again.
 *  The same object back when nothing moved. */
export function touchThread(current: ThreadsByProject, channelId: string, at: number): ThreadsByProject {
  const when = epochMs(at)
  if (!when) return current
  for (const [projectId, list] of Object.entries(current)) {
    const found = list.findIndex(item => item.channelId === channelId)
    if (found < 0) continue
    if (list[found].lastActivityAt >= when) return current
    return { ...current, [projectId]: list.map((item, index) => (index === found ? { ...item, lastActivityAt: when } : item)) }
  }
  return current
}

const newestFirst = (a: MasterThread, b: MasterThread) =>
  b.lastActivityAt - a.lastActivityAt || b.createdAt - a.createdAt || (a.threadId < b.threadId ? 1 : a.threadId > b.threadId ? -1 : 0)

/** A project's conversations as the column lists them: the ones in use by last activity, newest first; the archived
 *  ones apart, in the same order. */
export function orderThreads(threads: readonly MasterThread[]): { live: MasterThread[]; archived: MasterThread[] } {
  return {
    live: threads.filter(item => !item.archived).sort(newestFirst),
    archived: threads.filter(item => item.archived).sort(newestFirst),
  }
}

/** The project's most recently active conversation that is not archived (the builder-side 安安 panel shows it, as the
 *  server's master_threads.latest_thread_id picks it), or null when there is none. */
export function latestThread(threads: readonly MasterThread[] | undefined): MasterThread | null {
  return orderThreads(threads ?? []).live[0] ?? null
}

/** What a conversation is called in the column: its title, else the first conversation's 「以前的对话」, else 「新对话」. */
export function threadTitle(thread: MasterThread, words: { legacy: string; untitled: string }): string {
  return thread.title || (thread.legacy ? words.legacy : words.untitled)
}

/** The dot before a conversation: something waits for the owner's answer (first: it needs the owner), or a build is
 *  working now; null = no dot. */
export type ThreadDot = 'waiting' | 'running'
export function threadDot(thread: MasterThread): ThreadDot | null {
  if (thread.waitingOwner) return 'waiting'
  if (thread.runningBuild) return 'running'
  return null
}

const two = (value: number) => String(value).padStart(2, '0')

/** How long ago, short, on the owner's clock: 刚刚 / 5 分钟 / 3 小时 / 昨天 / 4 天 / 9月27日 / 2025/9/27 (now / 5m / 3h /
 *  yesterday / 4d / Sep 27 / 2025-09-27). '' when the time names no moment. */
export function relativeWhen(at: number, now: number, zh: boolean): string {
  if (!Number.isFinite(at) || at <= 0) return ''
  const minutes = Math.floor(Math.max(0, now - at) / 60_000)
  if (minutes < 1) return zh ? '刚刚' : 'now'
  if (minutes < 60) return zh ? `${minutes} 分钟` : `${minutes}m`
  const moment = new Date(at)
  const today = new Date(now)
  const dayOf = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()
  const days = Math.round((dayOf(today) - dayOf(moment)) / 86_400_000)
  if (days <= 0) return zh ? `${Math.floor(minutes / 60)} 小时` : `${Math.floor(minutes / 60)}h`
  if (days === 1) return zh ? '昨天' : 'yesterday'
  if (days < 7) return zh ? `${days} 天` : `${days}d`
  if (moment.getFullYear() === today.getFullYear()) {
    return zh ? `${moment.getMonth() + 1}月${moment.getDate()}日` : `${moment.toLocaleString('en', { month: 'short' })} ${moment.getDate()}`
  }
  return zh ? `${moment.getFullYear()}/${moment.getMonth() + 1}/${moment.getDate()}` : `${moment.getFullYear()}-${two(moment.getMonth() + 1)}-${two(moment.getDate())}`
}

export interface ThreadGroup {
  projectId: string
  name: string
  /** The project on screen: first in the column and open. */
  current: boolean
  /** Its list has been read (a project not read yet says so instead of 「还没有对话」). */
  loaded: boolean
  live: MasterThread[]
  archived: MasterThread[]
}

type ProjectLike = { id: string; name?: string; lifecycle_status?: string }

/** The column's groups: the project on screen first, then the console's other projects in the list's order. A project
 *  archived or destroyed is left out (its conversations stay on the server), unless it is the one on screen. */
export function groupThreads(projects: readonly ProjectLike[], byProject: ThreadsByProject, currentProjectId: string): ThreadGroup[] {
  const groupOf = (projectId: string, name: string, current: boolean): ThreadGroup => {
    const list = byProject[projectId]
    return { projectId, name: name || projectId, current, loaded: list !== undefined, ...orderThreads(list ?? []) }
  }
  const currentRow = projects.find(project => project.id === currentProjectId)
  const others = projects.filter(project => project.id && project.id !== currentProjectId
    && !['archived', 'destroyed'].includes(String(project.lifecycle_status || 'active').trim()))
  return [
    groupOf(currentProjectId, currentRow?.name ?? currentProjectId, true),
    ...others.map(project => groupOf(project.id, project.name ?? project.id, false)),
  ]
}

/** Whether this viewer keeps the column collapsed: a per-browser convenience only. Every read and write is guarded --
 *  a private window, blocked site data or a storage that throws leaves the column open and the page working. */
export const COLUMN_COLLAPSED_KEY = 'ampuly.master.threadsCollapsed'
type ColumnStorage = Pick<Storage, 'getItem' | 'setItem'> | null | undefined

/** This browser's local storage, or null where it cannot be reached (no window, blocked site data: the getter throws). */
export function viewerStorage(): ColumnStorage {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

export function loadColumnCollapsed(storage: ColumnStorage): boolean {
  try {
    return storage?.getItem(COLUMN_COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

export function saveColumnCollapsed(storage: ColumnStorage, collapsed: boolean): boolean {
  try {
    if (!storage) return false
    storage.setItem(COLUMN_COLLAPSED_KEY, collapsed ? '1' : '0')
    return true
  } catch {
    return false
  }
}

// ── The open conversation ─────────────────────────────────────────────────────────────────────────────────────────

export interface MasterOpen {
  /** The project the conversation belongs to; '' = the project on screen (an address named the thread before the
   *  project was known). */
  projectId: string
  /** The conversation shown on 总控; '' = a new one (the landing), made by its first send. */
  threadId: string
  /** Which new conversation: every new one begun is a number of its own. */
  fresh: number
  /** Named by the address and not yet found in its project's list. */
  named: boolean
  /** The page's key: a new conversation keeps its key when its first send makes the thread, so the page is not
   *  mounted again under the owner's first message; opening another conversation is a new page. */
  key: string
}

/** 从图标栏进总控 (policy): a new conversation of the project on screen -- the landing, no thread yet. */
export function newConversation(projectId: string, previous?: MasterOpen): MasterOpen {
  const fresh = (previous?.fresh ?? 0) + 1
  return { projectId, threadId: '', fresh, named: false, key: `new-${fresh}` }
}

/** What 总控 opens on when the console loads: the conversation the address names (thread=, so a refresh stays in it),
 *  else a new one. The project is the one the console settles on. */
export function arrivalOpen(addressThreadId: string): MasterOpen {
  return validThreadId(addressThreadId)
    ? { projectId: '', threadId: addressThreadId, fresh: 0, named: true, key: `thread-${addressThreadId}` }
    : { projectId: '', threadId: '', fresh: 0, named: false, key: 'new-0' }
}

/** A conversation clicked in the column: shown whole. */
export function openThread(thread: MasterThread, previous: MasterOpen): MasterOpen {
  return { projectId: thread.projectId, threadId: thread.threadId, fresh: previous.fresh, named: false, key: `thread-${thread.threadId}` }
}

/** A click on a conversation: another project's is opened after the console switches to that project (the existing
 *  project-switch road), so a conversation and its project are always on screen together. */
export function openPlan(thread: MasterThread, activeProjectId: string): { switchTo: string } {
  return { switchTo: thread.projectId !== activeProjectId ? thread.projectId : '' }
}

/** Why the address's conversation was not opened: gone (not in its project's list), or archived. */
export type OpenNote = '' | 'thread_gone' | 'thread_archived'

/** Where the console stands: the project on screen, a switch under way (null = none), and the project whose history
 *  (the sync that settles a project) the console holds -- until that is the project on screen, the project on screen
 *  may still be the placeholder of a console that is loading, and nothing is decided against it. */
export interface ConsoleStand {
  activeProjectId: string
  pendingProjectId: string | null
  historyProjectId: string
}

/** The open conversation checked against what the console knows now. Nothing moves while a switch is under way or
 *  before the project on screen has settled. A conversation of a project that is no longer on screen (the console
 *  moved by another road) gives way to a new one of the project on screen. A conversation the address named is looked
 *  for once its project's list is in: found, it stays; archived or not there, a new conversation opens with a note.
 *  The same object back when nothing changes. */
export function resolveOpen(open: MasterOpen, stand: ConsoleStand, byProject: ThreadsByProject): { open: MasterOpen; note: OpenNote } {
  const { activeProjectId, pendingProjectId, historyProjectId } = stand
  if (pendingProjectId || !activeProjectId || historyProjectId !== activeProjectId) return { open, note: '' }
  if (open.projectId && open.projectId !== activeProjectId) return { open: newConversation(activeProjectId, open), note: '' }
  const pinned = open.projectId ? open : { ...open, projectId: activeProjectId }
  if (!pinned.threadId) return { open: pinned, note: '' }
  const list = byProject[activeProjectId]
  if (list === undefined) return { open: pinned, note: '' }
  const found = list.find(item => item.threadId === pinned.threadId)
  // Only the address's conversation is given up when a list lacks it: one made or clicked here stays -- a list asked
  // for before it was made may land after it -- and a send into one that is really gone is refused by the server,
  // which opens a new conversation with a note (App, master_send thread_not_found).
  if (!found) return pinned.named ? { open: newConversation(activeProjectId, open), note: 'thread_gone' } : { open: pinned, note: '' }
  if (found.archived) return { open: newConversation(activeProjectId, open), note: 'thread_archived' }
  return { open: pinned.named ? { ...pinned, named: false } : pinned, note: '' }
}

/** What a send from 总控 does: into the open conversation; in a new one, its first send makes the thread (create, then
 *  send with that thread) -- a second send before the thread is back waits for it -- so no empty conversation piles up;
 *  while the console is still switching to the conversation's project, nothing. */
export type SendPlan = { kind: 'send'; threadId: string } | { kind: 'create' } | { kind: 'queue' } | { kind: 'wait' }
export function sendPlan(open: MasterOpen, activeProjectId: string, creating: { projectId: string; fresh: number } | null): SendPlan {
  if ((open.projectId || activeProjectId) !== activeProjectId) return { kind: 'wait' }
  if (open.threadId) return { kind: 'send', threadId: open.threadId }
  if (creating && creating.projectId === activeProjectId && creating.fresh === open.fresh) return { kind: 'queue' }
  return { kind: 'create' }
}

/** The conversation is named but its messages are not on hand yet (its project's history or list still coming): the
 *  page says it is opening it rather than showing the landing and turning into the conversation a moment later. */
export function conversationOpening(open: MasterOpen, messageCount: number, stand: ConsoleStand): boolean {
  if (!open.threadId || messageCount > 0) return false
  return open.named || !!stand.pendingProjectId || stand.historyProjectId !== (open.projectId || stand.activeProjectId)
}

/** Whether 安安's running turn is this conversation's. `turnThreadId` is what the server said: null = it named no
 *  conversation (an older server: the turn is shown wherever the project's conversation is), '' = the project's first
 *  conversation, else that conversation's id. */
export function turnBelongsTo(turnThreadId: string | null, view: { threadId: string; legacy: boolean }): boolean {
  if (turnThreadId === null) return true
  if (turnThreadId === '') return view.legacy
  return view.threadId === turnThreadId
}
