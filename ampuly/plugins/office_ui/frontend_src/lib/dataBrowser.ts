// What the 「资料」 entry holds on to, and how a reply changes it -- data phase 2, D8. Pure: no fetch, no wsClient,
// no storage, no React. The server decides what may be seen (D4/D6); this file only keeps what came back, keyed by
// the project it belongs to, so a reply that arrives after a project switch can never be shown under the new one.

export type DataEntry = {
  name: string
  path: string
  kind: 'dir' | 'file'
  file_kind?: string
  size?: number
  modified_at?: string
  blocked?: string | null
}

export type FolderPage = {
  folder: string
  area?: string
  order_id?: string
  customer_id?: string
  label?: string
  exists: boolean
  entries: DataEntry[]
  offset?: number
  next_offset?: number
  truncated?: boolean
  truncated_reason?: string
  links_skipped?: number
  total?: number
}

export type OrderRow = {
  order_id: string
  status?: string
  workflow_id?: string
  customer_id?: string
  customer_name?: string
  created_at?: string
  has_folder?: boolean
}

export type MaterialCustomer = { customer_id: string; segment: string; exists: boolean }

export type ProjectIndex = {
  viewer: { owner: boolean; bound: boolean }
  exists: boolean
  orders: OrderRow[]
  orders_truncated?: boolean
  orders_unavailable?: boolean
  other_order_folders: string[]
  materials: {
    exists: boolean
    common: boolean
    customers: MaterialCustomer[]
    loose: { files: number | null; folders: number | null } | null
  }
  attachments: { count: number; truncated: boolean }
  usage: UsagePayload | null
}

export type StepFolder = { kind: string; path: string; exists: boolean; files: number | null }
export type StepDeliverable = { path?: string; exists?: boolean | null; pattern?: string; matched?: number | null }
export type OrderStep = { id: string; name: string; segment: string; folders: StepFolder[]; deliverables: StepDeliverable[] }

export type OrderView = {
  kind: 'order' | 'legacy_folder'
  order: OrderRow
  step_table: { state: string; dropped: number }
  steps: OrderStep[]
  areas: { area: string; path: string; label?: string; exists: boolean; files: number | null; folders?: number | null }[]
  counts_complete: boolean
  history: { path: string; exists: boolean; rounds: DataEntry[] }
  exports: { path: string; exists: boolean; files: DataEntry[] }
  portfolio?: { path: string; exists: boolean; files: DataEntry[] }
  customer_id?: string
}

export type AttachmentRow = {
  attachment_id: string
  name: string
  size: number
  modified_at: string
  disk_path: string
  blocked?: string | null
}

export type AttachmentPage = {
  exists: boolean
  attachments: AttachmentRow[]
  offset?: number
  next_offset: number
  truncated: boolean
  total: number
}

export type UsagePayload = {
  buckets: Record<string, { bytes: number; files: number }>
  archives?: { count: number; bytes: number }
  total_bytes: number
  links_skipped: number
  complete: boolean
  entries_seen: number
  elapsed_ms?: number
  computed_at?: string
}

export type UsageEntry = {
  usage: UsagePayload | null
  cached: boolean
  computing: boolean
  refresh_after: number
  received_at: number
}

export type PreviewPayload = {
  path: string
  name: string
  kind: string
  size: number
  modified_at: string
  content: string
  truncated: boolean
  data_url: string
  note: string
  area?: string
  order_id?: string
  customer_id?: string
}

export type DataLocation = {
  key: string; label: string; path: string; exists: boolean; note: string
  /** 数据与文件 P3: read off core/data_map — what kind of data this is, how long it is kept, and whether a
   *  backup carries it (with the reason when it does not). Optional so an older server still renders. */
  kind?: string
  retention?: string
  in_backup?: boolean
  reason?: string
}

/** 保留期说给人听。产品不认识的写法原样显示，不猜。 */
export function retentionText(retention: string | undefined, zh: boolean): string {
  if (retention === 'forever') return zh ? '永久保留' : 'kept for good'
  if (retention === 'days_30') return zh ? '30 天后清理' : 'cleaned after 30 days'
  return retention ?? ''
}

/** 数据与文件 P3: one backup, as the list shows it. 「在备份里」和「还原后就回来了」不是同一个承诺，
 *  所以 holds 里每一项自己说 restored。 */
export type BackupHold = { key: string; label: string; files: number; counts: Record<string, number>; restored: boolean }

export type BackupRow = {
  name: string; project_id: string; taken_at: string; bytes: number; entries: number
  schema_version: number; holds: BackupHold[]; restorable: boolean; note: string; blocked: boolean
}

/** 自动备份上次跑得怎么样，以及**它备的是哪个家目录**。 */
export type AutoBackupRun = {
  ran: boolean
  why_zh?: string
  ok?: boolean
  at?: string
  home?: string
  note?: string
  protects_this_home?: boolean
  pruned?: string[]
}

export type AutoBackupHealth = {
  this_home: string
  daily: AutoBackupRun
  weekly: AutoBackupRun
  warnings: { code: string; why_zh: string }[]
}

/** 这台机器上装过的一个备份计划任务。**连别的安装装的也列**——光看名字看不出它备的是谁。 */
export type BackupTask = {
  name: string
  arguments: string
  state: string
  last_result: number
  last_run: string
  next_run: string
  protects: string
  protects_implicitly: boolean
  this_install: boolean
}

/** 一个任务的一句话。重点永远是「它保护的是哪儿」，不是「它成功了吗」。 */
export function taskProtectsText(task: BackupTask): string {
  if (!task.protects) return '看不出它备的是哪儿'
  const how = task.protects_implicitly ? '（它没写明备哪儿，是按程序所在位置推出来的）' : ''
  return `${task.this_install ? '备的就是这里的数据' : '备的是另一套'}：${task.protects}${how}`
}

export type BackupList = {
  root: string; total: number; offset: number; limit: number; backups: BackupRow[]
  auto?: AutoBackupHealth
  bytes?: number
  tasks?: BackupTask[]
}

/** 一档自动备份，拆成可以分开排版的几段。

重点永远是「它保护的是哪儿」，不是「它成功了吗」——2026-09-24 那个骗过所有人的任务也是成功的。
拆开是因为这是整页最重要的一行：四件事挤成一句跑马句，同一个字重，眼睛就没有落点。 */
export type AutoRunParts = {
  ran: boolean
  ok: boolean
  /** 徽章上的那两个字，或者「没跑过」。 */
  badge: string
  /** 时间 + 备的是哪儿，一眼要看的就这两样。 */
  head: string
  /** 次要细节，另起一行。 */
  detail: string
  /** 备的不是这一套数据——界面据此变红。 */
  wrongHome: boolean
}

export function autoRunParts(run: AutoBackupRun | undefined, zh = true): AutoRunParts {
  if (!run || !run.ran) {
    return {
      ran: false, ok: false, wrongHome: false,
      badge: zh ? '没跑过' : 'never ran',
      head: run?.why_zh ?? (zh ? '还没有跑过' : 'has not run yet'),
      detail: '',
    }
  }
  const when = run.at ? run.at.replace('T', ' ').slice(0, 16) : (zh ? '时间不详' : 'time unknown')
  const wrongHome = run.protects_this_home === false
  const where = wrongHome
    ? (zh ? `备的不是这里的数据：${run.home || '不详'}` : `protects another home: ${run.home || 'unknown'}`)
    : (zh ? '备的就是这里的数据' : 'protects this home')
  return {
    ran: true,
    ok: Boolean(run.ok),
    wrongHome,
    badge: run.ok ? (zh ? '成功' : 'ok') : (zh ? '失败' : 'failed'),
    head: `${when} · ${where}`,
    detail: run.note ?? '',
  }
}

/** 一行说「里面有什么」。没有数目就只说名字——编一个数字出来比不说更糟。 */
export function holdText(hold: BackupHold, _zh: boolean): string {
  const counts = Object.entries(hold.counts ?? {})
  if (counts.length) return `${hold.label} ${counts.map(([, n]) => n).reduce((a, b) => a + b, 0)}`
  if (hold.files) return `${hold.label} ${hold.files}`
  return hold.label
}

/** 数据与文件 P3: 「看看会删掉什么」。**只列不删**：这一期没有执行的通路。 */
export type CleanupFamily = {
  key: string; label_zh: string; root: string; unit_zh: string
  items: number; files: number; bytes: number; oldest: string; newest: string
  sample: { name: string; files: number; bytes: number; modified: string }[]
}

export type CleanupPreview = {
  families: CleanupFamily[]
  totals: { items: number; files: number; bytes: number }
  /** 闸拦下了多少。只看「要删几项」无从判断规则对不对，看见拦下了什么才是可检验的。 */
  kept: { protected: number; too_young: number; unknown: number }
  keep_days: number; made_at: string; scanned: number; incomplete: boolean
  token: string; never_removed: string[]
}

/** 数据与文件 P3: 恢复完，备份里有哪一样没有放回去。回执里是归档成员名，这里换成人话。 */
export type NotRestored = { member: string; why_zh: string }

const MEMBER_LABELS: Record<string, string> = {
  'chat/messages.json': '聊天',
  'business/budgets.json': '预算',
  'business/history.json': '项目历史',
  'config/approval_allowlist.json': '授权清单',
  'runtime/global.db': '运行库',
}

export function memberLabel(member: string): string {
  return MEMBER_LABELS[member] ?? member
}

/** 数据与文件 P3: 删掉一个项目会带走什么、不会带走什么，以及现在能不能删。 */
export type RemovalPlan = {
  project_id: string
  goes: Record<string, Record<string, number>>
  stays: Record<string, Record<string, number>>
  blocked_by: { code: string; why_zh: string }[]
  backup: { name: string; taken_at: string; blocked: boolean } | null
  token: string
}

/** 「会带走 12 个文件、3 条工单」这样的一句话。数目全为零就返回空串，不说废话。 */
export function removalCounts(part: Record<string, Record<string, number>>): string {
  const parts: string[] = []
  for (const [, counts] of Object.entries(part ?? {})) {
    const total = Object.values(counts ?? {}).reduce((a, b) => a + (Number(b) || 0), 0)
    if (total) parts.push(String(total))
  }
  return parts.join(' + ')
}

/** 删掉的东西腾出来、却还没还给磁盘的那部分。只读的一次测量。 */
export type DeadSpace = {
  databases: { name: string; path: string; bytes: number; dead_bytes: number; dead_share: number }[]
  bytes: number
  dead_bytes: number
  dead_share: number
  worth_reclaiming: boolean
  why_zh: string
}

/** 每个运行库在表结构第几版（4.7-A19）。还没有版本号的库系统只读不改，要你确认后自己运行 `command`。 */
export type SchemaVersions = {
  databases: { name: string; label: string; path: string; kind: string; version: number | null; current: number; state: string }[]
  needs_owner: boolean
  command: string
  why_zh: string
}

/** 这些路径共同的那一段目录。

19 行里重复 19 遍同一个前缀是噪音，不是信息：它挤掉了每一行真正不同的那部分。前缀说一次，剩下的让人
一眼看出差别。 */
export function commonRoot(paths: string[]): string {
  const kept = paths.filter(Boolean)
  if (kept.length < 2) return ''
  const parts = kept.map(one => one.split(/[\\/]/))
  const head: string[] = []
  for (let i = 0; i < parts[0].length; i += 1) {
    const piece = parts[0][i]
    if (parts.every(one => one[i] === piece)) head.push(piece)
    else break
  }
  // 用输入里实际在用的那个分隔符拼回去。总是用反斜杠的话，正斜杠的路径（Docker/Linux 部署）算出来的
  // 前缀不是原串的前缀，`shortPath` 就一个字都缩不掉——本机是反斜杠，这个错在这台机器上看不出来。
  const sep = kept[0].includes('\\') ? '\\' : '/'
  return head.length > 1 ? head.join(sep) : ''
}

/** 去掉公共前缀之后剩下的那一段。整条就是前缀本身时，说人话而不是留一个空白。 */
export function shortPath(path: string, root: string, zh = true): string {
  if (!root || !path.startsWith(root)) return path
  const rest = path.slice(root.length).replace(/^[\\/]+/, '')
  return rest || (zh ? '（总目录本身）' : '(the root itself)')
}

/** 位置清单的一句话：多少个、各自什么待遇。

折叠之前，摘要要担得起「不用展开也知道大概」——否则折叠就只是把东西藏起来。 */
export function locationsSummary(
  rows: { retention?: string; in_backup?: boolean }[], zh: boolean,
): string {
  const total = rows.length
  const forever = rows.filter(row => row.retention === 'forever').length
  const cleaned = rows.filter(row => row.retention && row.retention !== 'forever').length
  const out = rows.filter(row => row.in_backup === false).length
  return zh
    ? `一共 ${total} 处 · ${forever} 处永久保留 · ${cleaned} 处到期自动清理 · ${out} 处不进备份`
    : `${total} places · ${forever} kept for good · ${cleaned} cleaned on a schedule · ${out} not in a backup`
}

export type DataLocations = {
  locations: DataLocation[]
  workplace_set_by: string
  data_area_inside_system_dir: boolean
  later: { key: string; phase: number; where: string }[]
  dead_space?: DeadSpace
  schema_versions?: SchemaVersions
}

export type DataAck = {
  ok?: boolean
  action?: string
  project_id?: string
  error?: string
  [key: string]: unknown
}

export type DataState = {
  projectId: string
  index: ProjectIndex | null
  orders: Record<string, OrderView>
  folders: Record<string, FolderPage>
  attachments: AttachmentPage | null
  preview: PreviewPayload | null
  errors: Record<string, string>
}

export const EMPTY_DATA: DataState = {
  projectId: '', index: null, orders: {}, folders: {}, attachments: null, preview: null, errors: {},
}

export type DataAction =
  | { kind: 'project'; projectId: string }
  | { kind: 'clearPreview' }
  | { kind: 'ack'; ack: DataAck }

/** The entry a 资料 reply belongs to, so a reply can replace exactly what it answered. */
function ackKey(ack: DataAck): string {
  const action = String(ack.action ?? '')
  if (action === 'data_folder_list') return String((ack.folder as string) ?? '')
  if (action === 'data_order_view') return String((ack.order as OrderRow | undefined)?.order_id ?? ack.order_id ?? '')
  return ''
}

/** What the page keeps. A reply for another project is dropped: the caller guards on the active project as well. */
export function dataReducer(state: DataState, action: DataAction): DataState {
  if (action.kind === 'project') {
    return action.projectId === state.projectId ? state : { ...EMPTY_DATA, projectId: action.projectId }
  }
  if (action.kind === 'clearPreview') return { ...state, preview: null }
  const ack = action.ack || {}
  const name = String(ack.action ?? '')
  const project = String(ack.project_id ?? '')
  if (!name.startsWith('data_')) return state
  if (state.projectId && project && project !== state.projectId) return state
  if (ack.ok === false) {
    return { ...state, errors: { ...state.errors, [name + (ackKey(ack) ? ':' + ackKey(ack) : '')]: String(ack.error ?? 'error') } }
  }
  const errors = { ...state.errors }
  delete errors[name]
  delete errors[name + ':' + ackKey(ack)]
  const next: DataState = { ...state, errors, projectId: state.projectId || project }
  if (name === 'data_project_index') {
    const fresh = ack as unknown as ProjectIndex
    next.index = fresh
    // The index counted the uploads again. A list that disagrees with that count is a list of rows that are no
    // longer there, so it is dropped and the page offers to read it again rather than showing what was deleted.
    if (state.attachments && Number(fresh.attachments?.count ?? -1) !== Number(state.attachments.total ?? -1)) {
      next.attachments = null
    }
  } else if (name === 'data_order_view') {
    const view = ack as unknown as OrderView
    const id = String(view.order?.order_id ?? ackKey(ack))
    if (id) next.orders = { ...state.orders, [id]: view }
  } else if (name === 'data_folder_list') {
    const page = ack as unknown as FolderPage
    const key = String(page.folder ?? '')
    const known = state.folders[key]
    const merged = Number(page.offset ?? 0) > 0 && known
      ? { ...page, entries: [...known.entries, ...(page.entries || [])] }
      : page
    next.folders = { ...state.folders, [key]: merged }
  } else if (name === 'data_preview') {
    next.preview = ack as unknown as PreviewPayload
  } else if (name === 'data_attachments') {
    const page = ack as unknown as AttachmentPage
    const known = state.attachments
    next.attachments = Number(page.offset ?? 0) > 0 && known
      ? { ...page, attachments: [...known.attachments, ...(page.attachments || [])] }
      : page
  }
  return next
}

/** How much space each project takes, kept per project id.
 *
 * Not reset on a project switch: the owner's own 磁盘位置 page (D9) asks about projects other than the active one,
 * and a walk that was started before a switch still answers the project it was started for. A reply for a project
 * this console does not know about is dropped.
 */
export function usageReducer(
  map: Record<string, UsageEntry>,
  ack: DataAck,
  knownProjects: readonly string[],
  now = 0,
): Record<string, UsageEntry> {
  if (String(ack.action ?? '') !== 'data_usage' || ack.ok === false) return map
  const project = String(ack.project_id ?? '')
  if (!project || (knownProjects.length > 0 && !knownProjects.includes(project))) return map
  return {
    ...map,
    [project]: {
      usage: (ack.usage as UsagePayload | null) ?? null,
      cached: Boolean(ack.cached),
      computing: Boolean(ack.computing),
      refresh_after: Number(ack.refresh_after ?? 0),
      received_at: now,
    },
  }
}

/** Where one file of a project is served. The path is a query value, so nothing in a name can change the route. */
export function dataDownloadUrl(projectId: string, path: string): string {
  return `/api/data/${encodeURIComponent(projectId)}/download?path=${encodeURIComponent(path)}`
}

export const AREA_LABELS: Record<string, [string, string]> = {
  orders: ['工单', 'Orders'],
  output: ['交付（全部）', 'Delivery (all)'],
  inputs: ['输入', 'Inputs'],
  audit: ['操作记录', 'Operation records'],
  downloads: ['下载（旧）', 'Downloads (legacy)'],
  steps: ['步骤', 'Steps'],
  work: ['工作文件', 'Working files'],
  writes: ['交付', 'Delivered'],
  'delivery-history': ['验收历史', 'Acceptance rounds'],
  'delivery-exports': ['导出', 'Exports'],
  'portfolio-exports': ['作品集导出', 'Portfolio exports'],
  'project-materials': ['资料', 'Materials'],
  common: ['公共', 'Shared'],
  customers: ['客户资料', 'Per customer'],
  step_table: ['步骤表', 'Step table'],
}

export const ERROR_LABELS: Record<string, [string, string]> = {
  project_not_found: ['没有这个项目', 'No such project'],
  project_destroyed: ['这个项目已经删除', 'This project was destroyed'],
  customer_scope_denied: ['这不在你负责的客户范围内', 'Outside your customers'],
  owner_only: ['只有所有者能看', 'Owner only'],
  invalid_path: ['这个位置打不开', 'That location cannot be opened'],
  not_found: ['没有找到', 'Not found'],
  refused_link: ['这是一个链接，出于安全没有打开', 'A link: not followed, for safety'],
  multiply_linked: ['这个文件在别处还有一个名字，没有打开', 'This file has a second name on disk; not opened'],
  too_large: ['文件太大，不能在这里打开', 'Too large to open here'],
  unreadable: ['读不了这个文件', 'Could not be read'],
  folder_changed: ['文件夹刚刚变了，请重试', 'The folder changed; try again'],
  folder_too_large: ['这个文件夹太大了', 'That folder is too large'],
  busy: ['正在忙，请稍后再试', 'Busy; try again shortly'],
  not_this_machine: ['这个控制台不在本机运行，打不开本机的文件夹', 'This console runs on another machine; it cannot open a folder here'],
  unsupported: ['这个系统上打不开文件管理器', 'No file manager on this system'],
  error: ['出了点问题', 'Something went wrong'],
}

export function areaLabel(area: string, zh: boolean): string {
  const pair = AREA_LABELS[area]
  return pair ? (zh ? pair[0] : pair[1]) : area
}

export function errorLabel(code: string, zh: boolean): string {
  const pair = ERROR_LABELS[code] ?? ERROR_LABELS.error
  return zh ? pair[0] : pair[1]
}

/** A size a person reads, not a number of bytes. */
export function formatBytes(bytes: number | null | undefined): string {
  const value = Number(bytes ?? 0)
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  const shown = size >= 100 || unit === 0 ? Math.round(size) : Number(size.toFixed(1))
  return `${shown} ${units[unit]}`
}
