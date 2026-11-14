import type { SocketStatus, VisualEvent } from '../types/visual'
import { buildTraceRun, compareTraceEvents, isTerminalTraceEvent, isToolFailureTraceEvent, projectVisualEvent, sanitizePublicSummary, sanitizeTraceIdentifier, traceRunKey, type TraceEvent, type TraceMetrics, type TracePhase, type TraceSnapshot } from './traceModel'

interface PhaseGuard { terminal?: TracePhase; review: number; failed: boolean; permissionAt: number; startedAt: number; lastAt: number }
const PHASES = new Set<TracePhase>(['idle', 'thinking', 'searching', 'executing', 'collaborating', 'review', 'verifying', 'completed', 'failed', 'cancelled', 'unknown'])
const SOFT_PROGRESS = new Set(['thinking_delta', 'assistant_delta', 'tool_progress', 'status_snapshot', 'subagent_updated', 'cost_update', 'context_usage', 'context_warning'])
const SOFT_NOTIFY_MS = 100

function safeProjection(event: TraceEvent, projectId: string): TraceEvent | null {
  if (event.projectId !== projectId || !Number.isFinite(event.time) || event.time < 0 || !event.id || !event.taskId || !event.runId) return null
  const id = sanitizeTraceIdentifier(event.id)
  const taskId = sanitizeTraceIdentifier(event.taskId)
  const runId = sanitizeTraceIdentifier(event.runId)
  const type = sanitizeTraceIdentifier(event.type)
  if (!id || !taskId || !runId || !type) return null
  const sourceId = sanitizeTraceIdentifier(event.sourceId)
  const metrics: TraceMetrics = {}
  for (const key of ['durationMs', 'inputTokens', 'outputTokens', 'totalTokens', 'costUsd'] as const) {
    const value = event.metrics?.[key]
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) metrics[key] = value
  }
  const references = (Array.isArray(event.references) ? event.references : []).slice(0, 8).flatMap(reference => {
    if (!reference || !['artifact', 'source', 'task'].includes(reference.kind)) return []
    const label = sanitizePublicSummary(reference.label)
    const validId = reference.kind === 'task' ? sanitizeTraceIdentifier(reference.id) : /^[\p{L}\p{N}_.-]{1,128}$/u.test(reference.id)
    if (!label || !validId) return []
    return [Object.freeze({ kind: reference.kind, id: reference.id, label })]
  })
  return Object.freeze({
    id, ...(sourceId ? { sourceId } : {}),
    time: event.time, projectId, taskId, runId,
    actorId: sanitizeTraceIdentifier(event.actorId) ?? 'runtime', type, phase: PHASES.has(event.phase) ? event.phase : 'unknown',
    title: sanitizePublicSummary(event.title) ?? '执行记录',
    summary: sanitizePublicSummary(event.summary) ?? '已记录一个执行事件。',
    references: Object.freeze(references), ...(Object.keys(metrics).length ? { metrics: Object.freeze(metrics) } : {}),
  })
}

/** Bounded in-memory display history. This is not the engine's audit journal. */
export class ExecutionTraceStore {
  private readonly capacity: number
  private readonly listeners = new Set<() => void>()
  private seen = new Set<string>()
  private seenOrder: string[] = []
  private phaseGuards = new Map<string, PhaseGuard>()
  private progressSignatures = new Map<string, Map<string, string>>()
  private pendingNotification: ReturnType<typeof setTimeout> | undefined
  private lastNotificationAt = -Infinity
  private snapshot: TraceSnapshot = Object.freeze({ projectId: '', connection: 'disconnected', runs: Object.freeze([]), events: Object.freeze([]), droppedEvents: 0, revision: 0 })

  constructor(capacity = 600) {
    this.capacity = Number.isFinite(capacity) ? Math.max(1, Math.min(10_000, Math.floor(capacity))) : 600
  }

  getSnapshot = (): TraceSnapshot => this.snapshot
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener); if (!this.listeners.size) this.clearNotification() }
  }

  setProject(projectId: string): void {
    const next = projectId.trim()
    if (next === this.snapshot.projectId) return
    this.clearNotification()
    this.seen = new Set(); this.seenOrder = []; this.phaseGuards.clear(); this.progressSignatures.clear()
    this.publish({ ...this.snapshot, projectId: next, events: Object.freeze([]), runs: Object.freeze([]), droppedEvents: 0 })
  }

  setConnection(connection: SocketStatus): void {
    if (connection === this.snapshot.connection) return
    this.publish({ ...this.snapshot, connection })
  }

  replace(events: readonly VisualEvent[], projectId: string): void {
    // A delayed snapshot from a previously selected project must not switch the
    // current project or erase its events. The host calls setProject explicitly.
    if (!projectId || this.snapshot.projectId !== projectId) return
    this.seen = new Set(); this.seenOrder = []; this.phaseGuards.clear(); this.progressSignatures.clear()
    this.projectAndPublish(events, projectId, true)
  }

  ingest(event: VisualEvent, projectId: string): void {
    if (!projectId || this.snapshot.projectId !== projectId) return
    const projected = projectVisualEvent(event, projectId)
    if (!projected) return
    this.appendProjected([projected], projectId)
  }

  /** Only accepts this module's safe projection shape, e.g. a vetted history adapter. */
  appendProjected(events: readonly TraceEvent[], projectId: string): void {
    if (!projectId || this.snapshot.projectId !== projectId) return
    const accepted: TraceEvent[] = []
    for (const incoming of events) {
      const event = safeProjection(incoming, projectId)
      if (!event) continue
      const key = this.dedupeKey(event)
      if (this.seen.has(key)) continue
      if (!this.hasNewDisplayFact(event)) continue
      this.remember(key)
      accepted.push(event)
    }
    if (!accepted.length) return
    for (const event of accepted.sort(compareTraceEvents)) this.rememberPhase(event)
    this.commitEvents([...this.snapshot.events, ...accepted], projectId, this.snapshot.droppedEvents, accepted.some(event => !SOFT_PROGRESS.has(event.type) || isTerminalTraceEvent(event)))
  }

  private projectAndPublish(events: readonly VisualEvent[], projectId: string, replace: boolean): void {
    const projected: TraceEvent[] = []
    const candidates = events.map(event => projectVisualEvent(event, projectId)).filter((event): event is TraceEvent => event !== null).sort(compareTraceEvents)
    for (const safe of candidates) {
      const key = this.dedupeKey(safe)
      if (this.seen.has(key)) continue
      if (!this.hasNewDisplayFact(safe)) continue
      this.remember(key); projected.push(safe)
    }
    for (const event of [...projected].sort(compareTraceEvents)) this.rememberPhase(event)
    this.commitEvents(projected, projectId, replace ? 0 : this.snapshot.droppedEvents)
  }

  private dedupeKey(event: TraceEvent): string {
    // An AMPULY event may have several delivery IDs after replay. The runtime
    // projection is one semantic event; supplementary adapter events are ignored.
    return JSON.stringify([event.projectId, event.sourceId || event.id, event.type])
  }

  private hasNewDisplayFact(event: TraceEvent): boolean {
    const runKey = traceRunKey(event)
    const key = JSON.stringify([runKey, event.actorId])
    if (!SOFT_PROGRESS.has(event.type) || isTerminalTraceEvent(event)) {
      // A tool/review/terminal boundary starts a new display segment. Subsequent
      // thinking and output signals may each add one new stage, not one row/token.
      this.progressSignatures.delete(key)
      return true
    }
    const signature = JSON.stringify([event.phase, event.title, event.summary, event.references, event.metrics])
    const previous = this.progressSignatures.get(key) ?? new Map<string, string>()
    if (previous.get(event.type) === signature) return false
    previous.set(event.type, signature)
    this.progressSignatures.delete(key)
    this.progressSignatures.set(key, previous)
    while (this.progressSignatures.size > this.capacity * 4) {
      const oldest = this.progressSignatures.keys().next().value
      if (oldest === undefined) break
      this.progressSignatures.delete(oldest)
    }
    return true
  }

  private rememberPhase(event: TraceEvent): void {
    const key = traceRunKey(event)
    const guard = this.phaseGuards.get(key) ?? { review: 0, failed: false, permissionAt: -1, startedAt: event.time, lastAt: event.time }
    guard.startedAt = Math.min(guard.startedAt, event.time)
    guard.lastAt = Math.max(guard.lastAt, event.time)
    if (isTerminalTraceEvent(event) && (!guard.terminal || event.phase === 'failed' || event.phase === 'cancelled')) guard.terminal = event.phase
    if (isToolFailureTraceEvent(event)) guard.failed = true
    if (event.time >= guard.permissionAt) {
      if (event.type === 'permission_requested') { guard.review += 1; guard.permissionAt = event.time }
      if (event.type === 'permission_resolved') { guard.review = Math.max(event.phase === 'review' ? 1 : 0, guard.review - 1); guard.permissionAt = event.time }
      if (event.type === 'status_snapshot' && event.phase === 'review') { guard.review = Math.max(1, guard.review); guard.permissionAt = event.time }
    }
    this.phaseGuards.set(key, guard)
    if (this.phaseGuards.size > this.capacity * 4) {
      const oldest = [...this.phaseGuards].sort((left, right) => left[1].lastAt - right[1].lastAt)[0]
      if (oldest) this.phaseGuards.delete(oldest[0])
    }
  }

  private remember(key: string): void {
    this.seen.add(key); this.seenOrder.push(key)
    // Keep a bounded replay window beyond the visible ring, without unbounded
    // memory growth on an always-open dashboard.
    while (this.seenOrder.length > this.capacity * 4) {
      const removed = this.seenOrder.shift()
      if (removed !== undefined) this.seen.delete(removed)
    }
  }

  private commitEvents(events: readonly TraceEvent[], projectId: string, previouslyDropped: number, urgent = true): void {
    const sorted = [...events].sort(compareTraceEvents)
    const trimmed = Math.max(0, sorted.length - this.capacity)
    const retained = Object.freeze(sorted.slice(-this.capacity))
    const groups = new Map<string, TraceEvent[]>()
    for (const event of retained) {
      const key = traceRunKey(event)
      const group = groups.get(key) ?? []
      group.push(event); groups.set(key, group)
    }
    const runs = Object.freeze([...groups.values()].map(events => {
      const run = buildTraceRun(events)
      const guard = this.phaseGuards.get(run.id)
      const guardedPhase = guard?.terminal ?? (guard?.review ? 'review' : guard?.failed ? 'failed' : run.phase)
      return Object.freeze({ ...run, phase: guardedPhase, startedAt: guard?.startedAt ?? run.startedAt })
    }).sort((left, right) => right.lastAt - left.lastAt || left.id.localeCompare(right.id)))
    this.publish({ ...this.snapshot, projectId, events: retained, runs, droppedEvents: previouslyDropped + trimmed }, urgent)
  }

  private clearNotification(): void {
    if (this.pendingNotification !== undefined) clearTimeout(this.pendingNotification)
    this.pendingNotification = undefined
  }

  private notify = (): void => {
    this.clearNotification()
    this.lastNotificationAt = Date.now()
    for (const listener of [...this.listeners]) listener()
  }

  private publish(next: Omit<TraceSnapshot, 'revision'> & { revision?: number }, urgent = true): void {
    this.snapshot = Object.freeze({ ...next, revision: this.snapshot.revision + 1 })
    if (!this.listeners.size) { this.clearNotification(); return }
    // Facts remain synchronously available through getSnapshot. Pure progress
    // notifications are coalesced to 10 Hz; important boundaries notify at once.
    if (urgent || Date.now() - this.lastNotificationAt >= SOFT_NOTIFY_MS) { this.notify(); return }
    if (this.pendingNotification === undefined) {
      this.pendingNotification = setTimeout(this.notify, Math.max(1, SOFT_NOTIFY_MS - (Date.now() - this.lastNotificationAt)))
    }
  }
}
