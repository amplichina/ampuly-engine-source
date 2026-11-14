// The browser universal connector on a step (4.7 Q05 B4, policy). A step carries it when no API or
// MCP component reaches the system it works in: the built-in browser works on the sites it names, towards the goal it
// states, and the step is done only when the page shows its conditions (checked by code, never by the model's word).
// The builder edits one field, `connector` -- its parameters, or null -- and the save keeps it as the step's component
// instance (ampuly/plugins/office_ui/browser_connector.py checks every piece; its findings point at this form).

import { isDesktopConnector, type DesktopConnectorParams } from './desktopConnector'

export type PostconditionKind = 'url_contains' | 'text_contains' | 'element' | 'file_exists'
export type DeciderMode = 'act' | 'suggest'
/** Bulk P1 (owner ruling ): how done is proven. operation (default): something the step did made a condition
 * true. lookup: the step only reads a site; it is done when the page shows the order's own key and the answer in one
 * block -- opening the result's address is enough. Only the owner sets it (master control is refused). */
export type VerifyMode = 'operation' | 'lookup' | 'batch'
export const VERIFY_MODES: readonly VerifyMode[] = ['operation', 'lookup', 'batch']

/** D1 (policy): what a batch step does with each row. The order's distribution authorization is
 * held to it at launch and on every read of the list; a batch step that has not chosen one is refused at launch. */
export type DistributionAction = 'upload' | 'publish' | 'look_back'
export const DISTRIBUTION_ACTION_OPTIONS: ReadonlyArray<readonly [code: DistributionAction, zh: string, en: string]> = [
  ['upload', '上传', 'Upload'],
  ['publish', '发布', 'Publish'],
  ['look_back', '回查', 'Look back'],
]

export interface VerifyParams {
  mode: VerifyMode
  /** batch (P4): where the list comes from, and which field of a row is that row's identity. */
  items?: { from: string }
  item_key?: string
  key?: { from: string }
  no_result?: string[]
  extract?: Record<string, string>
}

export interface ConnectorParams {
  allowed_domains: string[]
  /** P2 护栏一: narrow a site to part of it. Absent means the whole site, which is what every step said before
   *  this field existed. A path written into `allowed_domains` is dropped there, on purpose. */
  allowed_paths?: string[]
  /** P2 护栏一: the buttons this step may click. Absent means the click rules are unchanged; a list means only
   *  those, and an EMPTY list means none at all -- which is why an empty box must send nothing, not `[]`. */
  controls?: string[]
  goal: string
  inputs?: Record<string, { from: string }>
  postconditions: Array<{ kind: PostconditionKind; value: string }>
  output: { path: string; fields?: string[] }
  handoff?: { captcha?: 'human'; upload?: 'human'; login?: 'human'; max_steps?: number }
  login?: { mode: 'handoff' }
  decider?: { jev?: boolean; mode?: DeciderMode }
  verify?: VerifyParams
  /** D1: a batch step's 「这一步做什么」. Absent = not chosen yet (refused at launch, not at save). */
  distribution_action?: DistributionAction
  // What the form does not show is carried back as it came.
  [key: string]: unknown
}

export const POSTCONDITION_KINDS: readonly PostconditionKind[] = ['url_contains', 'text_contains', 'element', 'file_exists']

export const POSTCONDITION_LABELS: Record<PostconditionKind, readonly [string, string]> = {
  url_contains: ['网址包含', 'URL contains'],
  text_contains: ['页面出现文字', 'page shows text'],
  element: ['页面出现元素（选择器）', 'page shows element (selector)'],
  file_exists: ['下载到了文件', 'a file was downloaded'],
}

/** What each condition's value is (Q05 B5): a downloaded file is named by its type, never by a page's file name. */
export const POSTCONDITION_HINTS: Record<PostconditionKind, readonly [string, string]> = {
  url_contains: ['如 /orders/', 'e.g. /orders/'],
  text_contains: ['如 物流单号；查询步骤可写 {key}（本单钥匙）', 'e.g. Tracking number; a lookup may write {key}'],
  element: ['如 #tracking-no', 'e.g. #tracking-no'],
  file_exists: ['文件类型，如 pdf、xlsx；* 为任意', 'file type, e.g. pdf, xlsx; * for any'],
}

export const MAX_STEPS_DEFAULT = 20
export const MAX_STEPS_LIMIT = 40

/** A new connector for a step: nothing guessed -- no site, no goal -- the save names what is still missing. */
export function newConnector(deliverables: readonly string[] = []): ConnectorParams {
  return {
    allowed_domains: [],
    goal: '',
    postconditions: [{ kind: 'text_contains', value: '' }],
    output: { path: deliverables[0] ?? '' },
    handoff: { captcha: 'human', upload: 'human', login: 'human', max_steps: MAX_STEPS_DEFAULT },
    login: { mode: 'handoff' },
    // policy: a step with this connector may ask JEV by default.
    decider: { jev: true, mode: 'act' },
  }
}

/** Sites as a person writes them: one per line or separated by commas; the save cleans each one. */
export function domainsFromText(text: string): string[] {
  return text.split(/[\s,，、;；]+/).map(item => item.trim()).filter(Boolean)
}

export function domainsText(domains: readonly string[] | undefined): string {
  return (domains ?? []).join('\n')
}

/** A declaration a person types one per line (P2 护栏一): an address, or a button's name as the page reads it.
 *
 * Built on :func:`linesFromText` -- one per line, because a button is called 「立即投稿」 or "Go live" and
 * splitting on spaces or commas would tear those in half. The one thing it adds is the important one: an empty
 * box gives `undefined`, never `[]`. Leaving the field alone means 「没声明」 and changes nothing; an empty list
 * would mean 「一个都不准点」 and stop the step dead.
 */
export function declaredFromText(text: string): string[] | undefined {
  const rows = linesFromText(text)
  return rows.length ? rows : undefined
}

export function declaredText(rows: readonly string[] | undefined): string {
  return (rows ?? []).join('\n')
}

/** The order's inputs this step reads, by name: always a reference ({from: order.inputs.<name>}), never a value. */
export function inputsFromText(text: string): Record<string, { from: string }> {
  const out: Record<string, { from: string }> = {}
  for (const name of text.split(/[\s,，、;；]+/).map(item => item.trim()).filter(Boolean)) out[name] = { from: `order.inputs.${name}` }
  return out
}

export function inputsText(inputs: ConnectorParams['inputs']): string {
  return Object.keys(inputs ?? {}).join(', ')
}

export function verifyMode(connector: ConnectorParams | null | undefined): VerifyMode {
  const mode = connector?.verify?.mode
  return mode === 'lookup' || mode === 'batch' ? mode : 'operation'
}

/** Which field of a row is that row's identity, for a batch step ('' when not set). */
export function itemKeyName(connector: ConnectorParams | null | undefined): string {
  return String(connector?.verify?.item_key ?? '')
}

/** The batch step's list, as the name of one of the step's order inputs ('' when none is set). */
export function itemsInputName(connector: ConnectorParams | null | undefined): string {
  const from = connector?.verify?.items?.from ?? ''
  return from.startsWith('order.inputs.') ? from.slice('order.inputs.'.length) : ''
}

/** The lookup's key as the name of one of the step's order inputs ('' when none is set). */
export function lookupKeyName(connector: ConnectorParams | null | undefined): string {
  const from = connector?.verify?.key?.from ?? ''
  return from.startsWith('order.inputs.') ? from.slice('order.inputs.'.length) : ''
}

/** Switch how done is proven.
 *
 *  Each mode keeps only its own settings: the two are not variations of one another, and carrying a lookup's key
 *  into a batch step (or the other way round) is refused by the save, which then complains about a field the
 *  person never typed. `operation` drops both (a {key} or {item.…} left in a condition is refused, and says so).
 */
export function withVerifyMode(connector: ConnectorParams, mode: VerifyMode): ConnectorParams {
  // 「这一步做什么」 belongs to a batch step only (D1): leaving batch takes it off, as the other modes' settings are.
  if (mode !== 'batch' && 'distribution_action' in connector) {
    const { distribution_action: _dropped, ...rest } = connector
    return withVerifyMode(rest as ConnectorParams, mode)
  }
  if (mode === 'operation') return { ...connector, verify: { mode: 'operation' } }
  const names = Object.keys(connector.inputs ?? {})
  if (mode === 'batch') {
    const current = itemsInputName(connector)
    const from = names.includes(current) ? current : (names[0] ?? '')
    return { ...connector, verify: { mode: 'batch', item_key: itemKeyName(connector),
                                     ...(from ? { items: { from: `order.inputs.${from}` } } : {}) } }
  }
  const current = lookupKeyName(connector)
  const key = names.includes(current) ? current : (names[0] ?? '')
  return { ...connector, verify: { mode: 'lookup', ...(key ? { key: { from: `order.inputs.${key}` } } : {}),
                                   ...(connector.verify?.no_result ? { no_result: connector.verify.no_result } : {}),
                                   ...(connector.verify?.extract ? { extract: connector.verify.extract } : {}) } }
}

export function withItemsInput(connector: ConnectorParams, name: string): ConnectorParams {
  const verify: VerifyParams = { ...(connector.verify ?? { mode: 'batch' }), mode: 'batch' }
  if (name) verify.items = { from: `order.inputs.${name}` }
  else delete verify.items
  return { ...connector, verify }
}

export function withItemKey(connector: ConnectorParams, field: string): ConnectorParams {
  return { ...connector, verify: { ...(connector.verify ?? { mode: 'batch' }), mode: 'batch', item_key: field } }
}

/** A batch step's declared action ('' when none, or when the stored value is not one of the three). */
export function distributionAction(connector: ConnectorParams | null | undefined): DistributionAction | '' {
  const value = connector?.distribution_action
  return DISTRIBUTION_ACTION_OPTIONS.some(([code]) => code === value) ? value as DistributionAction : ''
}

/** Choose 「这一步做什么」; '' takes the choice off (the field is left out, never saved as an empty word). */
export function withDistributionAction(connector: ConnectorParams, value: string): ConnectorParams {
  const { distribution_action: _previous, ...rest } = connector
  const picked = DISTRIBUTION_ACTION_OPTIONS.find(([code]) => code === value)?.[0]
  return picked ? { ...rest, distribution_action: picked } as ConnectorParams : rest as ConnectorParams
}

export function withLookupKey(connector: ConnectorParams, name: string): ConnectorParams {
  const verify: VerifyParams = { ...(connector.verify ?? { mode: 'lookup' }), mode: 'lookup' }
  if (name) verify.key = { from: `order.inputs.${name}` }
  else delete verify.key
  return { ...connector, verify }
}

/** Names written one after another (commas, pauses or new lines between them). */
export function namesFromText(text: string): string[] {
  return text.split(/[\n,，、;；]+/).map(item => item.trim()).filter(Boolean)
}

/** Phrases one per line (a phrase may hold a comma: 未找到，请重试). */
export function linesFromText(text: string): string[] {
  return text.split(/\n+/).map(item => item.trim()).filter(Boolean)
}

/** What the step's card says of its connector: the sites (the first, and how many more); '' when it has none.
 *  A desktop connector (4.7-A06) has its own row (workspace/desktopConnector.ts desktopRow), so '' here too. */
export function connectorRow(connector: ConnectorParams | DesktopConnectorParams | null | undefined, zh: boolean): string {
  if (!connector || isDesktopConnector(connector)) return ''
  const sites = connector.allowed_domains ?? []
  const where = sites.length === 0 ? (zh ? '未指定网站' : 'no site yet') : sites.length === 1 ? sites[0] : `${sites[0]} ${zh ? `等 ${sites.length} 个网站` : `+${sites.length - 1}`}`
  const who = connector.decider?.jev === false ? '' : connector.decider?.mode === 'suggest' ? (zh ? ' · JEV 建议' : ' · JEV suggests') : ' · JEV'
  const mode = verifyMode(connector)
  const how = mode === 'lookup' ? (zh ? ' · 查询' : ' · lookup')
    : mode === 'batch' ? (zh ? ' · 批量' : ' · batch') : ''
  return `${where}${who}${how}`
}

export function clampSteps(value: number): number {
  if (!Number.isFinite(value)) return MAX_STEPS_DEFAULT
  return Math.max(1, Math.min(MAX_STEPS_LIMIT, Math.round(value)))
}
