// The product shell's navigation model (frontend refactor U1). Pure: the rail and its tests read the same lists, so
// an entry cannot exist in one place and be missing from another.
//
// Five gates in the design's order (FRONTEND-IA-2026-09-26 「五个门」: 总控 · 业务工单 · 项目中心 · 搭建台 · 组件库) and
// two at the foot. Since S3-7 (policy) there are no older pages beside them: 待审批, 工作台, 控制面 and
// 组织 are retired, and every page the console can restore is on the rail.

import type { RestorableAppPage } from './appPageState'

export const PRIMARY_NAV = ['master', 'orders', 'dashboard', 'workflows', 'components'] as const
export const FOOT_NAV = ['config', 'settings'] as const

export type PrimaryNavPage = typeof PRIMARY_NAV[number]
export type FootNavPage = typeof FOOT_NAV[number]
export type RailPage = PrimaryNavPage | FootNavPage

/** The i18n key of a page's name. `dashboard` is shown as the project centre, not by its old name. */
export const PAGE_LABEL_KEYS = {
  master: 'app.page.master',
  workflows: 'app.page.workflows',
  dashboard: 'app.page.projects',
  orders: 'app.page.orders',
  components: 'app.page.components',
  config: 'app.page.config',
  settings: 'app.page.preferences',
} as const satisfies Record<RestorableAppPage, string>

/** Which rail entry is lit for a page. Every page is a rail entry now; the null branch is kept for a page id that
 *  is not on the rail, so a caller never lights a neighbour. */
export function railSelection(page: RestorableAppPage): RailPage | null {
  return ([...PRIMARY_NAV, ...FOOT_NAV] as readonly string[]).includes(page) ? page as RailPage : null
}

/** What a count badge shows; nothing for zero, capped so it never widens the rail. */
export function badgeText(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return ''
  return count > 99 ? '99+' : String(Math.floor(count))
}
