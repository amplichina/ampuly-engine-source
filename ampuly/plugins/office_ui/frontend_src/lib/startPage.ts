// 设置 → 通用 → 启动默认页面 (the prototype has it as a display-only preference; here it is real). A per-browser
// choice, like the theme: which page the console opens on when the address names none. The default is what the
// console has always done -- the page this browser was last on.

import { isRestorableAppPage, type RestorableAppPage } from './appPageState'

export const START_PAGE_KEY = 'superones.startPage.v1'
/** `last` = where this browser left off; the rest are the four pages work starts from. */
export const START_PAGES = ['last', 'master', 'workflows', 'dashboard', 'orders'] as const
export type StartPage = typeof START_PAGES[number]

type Store = Pick<Storage, 'getItem' | 'setItem'>

export function loadStartPage(storage: Pick<Storage, 'getItem'> | null | undefined): StartPage {
  try {
    const stored = storage?.getItem(START_PAGE_KEY)
    return (START_PAGES as readonly string[]).includes(stored ?? '') ? stored as StartPage : 'last'
  } catch {
    return 'last'
  }
}

export function saveStartPage(storage: Store | null | undefined, value: StartPage): boolean {
  if (!(START_PAGES as readonly string[]).includes(value)) return false
  try {
    storage?.setItem(START_PAGE_KEY, value)
    return storage != null
  } catch {
    return false
  }
}

/** The page to open on when the address names none. An address that names a page always wins (the caller checks). */
export function resolveStartPage(preference: StartPage, lastPage: RestorableAppPage): RestorableAppPage {
  return preference !== 'last' && isRestorableAppPage(preference) ? preference : lastPage
}
