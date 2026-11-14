// Theme registry + pure data mapping for the 关系 view's visual themes.
// Every number shown by a theme comes from here (i.e. from buildCollaborationView via
// buildRelationGraph); motion itself is decorative and labelled as such in the UI.
// Original Ampuly code. Theme ideas (视界熔炉 / 天工开物 / 数据飞轮) credited to
// cyberdaddyleo/agent-display (PolyForm-Noncommercial) — ideas only, no code used.
import type { RelationGraphData, RelationNode, RelationPulse } from './relations'

/** User-selectable themes (switcher + T cycle). */
export type RelationTheme = 'forge' | 'works' | 'ops'

/**
 * What can actually render. 'basic' is the plain force graph (RelationsGraph): never offered
 * in the switcher, only used automatically when the chosen theme cannot run (no WebGL,
 * reduced motion, or the theme crashed / failed to load).
 */
export type RenderedRelationView = RelationTheme | 'basic'
export type RelationFallback = 'webgl' | 'motion' | 'error'

export interface ThemeInfo {
  id: RelationTheme
  label: string
  en: string
  webgl: boolean
}

export const THEMES: readonly ThemeInfo[] = [
  { id: 'forge', label: '视界熔炉', en: 'HORIZON FORGE', webgl: true },
  { id: 'works', label: '天工开物', en: 'HEAVEN WORKS', webgl: false },
  { id: 'ops', label: '数据飞轮', en: 'DATA FLYWHEEL', webgl: false },
]

/** Internal fallback view label (shown only in the fallback chip / loading text). */
export const BASIC_VIEW_LABEL = '基础关系图'

export const THEME_STORAGE_KEY = 'ampuly.collab.relationTheme'
export const DEFAULT_THEME: RelationTheme = 'forge'
/** Stored values from earlier builds that are no longer selectable; migrated to DEFAULT_THEME. */
export const RETIRED_THEME_VALUES: readonly string[] = ['simple']

export function isTheme(value: unknown): value is RelationTheme {
  return typeof value === 'string' && THEMES.some(theme => theme.id === value)
}

export function nextTheme(current: RelationTheme): RelationTheme {
  const index = THEMES.findIndex(theme => theme.id === current)
  return THEMES[(index + 1) % THEMES.length].id
}

/**
 * What actually renders: reduced motion -> static basic graph; WebGL themes without WebGL ->
 * basic graph; a theme that crashed or failed to load (failed === preferred) -> basic graph.
 */
export function resolveTheme(
  preferred: RelationTheme, env: { webgl: boolean; reducedMotion: boolean; failed?: RelationTheme },
): { theme: RenderedRelationView; fallback?: RelationFallback } {
  if (env.reducedMotion) return { theme: 'basic', fallback: 'motion' }
  const info = THEMES.find(theme => theme.id === preferred)
  if (info?.webgl && !env.webgl) return { theme: 'basic', fallback: 'webgl' }
  if (env.failed === preferred) return { theme: 'basic', fallback: 'error' }
  return { theme: preferred }
}

export interface ThemeTask {
  id: string
  title: string
  memberIds: string[]
}

export interface ThemeModel {
  owner?: RelationNode
  /** Agents/humans (no owner, no fold), active first then by depth-ish radius, capped. */
  members: RelationNode[]
  hiddenMembers: number
  folds: RelationNode[]
  /** Active tasks: observed currentTaskId shared by at least one working member. */
  tasks: ThemeTask[]
  idleMembers: number
  activeMembers: number
  blocked: number
  waiting: number
  /** Recent trace events (real), newest last. */
  events: RelationPulse[]
}

export const THEME_MEMBER_CAP = 64

export function buildThemeModel(
  data: RelationGraphData,
  taskTitles: Readonly<Record<string, string>>,
  pulses: readonly RelationPulse[] = [],
  cap = THEME_MEMBER_CAP,
): ThemeModel {
  const owner = data.nodes.find(node => node.isOwner)
  const all = data.nodes.filter(node => !node.isOwner && node.kind !== 'fold')
  const sorted = [...all].sort((a, b) => Number(b.active) - Number(a.active) || b.radius - a.radius || a.label.localeCompare(b.label))
  const members = sorted.slice(0, cap)
  const byTask = new Map<string, string[]>()
  for (const node of all) {
    if (!node.active || !node.taskId) continue
    const list = byTask.get(node.taskId) ?? []
    list.push(node.id)
    byTask.set(node.taskId, list)
  }
  const tasks = [...byTask.entries()]
    .map(([id, memberIds]) => ({ id, title: taskTitles[id] || id, memberIds }))
    .sort((a, b) => b.memberIds.length - a.memberIds.length || a.title.localeCompare(b.title))
  return {
    ...(owner ? { owner } : {}),
    members,
    hiddenMembers: all.length - members.length,
    folds: data.nodes.filter(node => node.kind === 'fold'),
    tasks,
    activeMembers: all.filter(node => node.active).length,
    idleMembers: all.filter(node => !node.active).length,
    blocked: all.filter(node => node.state === 'blocked').length,
    waiting: all.filter(node => node.state === 'waiting').length,
    events: pulses.slice(-24),
  }
}

type MiniStorage = Pick<Storage, 'getItem' | 'setItem'>

function safeStorage(): MiniStorage | undefined {
  try { return typeof localStorage !== 'undefined' ? localStorage : undefined } catch { return undefined }
}

export function readStoredTheme(storage: MiniStorage | undefined = safeStorage()): RelationTheme {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY)
    if (isTheme(value)) return value
    // Retired choice (e.g. the old 简洁 'simple'): rewrite to the default so it sticks.
    if (typeof value === 'string' && RETIRED_THEME_VALUES.includes(value)) storeTheme(DEFAULT_THEME, storage)
    return DEFAULT_THEME
  } catch { return DEFAULT_THEME }
}

export function storeTheme(theme: RelationTheme, storage: MiniStorage | undefined = safeStorage()): void {
  try { storage?.setItem(THEME_STORAGE_KEY, theme) } catch { /* private mode / quota: keep in-memory only */ }
}

/** Keyboard cycling: plain T (no modifiers) outside editable fields. */
export function isThemeCycleKey(event: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'altKey'>, targetTag?: string, editable?: boolean): boolean {
  if (event.ctrlKey || event.metaKey || event.altKey) return false
  if (editable || (targetTag && /^(INPUT|TEXTAREA|SELECT)$/i.test(targetTag))) return false
  return event.key === 't' || event.key === 'T'
}
