/**
 * How often the console may re-read one session's whole transcript (2026-09-26).
 *
 * The console process sat at one full core and the page's connection dropped every minute or two. After every
 * sync the server replays the owner's open cards (a delivery waiting for feedback, a run that needs a decision
 * after a restart) as session messages. The page took each replay for a new message and asked for that session's
 * full detail again -- three sessions, every 6-10 seconds, each answer about 0.6 s of server CPU on a real project.
 * Two rules live here:
 *
 * - a message the page has already shown (same id, same text, same time, same card state) is not news, so it does
 *   not trigger a detail refresh (`SessionMessageLedger`, `storeAlreadyShows`);
 * - refreshes of one session merge: while one is waiting it absorbs the others, two refreshes of the same session
 *   go out at least `SESSION_DETAIL_MIN_INTERVAL_MS` apart, and none goes out while the previous one is still
 *   unanswered (`SessionDetailRefreshGate`).
 */

export const SESSION_DETAIL_REFRESH_DEBOUNCE_MS = 180
export const SESSION_DETAIL_MIN_INTERVAL_MS = 5_000
export const SESSION_DETAIL_IN_FLIGHT_RECHECK_MS = 1_000

export type SessionDetailLevel = 'summary' | 'full'

export interface SessionDetailRefreshWish {
  detailLevel: SessionDetailLevel
  force: boolean
}

export class SessionDetailRefreshGate {
  private readonly lastSentAt = new Map<string, number>()
  private readonly inFlight = new Set<string>()
  private readonly wishes = new Map<string, SessionDetailRefreshWish>()

  constructor(
    private readonly minIntervalMs = SESSION_DETAIL_MIN_INTERVAL_MS,
    private readonly debounceMs = SESSION_DETAIL_REFRESH_DEBOUNCE_MS,
    private readonly inFlightRecheckMs = SESSION_DETAIL_IN_FLIGHT_RECHECK_MS,
    private readonly maxKeys = 500,
  ) {}

  /** Records a wish to refresh `key`, merged into one already waiting: 'full' wins and a forced wish stays forced. */
  want(key: string, detailLevel: SessionDetailLevel, force: boolean): SessionDetailRefreshWish {
    const previous = this.wishes.get(key)
    const merged: SessionDetailRefreshWish = {
      detailLevel: previous?.detailLevel === 'full' || detailLevel === 'full' ? 'full' : 'summary',
      force: Boolean(previous?.force || force),
    }
    this.wishes.set(key, merged)
    this.trim()
    return merged
  }

  /** How long the waiting refresh of `key` has to wait before it may go out. */
  delayFor(key: string, now: number): number {
    if (this.inFlight.has(key)) return Math.max(this.debounceMs, this.inFlightRecheckMs)
    const last = this.lastSentAt.get(key)
    if (last === undefined) return this.debounceMs
    return Math.max(this.debounceMs, last + this.minIntervalMs - now)
  }

  isInFlight(key: string): boolean {
    return this.inFlight.has(key)
  }

  /** The merged wish that may go out now, removed from the waiting set; null when nothing waits. */
  take(key: string): SessionDetailRefreshWish | null {
    const wish = this.wishes.get(key) ?? null
    this.wishes.delete(key)
    return wish
  }

  markSent(key: string, now: number): void {
    this.lastSentAt.set(key, now)
    this.inFlight.add(key)
    this.trim()
  }

  /** The answer came back (or the request failed or timed out): the next refresh may be sent. */
  markSettled(key: string): void {
    this.inFlight.delete(key)
  }

  forget(): void {
    this.lastSentAt.clear()
    this.inFlight.clear()
    this.wishes.clear()
  }

  private trim(): void {
    for (const map of [this.lastSentAt, this.wishes] as Array<Map<string, unknown>>) {
      while (map.size > this.maxKeys) {
        const oldest = map.keys().next().value
        if (oldest === undefined) break
        map.delete(oldest)
      }
    }
  }
}

export interface SessionMessageLike {
  id: string
  channelId: string
  sender: string
  timestamp: number
  content: string
  metadata?: unknown
}

function fnv1a(text: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(16)
}

function stableMetadata(metadata: unknown): string {
  if (metadata === undefined || metadata === null) return ''
  try {
    return JSON.stringify(metadata) ?? ''
  } catch {
    return typeof metadata === 'object' ? String(Object.keys(metadata as object).length) : String(metadata)
  }
}

/** What makes two deliveries the same message: its id, or (without one) where, who, when and what. */
export function sessionMessageIdentity(message: SessionMessageLike): string {
  const id = String(message.id ?? '').trim()
  if (id) return `id:${id}`
  return `anon:${message.channelId}|${message.sender}|${message.timestamp}|${fnv1a(String(message.content ?? ''))}`
}

/** What a later delivery must repeat to be "the same": time, text, card state and the rest of its metadata. */
export function sessionMessageFingerprint(message: SessionMessageLike): string {
  const metadata = message.metadata && typeof message.metadata === 'object' ? message.metadata as Record<string, unknown> : undefined
  const status = String(metadata?.checkpoint_status ?? '')
  return `${message.timestamp}|${status}|${fnv1a(String(message.content ?? ''))}|${fnv1a(stableMetadata(message.metadata))}`
}

/** Remembers the session messages the page has handled, so a replay of one is recognised as a replay. */
export class SessionMessageLedger {
  private readonly seen = new Map<string, string>()

  constructor(private readonly maxEntries = 2_000) {}

  /** True when exactly this message (same identity and fingerprint) was handled before; records it either way. */
  seenBefore(message: SessionMessageLike): boolean {
    const identity = sessionMessageIdentity(message)
    const fingerprint = sessionMessageFingerprint(message)
    const previous = this.seen.get(identity)
    this.seen.delete(identity)
    this.seen.set(identity, fingerprint)
    while (this.seen.size > this.maxEntries) {
      const oldest = this.seen.keys().next().value
      if (oldest === undefined) break
      this.seen.delete(oldest)
    }
    return previous === fingerprint
  }

  forget(): void {
    this.seen.clear()
  }
}

/** True when the store already shows this very message: same id, same time, same text, same card state. */
export function storeAlreadyShows(existing: readonly SessionMessageLike[] | undefined, message: SessionMessageLike): boolean {
  const id = String(message.id ?? '').trim()
  if (!id || !existing || existing.length === 0) return false
  let fingerprint: string | null = null
  for (const candidate of existing) {
    if (candidate.id !== id) continue
    fingerprint ??= sessionMessageFingerprint(message)
    if (sessionMessageFingerprint(candidate) === fingerprint) return true
  }
  return false
}
