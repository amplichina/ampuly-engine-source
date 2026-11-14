export const LAST_ACTIVE_PAGE_KEY = 'superones_last_active_page'

// The seven pages of the shell (frontend redesign S3-7, policy): the five gates of the rail and its two
// foot entries. 待审批 / 工作台 / 控制面 / 组织 are no longer pages: their content lives on 总控, in the inbox's 「现在」
// block, in 执行区's side window and under 配置. Their old addresses are still understood -- lib/shellRoute
// (retiredPageLanding) says where each one lands -- but none of them is restored as a page, so a remembered last page
// naming one falls back to the start page instead of an empty screen.
export const RESTORABLE_APP_PAGES = [
  'master',
  'workflows',
  'dashboard',
  'orders',
  'components',
  // The shell's two foot entries (frontend refactor U1).
  'config',
  'settings',
] as const

export type RestorableAppPage = typeof RESTORABLE_APP_PAGES[number]

type StorageReader = Pick<Storage, 'getItem'>
type StorageWriter = Pick<Storage, 'setItem'>

export function isRestorableAppPage(value: unknown): value is RestorableAppPage {
  return typeof value === 'string' && (RESTORABLE_APP_PAGES as readonly string[]).includes(value)
}

export function loadLastActivePage(storage: StorageReader | null | undefined, fallback: RestorableAppPage = 'master'): RestorableAppPage {
  try {
    const stored = storage?.getItem(LAST_ACTIVE_PAGE_KEY)
    return isRestorableAppPage(stored) ? stored : fallback
  } catch {
    return fallback
  }
}

export function saveLastActivePage(storage: StorageWriter | null | undefined, page: unknown): boolean {
  if (!isRestorableAppPage(page)) return false
  try {
    storage?.setItem(LAST_ACTIVE_PAGE_KEY, page)
    return storage != null
  } catch {
    return false
  }
}
