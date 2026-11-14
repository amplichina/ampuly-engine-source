// 项目文件夹: what the chooser and the panel hold on to, and the words for what the server refused. Pure -- no
// fetch, no wsClient, no storage, no React. Every path in here came from a reply; the console composes none.

export type DirEntry = {
  name: string
  path: string
  kind?: string
  link?: boolean
  writable?: boolean
  refused?: string
  warn?: string[]
}

export type DirPage = {
  kind: 'drives' | 'dir'
  path: string
  parent: string
  entries: DirEntry[]
  next_offset: number
  truncated: boolean
  can_create: boolean
}

export type FolderCheck = { code: string; warn: string[]; names: string[] }

export type FolderPlan = {
  source: string
  target: string
  files: number
  dirs: number
  bytes: number
  buckets: Record<string, { bytes: number; files: number }>
  complete: boolean
  same_volume: boolean
  token: string
}

export type PlanReply = {
  scope: 'project' | 'default'
  source: string
  target: string
  check: FolderCheck
  plan: FolderPlan | null
  pins: { project_id: string; path: string }[]
  pins_truncated: boolean
  busy: string
  token: string
  expires_in: number
}

export type LegacyFolder = { path: string; files: number; bytes: number; token: string; complete: boolean }

export type FolderState = {
  projectId: string
  open: boolean
  scope: 'project' | 'default'
  page: DirPage | null
  looking: boolean
  /** Where the chooser is pointing right now -- always a path a reply gave us. */
  selected: string
  plan: PlanReply | null
  /** The old folder's files, while the owner has not dealt with them. */
  legacy: LegacyFolder | null
  /** What an import could not remove, because it was not ours: the owner has to clear it by hand. */
  leftovers: { path: string; entries: number } | null
  /** The machine's own dialog is open in front of the owner. */
  picking: boolean
  importing: boolean
  progress: { done: number; total: number } | null
  error: string
  note: string
}

export const EMPTY_FOLDER_STATE: FolderState = {
  projectId: '', open: false, scope: 'project', page: null, looking: false, selected: '',
  plan: null, legacy: null, leftovers: null, picking: false, importing: false, progress: null, error: '', note: '',
}

export type FolderAck = { ok?: boolean; action?: string; project_id?: string; error?: string; [key: string]: unknown }

export type FolderAction =
  | { kind: 'project'; projectId: string }
  | { kind: 'open'; scope: 'project' | 'default' }
  | { kind: 'close' }
  | { kind: 'looking' }
  | { kind: 'picking'; scope: 'project' | 'default' }
  | { kind: 'select'; path: string }
  | { kind: 'ack'; ack: FolderAck }

export function folderReducer(state: FolderState, action: FolderAction): FolderState {
  if (action.kind === 'project') {
    return action.projectId === state.projectId ? state : { ...EMPTY_FOLDER_STATE, projectId: action.projectId }
  }
  if (action.kind === 'open') {
    return { ...state, open: true, scope: action.scope, page: null, plan: null, selected: '', error: '', looking: true }
  }
  if (action.kind === 'close') return { ...state, open: false, page: null, plan: null, selected: '', error: '' }
  if (action.kind === 'looking') return { ...state, looking: true, error: '' }
  if (action.kind === 'picking') {
    return { ...state, picking: true, scope: action.scope, plan: null, selected: '', error: '', note: '',
             leftovers: null }
  }
  if (action.kind === 'select') return { ...state, selected: action.path, plan: null }

  const ack = action.ack || {}
  const name = String(ack.action ?? '')
  const project = String(ack.project_id ?? '')
  if (state.projectId && project && project !== state.projectId) return state
  if (ack.ok === false) {
    return { ...state, looking: false, picking: false, importing: false, error: String(ack.error ?? 'error') }
  }
  if (name === 'data_dir_pick') {
    // Cancelling the dialog is an answer, not a failure: nothing is said and nothing changes.
    return { ...state, picking: false, selected: String(ack.path ?? ''), error: '' }
  }
  if (name === 'data_dir_browse') {
    return { ...state, looking: false, error: '', page: ack as unknown as DirPage }
  }
  if (name === 'data_dir_create') {
    const entry = ack.entry as DirEntry | undefined
    return { ...state, looking: false, error: '', selected: entry?.path ?? state.selected }
  }
  if (name === 'data_folder_legacy') {
    const found = (ack.legacy as LegacyFolder | null) ?? null
    return { ...state, looking: false, error: '', legacy: found }
  }
  if (name === 'data_folder_plan') {
    return { ...state, looking: false, error: '', plan: ack as unknown as PlanReply }
  }
  if (name === 'data_folder_set') {
    const legacy = (ack.legacy as LegacyFolder | null) ?? null
    return { ...state, open: false, page: null, plan: null, selected: '', error: '', legacy,
             note: legacy ? 'pointed_with_legacy' : 'pointed' }
  }
  if (name === 'data_folder_import') {
    if (String(ack.phase ?? '') === 'importing') {
      return { ...state, importing: true,
               progress: { done: Number(ack.done_bytes ?? 0), total: Number(ack.total_bytes ?? 0) } }
    }
    const left = ack.leftovers as { path?: unknown; entries?: unknown } | null | undefined
    return { ...state, importing: false, progress: null, legacy: null, note: 'imported',
             leftovers: left ? { path: String(left.path ?? ''), entries: Number(left.entries ?? 0) } : null }
  }
  return state
}

/** What the server refused, in words. Codes only -- a reply never carries an operating system's own message. */
export const FOLDER_ERROR_LABELS: Record<string, [string, string]> = {
  owner_only: ['只有所有者能改项目文件夹', 'Owner only'],
  not_this_machine: ['这个控制台不在本机运行，选不了本机的文件夹', 'This console runs on another machine'],
  invalid_path: ['这个位置用不了', 'That location cannot be used'],
  invalid_name: ['这个名字文件系统不接受', 'The file system will not take that name'],
  pick_timed_out: ['选择文件夹的窗口一直没有关，已经取消', 'The folder dialog was left open; cancelled'],
  unsupported: ['这台机器上打不开文件夹选择窗口', 'No folder dialog on this machine'],
  name_taken: ['同名的文件夹已经存在', 'A folder with that name is already there'],
  network_path: ['网络位置用不了：任务在共享盘上跑不起来', 'A network location cannot be used'],
  refused_link: ['这是一个链接，不能用作项目文件夹', 'That is a link, not a folder of its own'],
  drive_root: ['不能直接用盘符根目录，请在里面建一个文件夹', 'Not a drive root; make a folder inside it'],
  inside_ampuly_home: ['不能放在系统目录里', 'Not inside the system directory'],
  inside_product_repo: ['不能放在程序目录里', 'Not inside the program directory'],
  inside_project_area: ['这里和别的项目的文件夹重叠了', 'That overlaps another project’s folder'],
  root_has_project_names: ['这个文件夹里已经有同名的项目目录', 'That folder already holds a project of the same name'],
  same_folder: ['这就是这个项目现在用的文件夹', 'That is the folder this project already uses'],
  target_not_empty: ['要换到一个空文件夹', 'Choose an empty folder'],
  not_writable: ['这个位置写不进去', 'That location cannot be written to'],
  not_readable: ['这个文件夹读不了', 'That folder cannot be read'],
  not_found: ['没有找到', 'Not found'],
  order_running: ['这个项目有工单正在跑，先等它结束', 'An order is running in this project'],
  project_busy: ['这个项目有任务正在跑，先等它结束', 'A task is running in this project'],
  plan_stale: ['刚才的信息已经变了，请重新选一次', 'Things changed; choose again'],
  plan_incomplete: ['这个文件夹太大，一次数不完，暂时不能一键导入', 'Too large to count in one go'],
  import_conflict: ['新文件夹里已经有同名但不一样的文件，没有覆盖', 'Files of the same name are already there'],
  import_failed: ['导入没有完成，文件还在原来的位置', 'The import did not finish; nothing was lost'],
  verify_failed: ['拷过去的内容和原来的对不上，没有删除任何东西', 'The copy did not match; nothing was deleted'],
  source_changed: ['导入过程中旧文件夹又有了新文件，没有删除它', 'The old folder changed while copying'],
  source_has_link: ['旧文件夹里有链接，不能一键导入', 'The old folder holds a link'],
  source_multiply_linked: ['旧文件夹里有文件在别处还有一个名字，不能一键导入', 'A file there has a second name on disk'],
  pin_rejected: ['这个设置写进去以后不生效，已经还原', 'That setting would not have taken effect; it was undone'],
  workplace_missing: ['指定的项目文件夹不在了', 'The folder this project was pointed at is gone'],
  // 回收区
  original_exists: ['原来的位置已经有一个同名文件，没有覆盖；先把那个文件挪开或改名，再恢复', 'A file of that name is already at the original place; nothing was replaced'],
  restore_failed: ['恢复没有完成，文件还在回收区里', 'The restore did not finish; the file is still in the recycle area'],
  clear_failed: ['回收区没有清空，有文件删不掉（可能正被别的程序打开）', 'Not everything could be deleted; a file may be open elsewhere'],
  error: ['出了点问题', 'Something went wrong'],
}

export const FOLDER_WARN_LABELS: Record<string, [string, string]> = {
  sync_folder: ['这个位置在同步盘里（OneDrive / 桌面 / 文档），同步会锁文件', 'Inside a synced folder; syncing locks files'],
  long_path: ['路径有点长，深一点的文件可能超出系统限制', 'A long path; deeper files may hit the limit'],
  different_volume: ['和现在的位置不在同一个磁盘，导入会是复制而不是改名', 'Another disk: an import copies rather than renames'],
  removable_drive: ['这是可移动磁盘，拔掉以后项目就打不开了', 'A removable drive: unplugging it hides the project'],
}

export function folderError(code: string, zh: boolean): string {
  const pair = FOLDER_ERROR_LABELS[code] ?? FOLDER_ERROR_LABELS.error
  return zh ? pair[0] : pair[1]
}

export function folderWarn(code: string, zh: boolean): string {
  const pair = FOLDER_WARN_LABELS[code]
  return pair ? (zh ? pair[0] : pair[1]) : code
}

// ── 回收区: what a model "deleted". Listed for the owner; put back or cleared for good from the console only. ──

export type RecycleEntry = {
  /** Echoed back to restore; a path the server composed, relative to the project folder. */
  id: string
  /** Where it came from, relative to the project folder. */
  original: string
  /** '' for the project folder itself, 'orders/<order>' for one order's folder. */
  area: string
  batch: string
  moved_at: string
  size: number
}

export type RecycleList = {
  entries: RecycleEntry[]
  count: number
  total_bytes: number
  truncated: boolean
  capped?: boolean
  /** What the owner saw; the clear sends it back, and a folder that changed since is refused. */
  token: string
}

export type RecycleState = {
  projectId: string
  list: RecycleList | null
  loading: boolean
  /** The id being put back, or 'clear' while the folder is being emptied. */
  busy: string
  /** The second question for 清空 is on screen. */
  confirming: boolean
  error: string
  note: '' | 'restored' | 'cleared'
  restored: string
  cleared: { removed: number; bytes: number } | null
}

export const EMPTY_RECYCLE_STATE: RecycleState = {
  projectId: '', list: null, loading: false, busy: '', confirming: false, error: '', note: '', restored: '', cleared: null,
}

export type RecycleAction =
  | { kind: 'project'; projectId: string }
  | { kind: 'load' }
  | { kind: 'askClear' }
  | { kind: 'cancelClear' }
  | { kind: 'restoring'; id: string }
  | { kind: 'clearing' }
  | { kind: 'ack'; ack: FolderAck }

export function recycleReducer(state: RecycleState, action: RecycleAction): RecycleState {
  if (action.kind === 'project') {
    return action.projectId === state.projectId ? state : { ...EMPTY_RECYCLE_STATE, projectId: action.projectId }
  }
  if (action.kind === 'load') return { ...state, loading: true, error: '' }
  if (action.kind === 'askClear') return { ...state, confirming: true, error: '', note: '' }
  if (action.kind === 'cancelClear') return { ...state, confirming: false }
  if (action.kind === 'restoring') return { ...state, busy: action.id, error: '', note: '' }
  if (action.kind === 'clearing') return { ...state, busy: 'clear', error: '', note: '' }

  const ack = action.ack || {}
  const name = String(ack.action ?? '')
  if (!name.startsWith('data_recycle_')) return state
  const project = String(ack.project_id ?? '')
  if (state.projectId && project && project !== state.projectId) return state
  if (ack.ok === false) {
    // The second question is withdrawn on a refusal: a stale token means the folder changed under the owner,
    // and the fresh listing has to be looked at before it is asked again.
    return { ...state, loading: false, busy: '', confirming: false, error: String(ack.error ?? 'error') }
  }
  const list = ack as unknown as RecycleList
  if (name === 'data_recycle_list') return { ...state, loading: false, error: '', list }
  if (name === 'data_recycle_restore') {
    const restored = ack.restored as { original?: unknown } | undefined
    return { ...state, loading: false, busy: '', error: '', list, note: 'restored', restored: String(restored?.original ?? '') }
  }
  if (name === 'data_recycle_clear') {
    const cleared = ack.cleared as { removed?: unknown; bytes?: unknown } | undefined
    return { ...state, loading: false, busy: '', confirming: false, error: '', list, note: 'cleared',
             cleared: { removed: Number(cleared?.removed ?? 0), bytes: Number(cleared?.bytes ?? 0) } }
  }
  return state
}

/** When a file was moved in, as a person reads it: the batch stamp without the seconds. */
export function recycleMovedAt(value: string): string {
  const text = String(value ?? '')
  return text ? text.replace('T', ' ').slice(0, 16) : ''
}

export function recycleAreaLabel(area: string, zh: boolean): string {
  if (!area) return zh ? '项目文件夹' : 'Project folder'
  const order = area.replace(/^orders\//, '')
  return zh ? `工单 ${order}` : `Order ${order}`
}
