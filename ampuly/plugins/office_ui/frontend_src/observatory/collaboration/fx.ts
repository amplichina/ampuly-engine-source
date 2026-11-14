// Shared, renderer-agnostic helpers for the 关系 (2D) and 星图 (3D) particle views.
// Pure functions only (no DOM / canvas / three), so they are unit-tested in node.
// Original Ampuly code. The "deep-space agent constellation" look is inspired by
// useroneoneone/neural-vault (PolyForm-Noncommercial) — idea only, no code used.
import type { CollaborationLinkKind } from './model'
import type { RelationGraphData, RelationLink, RelationNode } from './relations'

/** Deep-space palette for the particle views (owner request: dark, glowing, vivid). */
export const FX_COLORS = {
  space: '#03040c',
  ai: '#46c8ff',
  human: '#ffc65a',
  owner: '#fff2c4',
  fold: '#7d89b0',
  label: '#e8f1ff',
  labelDim: '#8ea0c4',
  working: '#3dffa2',
  waiting: '#ffb547',
  blocked: '#ff4d6d',
  idle: '#5d6b8f',
  link: {
    trace: '#7cf7ff',
    'shared-task': '#b388ff',
    'shared-order': '#ff7ad9',
    team: '#5cffb0',
    reports: '#5b8cff',
  } satisfies Record<CollaborationLinkKind, string>,
} as const

export const LINK_KIND_LABEL: Record<CollaborationLinkKind, string> = {
  trace: '共用任务(轨迹)', 'shared-task': '共同任务', 'shared-order': '同一业务', team: '团队', reports: '汇报',
}

/** Hard cap on simultaneously animated particles (across all links) for frame budget. */
export const MAX_FX_PARTICLES = 220

export function nodeColor(node: Pick<RelationNode, 'kind' | 'isOwner'>): string {
  if (node.isOwner) return FX_COLORS.owner
  if (node.kind === 'human') return FX_COLORS.human
  if (node.kind === 'fold') return FX_COLORS.fold
  return FX_COLORS.ai
}

export function stateColor(state: RelationNode['state']): string {
  if (state === 'working') return FX_COLORS.working
  if (state === 'waiting') return FX_COLORS.waiting
  if (state === 'blocked') return FX_COLORS.blocked
  return FX_COLORS.idle
}

/** Vivid per-task ion colours (ION idea: every task gets its own colour). */
export const TASK_COLORS = ['#38e1ff', '#ff5ec4', '#ffd23f', '#7cff6b', '#a67bff', '#ff8a3d', '#3dffd0', '#ff4d6d'] as const

export function taskColor(taskId: string): string {
  return TASK_COLORS[Math.floor(hash01(taskId) * TASK_COLORS.length) % TASK_COLORS.length]
}

/** Colour of a relation's ions: its shared task if any, else its relation kind. */
export function ionColor(link: Pick<RelationLink, 'kind' | 'taskId'>): string {
  return link.taskId ? taskColor(link.taskId) : linkColor(link.kind)
}

/** Colour of an agent's halo/cloud: its current task when working, else AI/human tone. */
export function auraColor(node: Pick<RelationNode, 'kind' | 'isOwner' | 'taskId' | 'active'>): string {
  if (node.active && node.taskId && !node.isOwner) return taskColor(node.taskId)
  return nodeColor(node)
}

export function linkColor(kind: CollaborationLinkKind): string {
  return FX_COLORS.link[kind] ?? FX_COLORS.link.reports
}

/** '#rrggbb' -> 'rgba(r,g,b,a)'. */
export function hexA(hex: string, a: number): string {
  const v = hex.replace('#', '')
  const n = parseInt(v.length === 3 ? v.split('').map(c => c + c).join('') : v, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${Math.max(0, Math.min(1, a))})`
}

/**
 * Scale per-link particle counts so the total never exceeds `cap`.
 * Every active link keeps at least one particle; idle links keep zero.
 */
export function budgetParticles(links: readonly Pick<RelationLink, 'particles'>[], cap = MAX_FX_PARTICLES): number[] {
  const wanted = links.map(link => Math.max(0, link.particles))
  const total = wanted.reduce((sum, n) => sum + n, 0)
  if (total <= cap) return wanted
  const ratio = cap / total
  const scaled = wanted.map(n => (n > 0 ? Math.max(1, Math.floor(n * ratio)) : 0))
  let over = scaled.reduce((sum, n) => sum + n, 0) - cap
  for (let i = 0; over > 0 && i < scaled.length * 4; i += 1) {
    const j = i % scaled.length
    if (scaled[j] > 1) { scaled[j] -= 1; over -= 1 }
  }
  return scaled
}

/** Point on a quadratic curve between a and b bent by `curvature` (force-graph convention). */
export function curvePoint(
  ax: number, ay: number, bx: number, by: number, curvature: number, t: number,
): { x: number; y: number } {
  const cx = (ax + bx) / 2 + curvature * (by - ay)
  const cy = (ay + by) / 2 - curvature * (bx - ax)
  const u = 1 - t
  return { x: u * u * ax + 2 * u * t * cx + t * t * bx, y: u * u * ay + 2 * u * t * cy + t * t * by }
}

/** Deterministic 0..1 hash of a string (stable particle phases / star seeds). */
export function hash01(text: string): number {
  let h = 2166136261
  for (let i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619) }
  return ((h >>> 0) % 100000) / 100000
}

/** Seeded PRNG (mulberry32) so the starfield is identical between renders/screenshots. */
export function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export interface Star { x: number; y: number; r: number; base: number; speed: number; phase: number; hue: number }

/** Normalised (0..1) starfield; renderers scale to their viewport. */
export function makeStars(count: number, seed = 7): Star[] {
  const next = rng(seed)
  return Array.from({ length: count }, () => {
    const big = next() > 0.93
    return {
      x: next(), y: next(),
      r: big ? 0.9 + next() * 1.1 : 0.25 + next() * 0.7,
      base: 0.25 + next() * 0.6,
      speed: 0.4 + next() * 1.8,
      phase: next() * Math.PI * 2,
      hue: next() < 0.15 ? 40 : next() < 0.5 ? 210 : 230,
    }
  })
}

/** Ids of a node plus its direct neighbours (hover highlight). */
export function neighbourhood(data: Pick<RelationGraphData, 'links'>, id: string): Set<string> {
  const out = new Set<string>([id])
  for (const link of data.links) {
    if (link.source === id) out.add(link.target)
    if (link.target === id) out.add(link.source)
  }
  return out
}

/** Fresh pulses (by id) that were not seen before; mutates `seen`. */
export function freshPulses<T extends { id: string }>(pulses: readonly T[], seen: Set<string>): T[] {
  const out: T[] = []
  for (const pulse of pulses) {
    if (seen.has(pulse.id)) continue
    seen.add(pulse.id)
    out.push(pulse)
  }
  return out
}

/** Trace times may arrive in seconds or milliseconds; normalise to ms. */
export function pulseMs(time: number): number {
  return time > 0 && time < 1e11 ? time * 1000 : time
}
