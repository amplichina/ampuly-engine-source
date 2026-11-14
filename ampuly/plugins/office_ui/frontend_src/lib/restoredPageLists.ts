// What a page restored without a click still needs (frontend refactor U1).
//
// A reload, or an address that names a page, shows the page without the click that used to ask the server for its
// lists; a project switch drops every answer in flight. So the lists are asked for once the project has settled.
// "Once" is the point: the settle signal (a collab sync) arrives again on every run event, and one of the click road's
// requests (the organisation info) itself causes a sync -- re-running the click road on each signal is a request
// storm on most pages and an endless loop on one. This re-ask is therefore:
//   - once per project and page (a click on the entry counts as having asked);
//   - quiet: it never clears what the page already shows and never sets a loading state;
//   - inside the console: nothing that reaches an outside service, and nothing that answers with another sync.

import type { RestorableAppPage } from './appPageState'

/** The client methods a restored page asks for. Names are wsClient's; each takes the project id. */
export type QuietListRequest =
  | 'workflowList' | 'solutionBlueprintList' | 'runBudgetList' | 'orderMetrics' | 'orderList'
  | 'componentCatalogList' | 'skillImprovementList' | 'improvementProposalList' | 'skillTrialList'
  | 'credentialReferenceList' | 'deploymentList' | 'connectorSwitchList' | 'connectorCandidateList'
  | 'harnessPendingList' | 'processSpecList' | 'settingsGet' | 'accountList'

const ORDERS: QuietListRequest[] = ['workflowList', 'solutionBlueprintList', 'runBudgetList', 'orderMetrics', 'orderList']

export function quietListsFor(page: RestorableAppPage, owner: boolean): QuietListRequest[] {
  // 搭建台's step editor says which model an AI step runs on (the configured default): the owner's settings say it.
  if (page === 'workflows') return owner ? [...ORDERS, 'settingsGet'] : ORDERS
  if (page === 'orders') return ORDERS
  if (page === 'dashboard') return ['workflowList', 'orderList']
  // 总控's landing offers what is unfinished or latest: it needs the flows and the orders to say so.
  // ...and, on the owner's console, the settings, so the composer can say which model answers.
  if (page === 'master') return owner ? ['workflowList', 'orderList', 'settingsGet'] : ['workflowList', 'orderList']
  if (page === 'components') {
    return owner
      ? ['componentCatalogList', 'workflowList', 'skillImprovementList', 'improvementProposalList', 'skillTrialList']
      : ['componentCatalogList', 'workflowList']
  }
  // 配置 shows what the server reports about models and channels, and since S3-7 holds 控制面's panels too
  // (服务与连接): the owner's console reads their lists here, once per project and page, quietly.
  if (page === 'config') {
    return owner
      ? ['settingsGet', 'credentialReferenceList', 'deploymentList', 'connectorSwitchList', 'connectorCandidateList', 'harnessPendingList', 'processSpecList']
      : ['settingsGet']
  }
  // 设置 → 通知 reads the paging channels, and the owner console's 账号 / 模型与密钥 read the account list and the
  // same settings. Both are quiet reads: neither reaches an outside service nor answers with another sync.
  if (page === 'settings') return owner ? ['settingsGet', 'accountList'] : []
  return []
}

/** Never part of the quiet re-ask, whatever page: `orgInfo` answers with a sync (the loop), the market calls reach an
 *  outside service. The test holds the list above against this one. */
export const NEVER_QUIET = ['orgInfo', 'skillMarketBrowse', 'skillMarketCategories', 'collabSync'] as const

export function askedKey(projectId: string, page: RestorableAppPage): string {
  return `${projectId}\n${page}`
}

/** Whether this settle signal is the first for this project and page, and the key to remember either way. */
export function restoredPageAsk(lastKey: string, projectId: string, page: RestorableAppPage): { ask: boolean; key: string } {
  const key = askedKey(projectId, page)
  return { ask: Boolean(projectId) && key !== lastKey, key: projectId ? key : lastKey }
}
