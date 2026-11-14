// Trying one step on pinned upstream results: what the server says has to be pinned, what a person chose, and
// whether the run button may be pressed. No view code here -- the panel that draws this can change without it.

export interface NodeTrialPinCandidate {
  spec_id: string
  created_at: string
  files: number
  /** That trial itself ran on pinned results, so it carries what its own upstream handed it. */
  pinned_upstream: boolean
}

export interface NodeTrialUpstream {
  node_id: string
  name: string
  required: boolean
  candidates: NodeTrialPinCandidate[]
}

export interface NodeTrialPinning {
  workflow_id: string
  node_id: string
  required: string[]
  optional: string[]
  /** Human steps passed through: the trial assumes they released, and says so. */
  released_by_assumption: string[]
  /** Why this step cannot be tried on pinned results at all ('' when it can). The sentence is the server's, in
   *  English; `blocked_code` and `blocked_step` let the page say it in the reader's language. */
  blocked: string
  blocked_code?: string
  blocked_step?: string
  upstream: NodeTrialUpstream[]
}

/** upstream node id -> the trial chosen for it ('' = none). */
export type PinSelection = Record<string, string>

export interface NodeTrialPin { node_id: string; spec_id: string }

/** Keep what the person chose while it is still on offer; otherwise the newest trial of a required step, and nothing
 *  for an optional one -- an optional pin is a decision, not a default. */
export function reconcileSelection(pinning: NodeTrialPinning | null, previous: PinSelection = {}): PinSelection {
  const next: PinSelection = {}
  for (const step of pinning?.upstream ?? []) {
    const offered = step.candidates.map(item => item.spec_id)
    const kept = previous[step.node_id]
    if (kept !== undefined && (kept === '' ? !step.required : offered.includes(kept))) next[step.node_id] = kept
    else next[step.node_id] = step.required ? (offered[0] ?? '') : ''
  }
  return next
}

export function pinsFor(pinning: NodeTrialPinning | null, selection: PinSelection): { pins: NodeTrialPin[]; missing: NodeTrialUpstream[] } {
  const pins: NodeTrialPin[] = []
  const missing: NodeTrialUpstream[] = []
  for (const step of pinning?.upstream ?? []) {
    const chosen = selection[step.node_id] ?? ''
    if (chosen && step.candidates.some(item => item.spec_id === chosen)) pins.push({ node_id: step.node_id, spec_id: chosen })
    else if (step.required) missing.push(step)
  }
  return { pins, missing }
}

/** The reply sent for a question a step asked. One question: the person's words as they are. Several: each answer
 *  under the question it answers, so the step can tell which is which. Empty until every question has an answer. */
export function composeReply(questions: ReadonlyArray<{ header: string; question: string }>, answers: readonly string[]): string {
  const given = questions.map((_, index) => (answers[index] ?? '').trim())
  if (questions.length <= 1) return given[0] ?? (answers[0] ?? '').trim()
  if (given.some(item => !item)) return ''
  return questions.map((item, index) => `${index + 1}. ${item.header || item.question}\n回答：${given[index]}`).join('\n')
}

/** The paid tool a node trial admits, and the image ceiling the owner set for it : 2 by default, changed
 * before the run, never more than 6. The server enforces the same bounds. */
export const IMAGE_TOOL = 'image_generate'
export const DEFAULT_IMAGE_LIMIT = 2
export const MAX_IMAGE_LIMIT = 6

/** What the person typed, as a ceiling -- or null while it is not one (empty, a fraction, out of range). Never
 *  clamped while typing: "13" on the way from "1" to "3" must not turn into 6, the most expensive value. */
export function parseImageLimit(raw: string): number | null {
  const text = raw.trim()
  if (!/^\d+$/.test(text)) return null
  const value = Number(text)
  return value >= 1 && value <= MAX_IMAGE_LIMIT ? value : null
}

/** Images of a trial that is over whose outcome nobody established: the request left and no result came back (an
 *  error after sending, a deadline). They may have been billed. While a trial runs, a held image is just in flight. */
export function imagesOfUnknownOutcome(trial: { state: string; budget?: { held?: Record<string, number> } }): number {
  if (['queued', 'running', 'waiting_for_answer'].includes(trial.state)) return 0
  const held = Number(trial.budget?.held?.image ?? 0)
  return Number.isFinite(held) && held > 0 ? held : 0
}

export type TrialGateReason = '' | 'draft' | 'busy' | 'waiting' | 'plan_pending' | 'blocked' | 'missing_upstream'

/** Why the run button is off. A step with upstream steps never runs before the server's plan for it has arrived:
 *  the page does not decide on its own what counts as an input. */
export function trialGate(input: {
  dependsOn: string[]
  nodeId: string
  draftOpen: boolean
  busy: boolean
  /** A trial of this step is waiting for a person's answer: it is answered or abandoned before another is started
   *  (its question is shown on the step's latest trial only, and its allowance is still open). */
  waiting?: boolean
  pinning: NodeTrialPinning | null
  selection: PinSelection
}): { canRun: boolean; reason: TrialGateReason; pins: NodeTrialPin[]; missing: NodeTrialUpstream[] } {
  const none = { pins: [] as NodeTrialPin[], missing: [] as NodeTrialUpstream[] }
  if (input.draftOpen) return { canRun: false, reason: 'draft', ...none }
  if (input.busy) return { canRun: false, reason: 'busy', ...none }
  if (input.waiting) return { canRun: false, reason: 'waiting', ...none }
  const plan = input.pinning && input.pinning.node_id === input.nodeId ? input.pinning : null
  // A step with no upstream needs no plan to run -- but when the plan has arrived and says the step cannot be tried
  // (a tool a node trial cannot provide), that is said before the click, not as a refusal after it.
  if (input.dependsOn.length === 0) return plan?.blocked ? { canRun: false, reason: 'blocked', ...none } : { canRun: true, reason: '', ...none }
  if (!plan) return { canRun: false, reason: 'plan_pending', ...none }
  if (plan.blocked) return { canRun: false, reason: 'blocked', ...none }
  const { pins, missing } = pinsFor(plan, input.selection)
  if (missing.length > 0) return { canRun: false, reason: 'missing_upstream', pins, missing }
  return { canRun: true, reason: '', pins, missing }
}
