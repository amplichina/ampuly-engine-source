// One answer path for every card the owner answers (frontend redesign S1-B, policy). Whatever surface the
// click comes from -- the approval centre, 总控, 项目中心, the browser pane, the conversation's card, the employee's
// workbench, a gate's 放行 / 打回 -- the same rules hold:
//
//   * one answer out per card at a time: while it is out the card's buttons are disabled and say 「发出去了，等回执」;
//     a second click sends nothing;
//   * the client_request_id is stable per (card, answer) while that answer is out or unconfirmed, so the server can
//     tell a repeat from a new decision (it dedupes on the id); it is never minted from the clock on every click;
//   * no receipt after 30 s, or the connection cut between the send and the receipt: the card says the decision may
//     have gone through, nothing is re-sent on its own, and the NEXT click first asks for the list and sends only if
//     the card is still open (断线与超时都不自动重发);
//   * a receipt for a project other than the one on screen is dropped; a card of another project is refused before
//     anything is sent;
//   * every refusal is said in words, through refusalInWords -- 没连上 / 回执丢了 / 卡已关 / 不属于当前项目 and the rest.
//
// Pure of React except `useAnswering`; the transport (the socket, the list request, the active project, the
// locale) is handed in, so the path is tested with a fake one.

import { useSyncExternalStore } from 'react'
import { refusalInWords, type Locale } from '../i18n'
import type { InteractionDecision, InteractionReplyReceipt, InteractionReplyRequest } from '../types/chat'

export type AnswerState = 'pending' | 'lost'

export interface AnswerEntry {
  checkpointId: string
  optionId: string
  clientRequestId: string
  origin: string
  state: AnswerState
}

/** By checkpoint id: what is out (`pending`) or was never confirmed (`lost`). Read by the surfaces to draw the card. */
export type Answering = Readonly<Record<string, AnswerEntry>>

export interface AnswerAsk {
  checkpointId: string
  checkpointType: string
  /** Which of the card's answers; the stable request id is per (card, answer). */
  optionId: string
  decision: InteractionDecision
  origin?: string
  /** The project the card belongs to, when the server said: another project's card is refused before sending. */
  projectId?: string
  requesterTaskId?: string
  requesterSessionId?: string
  /** A request id the caller already keeps stable per card (the conversation's card does); minted here otherwise. */
  clientRequestId?: string
}

export type AnswerOutcome =
  | { kind: 'accepted'; receipt: InteractionReplyReceipt }
  | { kind: 'refused'; code: string; reason?: string; words: string }
  /** A receipt of another project: nothing on this project's cards is said about it. */
  | { kind: 'dropped'; code: 'foreign_project'; words: string }
  | { kind: 'not_sent'; code: 'duplicate_request_in_flight' | 'foreign_project' | 'checkpoint_not_found' | 'request_timeout'; words: string }

export interface AnswerTransport {
  send(projectId: string, request: InteractionReplyRequest): Promise<InteractionReplyReceipt>
  /** Ask for the pauses of the project; the answer reaches the path through `listArrived`. */
  askList(projectId: string): void
  activeProjectId(): string
  locale(): Locale
}

export interface AnswerPathOptions {
  /** How long an answer sent without a promise (a gate decision) waits for its receipt before it counts as lost. */
  timeoutMs?: number
  /** How long a look at the list (before re-sending) waits before it gives up as lost again. */
  verifyTimeoutMs?: number
  /** Called when an answer begun with `begin` never got its receipt. */
  onLost?: (checkpointId: string, origin: string) => void
  setTimeout?: (fn: () => void, ms: number) => unknown
  clearTimeout?: (handle: unknown) => void
}

export const ANSWER_TIMEOUT_MS = 30_000
export const NAMESPACE = 'approvals.refused'

/** The receipt never came, or the connection was cut before it did: the decision may have gone through. */
const LOST_CODES = new Set(['request_timeout', 'connection_lost', 'reply_outcome_unknown'])
/** The console is not connected: nothing was sent. */
const OFFLINE_CODES = new Set(['socket_unavailable', 'send_failed', 'send_queue_full'])
/** The card is gone: handled already, or its run has ended. */
const CLOSED_CODES = new Set(['checkpoint_not_found', 'invalid_state', 'gate_not_found', 'already_decided', 'approval_expired'])

export type RefusalKind = 'offline' | 'lost' | 'closed' | 'foreign' | 'other'

/** Which of the design's four sentences a refusal code belongs to, for a surface that styles them apart. */
export function refusalKind(code: unknown): RefusalKind {
  const raw = String(code ?? '').trim()
  if (LOST_CODES.has(raw)) return 'lost'
  if (OFFLINE_CODES.has(raw)) return 'offline'
  if (CLOSED_CODES.has(raw)) return 'closed'
  if (raw === 'foreign_project') return 'foreign'
  return 'other'
}

/** A refusal in the reader's words -- the one road every surface takes (refusalInWords, with the reason weighed). */
export function answerWords(locale: Locale, code: unknown, reason?: unknown, namespace: string = NAMESPACE): string {
  return refusalInWords(locale, namespace, code, reason)
}

/** The receipt a caller that speaks receipts (the conversation's card) gets for an outcome. */
export function receiptOf(outcome: AnswerOutcome, request: Pick<InteractionReplyRequest, 'checkpointId' | 'checkpointType' | 'clientRequestId'>): InteractionReplyReceipt {
  if (outcome.kind === 'accepted') return outcome.receipt
  return {
    ok: false,
    accepted: false,
    error: outcome.code,
    reason: outcome.kind === 'refused' ? outcome.reason : undefined,
    checkpoint_id: request.checkpointId,
    checkpoint_type: request.checkpointType,
    client_request_id: request.clientRequestId,
  }
}

const EMPTY: Answering = Object.freeze({})

export class AnswerPath {
  private entries: Record<string, AnswerEntry> = EMPTY as Record<string, AnswerEntry>
  private listeners = new Set<() => void>()
  private timers = new Map<string, unknown>()
  private looks = new Map<string, { resolve: (state: 'open' | 'closed' | 'unknown') => void; timer: unknown }>()
  private seq = 0
  private readonly timeoutMs: number
  private readonly verifyTimeoutMs: number
  private readonly setTimer: (fn: () => void, ms: number) => unknown
  private readonly clearTimer: (handle: unknown) => void

  constructor(private readonly transport: AnswerTransport, private readonly options: AnswerPathOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? ANSWER_TIMEOUT_MS
    this.verifyTimeoutMs = options.verifyTimeoutMs ?? ANSWER_TIMEOUT_MS
    this.setTimer = options.setTimeout ?? ((fn, ms) => globalThis.setTimeout(fn, ms))
    this.clearTimer = options.clearTimeout ?? ((handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>))
  }

  // ── what the surfaces read ──────────────────────────────────────────────

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  /** The same object until something changes, as useSyncExternalStore needs. */
  readonly snapshot = (): Answering => this.entries

  entry(checkpointId: string): AnswerEntry | undefined {
    return this.entries[checkpointId]
  }

  isPending(checkpointId: string): boolean {
    return this.entries[checkpointId]?.state === 'pending'
  }

  // ── answering a card (a promise-shaped send: interaction_reply) ─────────

  async answer(ask: AnswerAsk): Promise<AnswerOutcome> {
    const locale = this.transport.locale()
    const projectId = this.transport.activeProjectId()
    const origin = ask.origin ?? 'card'
    const current = this.entries[ask.checkpointId]
    if (current?.state === 'pending') {
      return { kind: 'not_sent', code: 'duplicate_request_in_flight', words: answerWords(locale, 'duplicate_request_in_flight') }
    }
    if (ask.projectId && ask.projectId !== projectId) {
      return { kind: 'not_sent', code: 'foreign_project', words: answerWords(locale, 'foreign_project') }
    }
    // The stable id: the one already out for this answer, else a new one for this (card, answer).
    const clientRequestId = ask.clientRequestId
      || (current && current.optionId === ask.optionId ? current.clientRequestId : this.mintRequestId(origin, ask.checkpointId, ask.optionId))
    this.set({ checkpointId: ask.checkpointId, optionId: ask.optionId, clientRequestId, origin, state: 'pending' })
    if (current?.state === 'lost') {
      // The last answer on this card never got its receipt: look at the list before sending anything.
      const state = await this.lookAtList(ask.checkpointId, projectId)
      if (state !== 'open') {
        const held = this.entries[ask.checkpointId]
        if (state === 'closed') this.clear(ask.checkpointId)
        else if (held) this.set({ ...held, state: 'lost' })
        const code = state === 'closed' ? 'checkpoint_not_found' : 'request_timeout'
        return { kind: 'not_sent', code, words: answerWords(locale, code) }
      }
    }
    let receipt: InteractionReplyReceipt
    try {
      receipt = await this.transport.send(projectId, {
        checkpointId: ask.checkpointId,
        checkpointType: ask.checkpointType,
        clientRequestId,
        requesterTaskId: ask.requesterTaskId,
        requesterSessionId: ask.requesterSessionId,
        decision: ask.decision,
      })
    } catch (error) {
      this.clear(ask.checkpointId)
      const code = error instanceof Error ? error.message : String(error ?? '')
      return { kind: 'refused', code, words: answerWords(locale, code) }
    }
    return this.settleWith(ask.checkpointId, clientRequestId, receipt)
  }

  private settleWith(checkpointId: string, clientRequestId: string, receipt: InteractionReplyReceipt): AnswerOutcome {
    const locale = this.transport.locale()
    const mine = this.entries[checkpointId]
    // A receipt of another project than the one on screen says nothing about this project's cards.
    const receiptProject = String(receipt.project_id ?? '').trim()
    if (receiptProject && receiptProject !== this.transport.activeProjectId()) {
      if (mine?.clientRequestId === clientRequestId) this.clear(checkpointId)
      return { kind: 'dropped', code: 'foreign_project', words: answerWords(locale, 'foreign_project') }
    }
    if (receipt.accepted) {
      this.clear(checkpointId)
      return { kind: 'accepted', receipt }
    }
    const code = String(receipt.error ?? '').trim()
    if (LOST_CODES.has(code) && mine) this.set({ ...mine, state: 'lost' })
    else this.clear(checkpointId)
    return { kind: 'refused', code, reason: receipt.reason, words: answerWords(locale, code, receipt.reason) }
  }

  // ── answering without a promise (gate_decide: the ack comes through the socket's handler) ──

  /** Mark an answer as out. `fresh` is false when one is already out for this card (the click sends nothing);
   *  `verify` is true when the last answer was never confirmed: look at the list (`lookAtList`) before sending. */
  begin(checkpointId: string, optionId: string, origin: string): { clientRequestId: string; fresh: boolean; verify: boolean } {
    const current = this.entries[checkpointId]
    if (current?.state === 'pending') return { clientRequestId: current.clientRequestId, fresh: false, verify: false }
    const clientRequestId = current && current.optionId === optionId ? current.clientRequestId : this.mintRequestId(origin, checkpointId, optionId)
    this.set({ checkpointId, optionId, clientRequestId, origin, state: 'pending' })
    this.clearTimerFor(checkpointId)
    this.timers.set(checkpointId, this.setTimer(() => {
      this.timers.delete(checkpointId)
      const entry = this.entries[checkpointId]
      if (!entry || entry.state !== 'pending') return
      this.set({ ...entry, state: 'lost' })
      this.options.onLost?.(checkpointId, entry.origin)
    }, this.timeoutMs))
    return { clientRequestId, fresh: true, verify: current?.state === 'lost' }
  }

  /** The receipt came (or the send was refused on the spot): the answer is no longer out. A lost code keeps the
   *  card marked so the next click looks first. */
  settle(checkpointId: string, code?: string): void {
    this.clearTimerFor(checkpointId)
    const entry = this.entries[checkpointId]
    if (!entry) return
    if (code && LOST_CODES.has(code)) this.set({ ...entry, state: 'lost' })
    else this.clear(checkpointId)
  }

  /** Settle every answer begun from an origin (a receipt that names no card). */
  settleOrigin(origin: string, code?: string): void {
    for (const entry of Object.values(this.entries)) if (entry.origin === origin) this.settle(entry.checkpointId, code)
  }

  // ── the list ─────────────────────────────────────────────────────────────

  /** Ask for the project's pauses and wait for them: is this card still open? `unknown` when nothing came back. */
  lookAtList(checkpointId: string, projectId: string): Promise<'open' | 'closed' | 'unknown'> {
    return new Promise(resolve => {
      const previous = this.looks.get(checkpointId)
      if (previous) { this.clearTimer(previous.timer); previous.resolve('unknown') }
      const timer = this.setTimer(() => {
        if (this.looks.get(checkpointId)?.timer === timer) { this.looks.delete(checkpointId); resolve('unknown') }
      }, this.verifyTimeoutMs)
      this.looks.set(checkpointId, { resolve, timer })
      this.transport.askList(projectId)
    })
  }

  /** The pauses of the project on screen came back: answer the looks, and forget unconfirmed answers of cards that
   *  are gone (the card was decided -- by that answer or another -- and there is nothing left to hold for it). */
  listArrived(openCheckpointIds: Iterable<string>): void {
    const open = new Set(openCheckpointIds)
    for (const [checkpointId, look] of [...this.looks.entries()]) {
      this.looks.delete(checkpointId)
      this.clearTimer(look.timer)
      look.resolve(open.has(checkpointId) ? 'open' : 'closed')
    }
    for (const entry of Object.values(this.entries)) {
      if (entry.state === 'lost' && !open.has(entry.checkpointId)) this.clear(entry.checkpointId)
    }
  }

  /** The project on screen changed (lib/currentProject): nothing out or unconfirmed belongs to it any more. A look
   *  at the list still waiting is answered `closed` -- the card is not this project's -- so a click waiting on it
   *  sends nothing to the new project. */
  forgetAll(): void {
    for (const [checkpointId, look] of [...this.looks.entries()]) {
      this.looks.delete(checkpointId)
      this.clearTimer(look.timer)
      look.resolve('closed')
    }
    for (const timer of this.timers.values()) this.clearTimer(timer)
    this.timers.clear()
    if (Object.keys(this.entries).length === 0) return
    this.entries = EMPTY as Record<string, AnswerEntry>
    this.notify()
  }

  // ── internals ────────────────────────────────────────────────────────────

  private mintRequestId(origin: string, checkpointId: string, optionId: string): string {
    this.seq += 1
    const cryptoApi = globalThis.crypto
    const nonce = cryptoApi && typeof cryptoApi.randomUUID === 'function' ? cryptoApi.randomUUID().slice(0, 8) : Math.random().toString(36).slice(2, 10)
    return `${origin}-${checkpointId}-${optionId}-${this.seq}-${nonce}`
  }

  private set(entry: AnswerEntry): void {
    this.entries = { ...this.entries, [entry.checkpointId]: entry }
    this.notify()
  }

  private clear(checkpointId: string): void {
    if (!(checkpointId in this.entries)) return
    const next = { ...this.entries }
    delete next[checkpointId]
    this.entries = Object.keys(next).length ? next : (EMPTY as Record<string, AnswerEntry>)
    this.clearTimerFor(checkpointId)
    this.notify()
  }

  private clearTimerFor(checkpointId: string): void {
    const timer = this.timers.get(checkpointId)
    if (timer !== undefined) { this.clearTimer(timer); this.timers.delete(checkpointId) }
  }

  private notify(): void {
    for (const listener of [...this.listeners]) listener()
  }
}

/** What is out or unconfirmed, for the page to draw. */
export function useAnswering(path: AnswerPath): Answering {
  return useSyncExternalStore(path.subscribe, path.snapshot, path.snapshot)
}
