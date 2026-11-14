// One request tracker for every write the console sends (frontend redesign S2-B, policy, candidate 6):
// each write is in one of five states, and a button never says 「点了没反应」 nor waits for ever.
//
//   * idle      nothing out for this key;
//   * busy      sent, the receipt is out: the button is disabled and says 「发出去了，等回执」;
//   * settled   the receipt came (ok or refused -- the page's own handler words a refusal);
//   * timeout   no receipt within the kind's time: 「没收到回执：结果可能已生效，先看列表再决定要不要再点」;
//   * unknown   the request never left (没发出去（没连上）) or the connection was cut while it was out (结果未知).
//
// Timeouts by kind (the design's table): a card answer 30 s, a list read 5 s, a draft save 10 s, a publish or a trial
// 30 s, every other write 15 s. A timed-out or unknown request is NEVER re-sent on its own: the next click is a new
// request, and the words tell the person to look at the list first. Reads may still wait in the socket's queue for a
// reconnect (lib/wsClient queuesForReconnect); a write is refused on the spot, which the tracker says at once.
//
// The card answers keep lib/answerPath as their card-level layer (one answer out per card, the stable request id, the
// look at the list before re-sending); the tracker is the general layer under every other button, and a gate decision
// sits on both. Pure of React except `useTracked`; the clock and the locale are handed in, so it is tested with fakes.

import { useSyncExternalStore } from 'react'
import { translateFor, type Locale } from '../i18n'

export type RequestState = 'idle' | 'busy' | 'settled' | 'timeout' | 'unknown'
export type RequestKind = 'answer' | 'list' | 'draft' | 'publish' | 'write'
export type SendDisposition = 'sent' | 'queued' | 'queue-full' | 'send-failed'
/** What a send callback answers: the socket's disposition, `true` (sent) / `false` (not sent -- the caller said why
 *  itself, nothing is worded here), nothing at all (the same as `false`), or a promise of one of those. */
export type SendResult = SendDisposition | boolean | void

/** 回答卡 30 s, 列表读取 5 s, 保存草稿 10 s, 发布与试跑 30 s, 其它写 15 s. */
export const REQUEST_TIMEOUT_MS: Readonly<Record<RequestKind, number>> = Object.freeze({
  answer: 30_000,
  list: 5_000,
  draft: 10_000,
  publish: 30_000,
  write: 15_000,
})

export interface RequestEntry {
  key: string
  /** The wire action the receipt names (`workflow_publish`); '' when the caller gave none. */
  action: string
  /** What the request acts on (an order, a flow, a checkpoint), matched against the receipt's id fields; '' for none. */
  id: string
  kind: RequestKind
  state: RequestState
  /** Why it is no longer busy: `not_sent`, `request_timeout`, `connection_lost`, the receipt's error code, or ''. */
  code: string
  /** The sentence for the state, in the reader's words ('' when there is nothing to say). */
  words: string
  startedAt: number
}

/** By key: every request that is out or was never confirmed. The same object until something changes. */
export type Tracked = Readonly<Record<string, RequestEntry>>

export interface TrackOptions {
  kind?: RequestKind
  /** Overrides the kind's timeout. */
  timeoutMs?: number
  /** The wire action and the id the receipt will name; parsed from the key (`action:id`) when left out. */
  action?: string
  id?: string
}

export interface RequestTrackerOptions {
  locale: () => Locale
  now?: () => number
  setTimeout?: (fn: () => void, ms: number) => unknown
  clearTimeout?: (handle: unknown) => void
  /** Called after an entry leaves busy (settled, timeout, unknown) or is turned unknown before it left, with the entry as it now is. */
  onChange?: (entry: RequestEntry, previous: RequestState) => void
}

/** The fields a receipt may name its subject by. An entry with an id settles on a receipt that carries the same value
 *  in any of them, or on one that carries none of them (a refusal that could not name the subject). */
export const RECEIPT_ID_FIELDS = Object.freeze([
  'workflow_id', 'order_id', 'task_id', 'checkpoint_id', 'request_id', 'session_id', 'spec_id', 'node_id',
  'candidate_id', 'proposal_id', 'component_id', 'binding_key', 'reservation_id', 'draft_id',
] as const)

export const NOT_SENT = 'not_sent'
export const TIMED_OUT = 'request_timeout'
export const CONNECTION_LOST = 'connection_lost'

/** `action:id`, or the bare action when there is no id. */
export function keyOf(action: string, id = ''): string {
  return id ? `${action}:${id}` : action
}

const EMPTY: Tracked = Object.freeze({})

export class RequestTracker {
  private entries: Record<string, RequestEntry> = EMPTY as Record<string, RequestEntry>
  private listeners = new Set<() => void>()
  private timers = new Map<string, unknown>()
  /** Per key: which track() call the timer and a late promise belong to; an older one changes nothing. */
  private seqs = new Map<string, number>()
  private seq = 0
  private readonly clock: () => number
  private readonly setTimer: (fn: () => void, ms: number) => unknown
  private readonly clearTimer: (handle: unknown) => void

  constructor(private readonly options: RequestTrackerOptions) {
    this.clock = options.now ?? (() => Date.now())
    this.setTimer = options.setTimeout ?? ((fn, ms) => globalThis.setTimeout(fn, ms))
    this.clearTimer = options.clearTimeout ?? ((handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>))
  }

  // ── what the pages read ────────────────────────────────────────────────

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  readonly snapshot = (): Tracked => this.entries

  entry(key: string): RequestEntry | undefined {
    return this.entries[key]
  }

  stateOf(key: string): RequestState {
    return this.entries[key]?.state ?? 'idle'
  }

  isBusy(key: string): boolean {
    return this.stateOf(key) === 'busy'
  }

  /** The sentence a button or a note shows for a state: 「发出去了，等回执」 while busy, the two "look first" sentences
   *  after a timeout or a cut, 「没发出去（没连上）」 for a request that never left, nothing for idle and settled. */
  wordsFor(state: RequestState, code = ''): string {
    const t = translateFor(this.options.locale())
    switch (state) {
      case 'busy': return t('request.busy')
      case 'timeout': return t('request.timeout')
      case 'unknown': return code === NOT_SENT ? t('request.notSent') : t('request.unknown')
      default: return ''
    }
  }

  // ── sending ────────────────────────────────────────────────────────────

  /** Send through `send` and follow the request until its receipt, its timeout or the connection's cut. While the
   *  key is busy a second call sends nothing and hands back the entry that is out. */
  track(key: string, send: () => SendResult | Promise<SendResult>, options: TrackOptions = {}): RequestEntry {
    const current = this.entries[key]
    if (current?.state === 'busy') return current
    const colon = key.indexOf(':')
    const kind = options.kind ?? 'write'
    const entry: RequestEntry = {
      key,
      action: options.action ?? (colon >= 0 ? key.slice(0, colon) : key),
      id: options.id ?? (colon >= 0 ? key.slice(colon + 1) : ''),
      kind,
      state: 'busy',
      code: '',
      words: this.wordsFor('busy'),
      startedAt: this.clock(),
    }
    const seq = ++this.seq
    this.seqs.set(key, seq)
    this.set(entry)
    this.clearTimerFor(key)
    this.timers.set(key, this.setTimer(() => {
      this.timers.delete(key)
      if (this.seqs.get(key) !== seq) return
      this.leaveBusy(key, 'timeout', TIMED_OUT)
    }, options.timeoutMs ?? REQUEST_TIMEOUT_MS[kind]))
    let result: SendResult | Promise<SendResult>
    try {
      result = send()
    } catch {
      result = 'send-failed'
    }
    if (result && typeof (result as Promise<SendResult>).then === 'function') {
      ;(result as Promise<SendResult>).then(
        (settled) => { if (this.seqs.get(key) === seq) this.applySend(key, settled) },
        () => { if (this.seqs.get(key) === seq) this.applySend(key, 'send-failed') },
      )
    } else {
      this.applySend(key, result as SendResult)
    }
    // Forgotten on the spot (the caller said why it did not send): idle, with nothing to say.
    return this.entries[key] ?? { ...entry, state: 'idle', words: '' }
  }

  /** The receipt came: no longer out. `code` is the receipt's error when it refused (the page words that itself). */
  settle(key: string, code = ''): void {
    if (!this.entries[key]) return
    this.leaveBusy(key, 'settled', code)
  }

  /** Every entry of `action` whose id the receipt names (or that the receipt, naming nothing, answers for). */
  settleAction(action: string, id = '', code = ''): RequestEntry[] {
    const settled: RequestEntry[] = []
    for (const entry of Object.values(this.entries)) {
      if (entry.action !== action) continue
      if (id && entry.id && entry.id !== id) continue
      if (entry.state !== 'busy') continue
      this.leaveBusy(entry.key, 'settled', code)
      settled.push(this.entries[entry.key])
    }
    return settled
  }

  /** A receipt from the socket (an `ack` payload): settles what it answers. Nothing else is read from it. */
  receipt(payload: Record<string, unknown>): RequestEntry[] {
    const action = String(payload.action ?? '').trim()
    if (!action) return []
    const code = payload.ok === false ? String(payload.error ?? '').trim() : ''
    const named = RECEIPT_ID_FIELDS
      .map(field => String(payload[field] ?? '').trim())
      .filter(Boolean)
    if (named.length === 0) return this.settleAction(action, '', code)
    const settled: RequestEntry[] = []
    for (const entry of Object.values(this.entries)) {
      if (entry.action !== action || entry.state !== 'busy') continue
      if (entry.id && !named.includes(entry.id)) continue
      this.leaveBusy(entry.key, 'settled', code)
      settled.push(this.entries[entry.key])
    }
    return settled
  }

  /** The connection was cut: whatever was out is unknown now (结果未知), and is not sent again. Returns them. */
  disconnected(): RequestEntry[] {
    const cut: RequestEntry[] = []
    for (const entry of Object.values(this.entries)) {
      if (entry.state !== 'busy') continue
      this.leaveBusy(entry.key, 'unknown', CONNECTION_LOST)
      cut.push(this.entries[entry.key])
    }
    return cut
  }

  forget(key: string): void {
    if (!(key in this.entries)) return
    this.clearTimerFor(key)
    this.seqs.delete(key)
    const next = { ...this.entries }
    delete next[key]
    this.entries = Object.keys(next).length ? next : (EMPTY as Record<string, RequestEntry>)
    this.notify()
  }

  // ── internals ──────────────────────────────────────────────────────────

  private applySend(key: string, result: SendResult): void {
    const entry = this.entries[key]
    if (!entry || entry.state !== 'busy') return
    if (result === true || result === 'sent' || result === 'queued') return
    if (result === false || result === undefined) {
      // Not sent for a reason the caller worded itself (the card is closed, the turn is gone): nothing is out.
      this.forget(key)
      return
    }
    // 'send-failed' | 'queue-full': it never left -- said at once, and not queued to go out by itself later.
    this.leaveBusy(key, 'unknown', NOT_SENT)
  }

  private leaveBusy(key: string, state: Exclude<RequestState, 'idle' | 'busy'>, code: string): void {
    const entry = this.entries[key]
    if (!entry) return
    const previous = entry.state
    this.clearTimerFor(key)
    const next: RequestEntry = { ...entry, state, code, words: this.wordsFor(state, code) }
    this.set(next)
    this.options.onChange?.(next, previous)
  }

  private set(entry: RequestEntry): void {
    this.entries = { ...this.entries, [entry.key]: entry }
    this.notify()
  }

  private clearTimerFor(key: string): void {
    const timer = this.timers.get(key)
    if (timer !== undefined) { this.clearTimer(timer); this.timers.delete(key) }
  }

  private notify(): void {
    for (const listener of [...this.listeners]) listener()
  }
}

// ── for the pages ────────────────────────────────────────────────────────

/** What is out or unconfirmed, for the page to draw. */
export function useTracked(tracker: RequestTracker): Tracked {
  return useSyncExternalStore(tracker.subscribe, tracker.snapshot, tracker.snapshot)
}

export function stateOf(tracked: Tracked, key: string): RequestState {
  return tracked[key]?.state ?? 'idle'
}

export function isBusy(tracked: Tracked, key: string): boolean {
  return stateOf(tracked, key) === 'busy'
}

/** The id of the one busy request among these actions (the ad-hoc `<verb>ingId` shape the pages read), else null. */
export function busyIdOf(tracked: Tracked, actions: string | readonly string[]): string | null {
  const wanted = typeof actions === 'string' ? [actions] : actions
  for (const entry of Object.values(tracked)) {
    if (entry.state === 'busy' && wanted.includes(entry.action)) return entry.id || null
  }
  return null
}

export function anyBusy(tracked: Tracked, actions: string | readonly string[]): boolean {
  const wanted = typeof actions === 'string' ? [actions] : actions
  return Object.values(tracked).some(entry => entry.state === 'busy' && wanted.includes(entry.action))
}

/** The most recently started request among these actions, whatever its state. */
export function latestOf(tracked: Tracked, actions: string | readonly string[]): RequestEntry | undefined {
  const wanted = typeof actions === 'string' ? [actions] : actions
  let latest: RequestEntry | undefined
  for (const entry of Object.values(tracked)) {
    if (!wanted.includes(entry.action)) continue
    if (!latest || entry.startedAt >= latest.startedAt) latest = entry
  }
  return latest
}

/** An ad-hoc flag narrowed by the tracker: the flag's id while that request is still out, else null -- so a flag the
 *  receipt handler never got to clear cannot keep a button waiting past the timeout or the cut. */
export function stillOut(tracked: Tracked, flag: string | null | undefined, actions: string | readonly string[]): string | null {
  if (!flag) return null
  const wanted = typeof actions === 'string' ? [actions] : actions
  return wanted.some(action => isBusy(tracked, keyOf(action, flag))) ? flag : null
}
