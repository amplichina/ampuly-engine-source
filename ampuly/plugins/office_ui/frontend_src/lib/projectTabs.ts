// 项目中心's two levels (frontend refactor U3, structure; redrawn S3-2B, policy). Pure.
//
// The owner's prototype has 项目中心 in two levels: every project in one list, and -- once inside a project -- four
// tabs: 业务总览 / 业务流 / 协作与接入 / 资料与数据. This module only says which tab is which and where each of the
// page's blocks belongs, so the page can show one tab at a time without its blocks being moved around in the source
// (several tests read that order).

export const PROJECT_TABS = ['overview', 'flows', 'collab', 'data'] as const
export type ProjectTab = typeof PROJECT_TABS[number]
export type ProjectLevel = 'project' | 'all'

export const PROJECT_TAB_LABELS: Record<ProjectTab, readonly [string, string]> = {
  overview: ['业务总览', 'Overview'],
  flows: ['业务流', 'Flows'],
  collab: ['协作与接入', 'People & connections'],
  data: ['资料与数据', 'Files & data'],
}

/** 协作与接入's own three boards. */
export const COLLAB_TABS = ['steps', 'systems', 'people'] as const
export type CollabTab = typeof COLLAB_TABS[number]
export const COLLAB_TAB_LABELS: Record<CollabTab, readonly [string, string]> = {
  steps: ['步骤责任', 'Who does which step'],
  systems: ['软件与连接', 'Software & connections'],
  people: ['人员与职责', 'People & roles'],
}

/** The page's blocks, and the tab each belongs to. */
export const BLOCK_TABS = {
  // 业务总览: the boundary line, 现在需要关注, and the flow lines (业务全景).
  boundary: 'overview',
  attention: 'overview',
  panorama: 'overview',
  // 业务流: today's flow cards (采用 / 升级 / 暂停 / 恢复 / 撤下 / 派生 / 到搭建台).
  composition: 'flows',
  // 协作与接入: 步骤责任 / 软件与连接 / 人员与职责 (the roster and the contact book).
  collab: 'collab',
  staff: 'collab',
  org: 'collab',
  // 资料与数据: the files and the customer memory, the storage facts and administration, the recorded amounts, and
  // the folded 系统记录 (值班巡检 / 运行日志 / 运行会话).
  artifacts: 'data',
  storage: 'data',
  money: 'data',
  records: 'data',
} as const satisfies Record<string, ProjectTab>
export type ProjectBlock = keyof typeof BLOCK_TABS

/** Whether a block is off screen: everything inside a project hides on the all-projects level, and inside a project
 *  only the open tab's blocks show. Returned as the value for the `hidden` attribute (undefined = shown). */
export function blockHidden(block: ProjectBlock, level: ProjectLevel, tab: ProjectTab): true | undefined {
  return level === 'project' && BLOCK_TABS[block] === tab ? undefined : true
}

/** The list of every project shows on its own level only. */
export function projectListHidden(level: ProjectLevel): true | undefined {
  return level === 'all' ? undefined : true
}

/** A tab named from outside (a link, a button elsewhere on the page); anything unknown is the overview. The old
 *  names still land somewhere sensible: 资料 and 数据与存储 are 资料与数据, 人员与职责 is 协作与接入. */
export function tabFrom(value: string | null | undefined): ProjectTab {
  if ((PROJECT_TABS as readonly string[]).includes(value ?? '')) return value as ProjectTab
  if (value === 'files') return 'data'
  if (value === 'people') return 'collab'
  return 'overview'
}
