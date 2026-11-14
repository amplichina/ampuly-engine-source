// Stable page addresses without a router library (frontend refactor U1). Pure string work over the query string.
//
// `?page=<id>` names the page. The approval deep link Feishu's audit button opens
// (`?page=approvals&checkpoint_id=…&project_id=…`, parsed by readConsoleDeepLink in App.tsx) keeps its own guarded
// path: this module never chooses a project or a checkpoint. A settled project can be written back explicitly.
// What it does know (S3-7, policy) is
// where the four retired page keys land, so an old address never 404s and never shows an empty page.

import { isRestorableAppPage, type RestorableAppPage } from './appPageState'

/** The page a URL names, or null when it names none (or one that does not exist: a stale link lands on the normal
 *  start page instead of an error). A retired key is "none" here; `retiredPageLanding` says where it goes. */
export function pageFromSearch(search: string): RestorableAppPage | null {
  try {
    const page = String(new URLSearchParams(search || '').get('page') || '').trim().toLowerCase()
    return isRestorableAppPage(page) ? page : null
  } catch {
    return null
  }
}

/** The four page keys retired by S3-7. Their addresses are still understood, their pages are gone. */
export const RETIRED_PAGE_KEYS = ['approvals', 'workspace', 'capabilities', 'org'] as const
export type RetiredPageKey = typeof RETIRED_PAGE_KEYS[number]

export type RetiredLanding =
  /** 待审批: 业务工单 on 等你处理 (policy) -- its cards were 总控's rows until 总控 became the conversation
   * alone; the orders that wait on the owner are handled in 业务工单 now. */
  | { from: RetiredPageKey; page: 'orders'; group: 'mine'; taskId?: undefined; section?: undefined }
  /** 工作台: 总控 -- with 执行区's side window open over the run when the address names one (`task_id`, or
   *  `session_id`), else 总控 alone. */
  | { from: RetiredPageKey; page: 'master'; taskId: string; section?: undefined; group?: undefined }
  /** 控制面 → 配置 · 服务与连接 (its connectors and credentials); 组织 → 配置 · 组织与角色. */
  | { from: RetiredPageKey; page: 'config'; section: 'connect' | 'org'; taskId?: undefined; group?: undefined }

/** Where an address naming a retired page lands, or null when it names none of them. */
export function retiredPageLanding(search: string): RetiredLanding | null {
  let query: URLSearchParams
  try {
    query = new URLSearchParams(search || '')
  } catch {
    return null
  }
  const page = String(query.get('page') || '').trim().toLowerCase()
  if (page === 'approvals') return { from: 'approvals', page: 'orders', group: 'mine' }
  if (page === 'workspace') {
    const taskId = String(query.get('task_id') || query.get('session_id') || '').trim()
    return { from: 'workspace', page: 'master', taskId }
  }
  if (page === 'capabilities') return { from: 'capabilities', page: 'config', section: 'connect' }
  if (page === 'org') return { from: 'org', page: 'config', section: 'org' }
  return null
}

/** The query string for being on `page`. Other parameters are kept, except what an approval link or a workspace
 *  link carried (its checkpoint, its run): consumed once where they land (业务工单 for a card, 总控 for a run), a
 *  reload must not reopen a card or a run the person walked away from. */
export function searchForPage(search: string, page: RestorableAppPage): string {
  let query: URLSearchParams
  try {
    query = new URLSearchParams(search || '')
  } catch {
    query = new URLSearchParams()
  }
  query.set('page', page)
  // A deep link's checkpoint / task / session is consumed once at mount (deepLinkRef); left in the address it would
  // re-run on every reload -- bouncing the owner to a card they already settled (review of S3-7). Whatever the page.
  query.delete('checkpoint_id')
  query.delete('checkpoint')
  query.delete('task_id')
  query.delete('session_id')
  // 总控's conversation (thread=, 总控对话栏 2026-09-29) belongs to 总控 alone: on any other page it is dropped, so it
  // never follows the owner to another page and back.
  if (page !== 'master') query.delete('thread')
  return `?${query.toString()}`
}

/** The 总控 conversation the address names (总控对话栏, policy: a refresh stays in the conversation it was
 * in), or '' when it names none or something that is not a thread id. Whether that conversation exists is not this
 * module's to say: 总控 looks for it in its project's list (lib/masterThreads resolveOpen). */
export function threadFromSearch(search: string): string {
  try {
    const thread = String(new URLSearchParams(search || '').get('thread') || '').trim()
    return /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(thread) ? thread : ''
  } catch {
    return ''
  }
}

/** The query string for 总控 showing `threadId` ('' = a new conversation: no thread in the address). Other parameters
 *  are kept. */
export function searchForThread(search: string, threadId: string): string {
  let query: URLSearchParams
  try {
    query = new URLSearchParams(search || '')
  } catch {
    query = new URLSearchParams()
  }
  if (threadId) query.set('thread', threadId)
  else query.delete('thread')
  const text = query.toString()
  return text ? `?${text}` : ''
}

/** Called only after the project has settled and the initial deep link has been consumed. A copied/reloaded
 * address must name the project on screen. A conversation/run from another project must not travel with it. */
export function searchForSettledProject(search: string, projectId: string): string {
  const next = projectId.trim()
  if (!next) return search
  const query = new URLSearchParams(search || '')
  const previous = query.get('project_id')
  if (previous === next) return search
  if (previous) for (const key of ['checkpoint_id', 'checkpoint', 'task_id', 'session_id', 'thread']) query.delete(key)
  query.set('project_id', next)
  return `?${query.toString()}`
}
