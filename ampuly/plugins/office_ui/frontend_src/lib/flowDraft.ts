// 需求 → 流程草案 (4.7 Q04-B): the draft of a NEW flow 安安 had the console compile from a requirement. It rides on the
// reply's metadata as `master_flow_draft`; nothing is created until the owner opens it on the builder, where the
// console compiles the stored spec again and the steps arrive as an unsaved draft. Read defensively: it is data.

import { componentCandidatesFrom, draftReference, type FlowDraftReference } from './flowComponentCandidate'

export interface FlowDraftStep {
  id: string
  name: string
  type: 'ai_do' | 'human_do' | 'human_review' | string
  role: string
  skills: string[]
  /** Q05 B6: the browser universal connector this step was given when nothing in the library matched. */
  connector: { goal: string; sites: string[] } | null
  /** What the step does, what it needs and what it hands on (4.7 A05: what 让安安起草这个组件 says about it). */
  does: string
  inputs: string[]
  delivers: string[]
}

export interface FlowDraft {
  reference?: FlowDraftReference
  name: string
  summary: string
  team: string
  steps: FlowDraftStep[]
  matches: Array<{ step: string; component: string; name: string; by: string }>
  missing: Array<{ step: string; capability: string; reason: string }>
  unassigned: string[]
  /** Per step, what it still owes. 'deliverables' is a step with nothing to hand on; the rest are a drafted
   *  connector's own blanks, which are a different thing and must not be shown as a missing deliverable. */
  incomplete: Array<{ step: string; missing: string[] }>
  undelivered: Array<{ step: string; input: string }>
  openQuestions: string[]
  assumptions: string[]
  sources: Record<string, string>
}

const MAX = 40

/** What a drafted connector still owes, in the owner's words. An unknown key is shown as written. */
const CONNECTOR_BLANKS: Record<string, string> = {
  allowed_domains: '允许访问的网址',
  postconditions: '怎么算做完',
  output: '这一步交出什么',
}

const connectorBlank = (key: string): string => CONNECTOR_BLANKS[key] ?? key

const text = (value: unknown, limit = 200): string => (typeof value === 'string' ? value.slice(0, limit) : '')
const texts = (value: unknown, limit = 200): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').map(item => item.slice(0, limit)).slice(0, MAX) : []
const rows = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object').slice(0, MAX) : []

/** The draft on a reply, or null when there is none (or it is not the shape the console writes). */
export function flowDraftFrom(metadata: unknown): FlowDraft | null {
  const raw = metadata && typeof metadata === 'object' ? (metadata as Record<string, unknown>).master_flow_draft : null
  if (!raw || typeof raw !== 'object') return null
  const draft = raw as Record<string, unknown>
  const steps = rows(draft.nodes).map(node => ({
    id: text(node.id, 64), name: text(node.name, 60) || text(node.id, 64), type: text(node.type, 20),
    role: text(node.role, 64), skills: texts(node.skills, 64),
    connector: node.connector && typeof node.connector === 'object'
      ? { goal: text((node.connector as Record<string, unknown>).goal, 500),
          sites: texts((node.connector as Record<string, unknown>).sites, 120) }
      : null,
    // The compiler appends 「（待补组件：…）」 to a placeholder's responsibility; the placeholder is named apart.
    does: text(node.responsibility, 500).replace(/（待补组件：[^）]*）$/, ''),
    inputs: texts(node.inputs, 120),
    delivers: texts(node.deliverables, 120),
  })).filter(step => step.id)
  if (!steps.length) return null
  const sources: Record<string, string> = {}
  if (draft.sources && typeof draft.sources === 'object') {
    for (const [key, value] of Object.entries(draft.sources as Record<string, unknown>).slice(0, MAX)) {
      if (typeof value === 'string') sources[key] = value.slice(0, 120)
    }
  }
  return {
    name: text(draft.name, 60) || '新业务流',
    reference: draftReference(draft.reference) ?? undefined,
    summary: text(draft.summary, 500),
    team: text(draft.team, 64),
    steps,
    matches: rows(draft.matches).map(item => ({ step: text(item.step, 64), component: text(item.component, 64), name: text(item.name, 60), by: text(item.by, 24) })),
    missing: rows(draft.missing_components).map(item => ({ step: text(item.step, 64), capability: text(item.capability, 80), reason: text(item.reason, 120) })),
    unassigned: texts(draft.unassigned_steps, 64),
    incomplete: rows(draft.incomplete_steps)
      .map(item => ({ step: text(item.step, 64), missing: texts(item.missing, 40) }))
      .filter(item => item.step),
    undelivered: rows(draft.undelivered_inputs).map(item => ({ step: text(item.step, 64), input: text(item.input, 120) })),
    openQuestions: texts(draft.open_questions),
    assumptions: texts(draft.assumptions),
    sources,
  }
}

/** What still stands between the draft and a save, in the order the owner deals with it. Empty = nothing known. */
export function flowDraftGaps(draft: FlowDraft): Array<{ kind: 'component' | 'role' | 'deliverable' | 'connector' | 'input' | 'question'; step: string; detail: string }> {
  const name = (id: string) => draft.steps.find(step => step.id === id)?.name || id
  return [
    ...draft.missing.map(item => ({ kind: 'component' as const, step: name(item.step), detail: item.capability })),
    ...draft.unassigned.map(id => ({ kind: 'role' as const, step: name(id), detail: '' })),
    // A step with nothing to hand on, and a drafted connector's own blanks, are different things: the first
    // stops the save, the second is what the owner confirms on the canvas. Naming them alike would send them
    // looking for a deliverable that is not missing.
    ...draft.incomplete.filter(item => !item.missing.length || item.missing.includes('deliverables'))
      .map(item => ({ kind: 'deliverable' as const, step: name(item.step), detail: '' })),
    ...draft.incomplete.filter(item => item.missing.length && !item.missing.includes('deliverables'))
      .map(item => ({ kind: 'connector' as const, step: name(item.step),
                      detail: item.missing.map(connectorBlank).join('、') })),
    ...draft.undelivered.map(item => ({ kind: 'input' as const, step: name(item.step), detail: item.input })),
    ...draft.openQuestions.map(question => ({ kind: 'question' as const, step: '', detail: question })),
  ]
}

/** What a placeholder is missing: an interface to another system, a skill (the default), or a whole flow. */
export type MissingKind = 'connector' | 'skill' | 'flow'

// A flow is named as one; an interface is named by the words people use for one. Anything else is work a skill does
// -- 「海报排版」 is not a connector, and asking for a connector candidate for it sent 安安 the wrong way (review 2026-09-26).
const FLOW_WORDS = /流程|工作流|审批流|子流程/
const CONNECTOR_WORDS = /接口|对接|开放平台|连接器|回调|webhook|\bapi\b|\bsdk\b|\bmcp\b|\berp\b|\bcrm\b|\boa\b/i

/** Where the last of these words starts in the text; -1 when there is none. */
function lastWordAt(words: RegExp, text: string): number {
  let at = -1
  for (const match of text.matchAll(new RegExp(words.source, words.flags.includes('g') ? words.flags : `${words.flags}g`))) at = match.index ?? at
  return at
}

/**
 * What the placeholder on this step is missing, read from its name first and then from what the step does. A name
 * that has both kinds of word is what its last one says, as a Chinese name ends on what the thing is: 「审批流程接口」 is an
 * interface, 「对接开票流程」 a flow.
 */
export function missingKind(draft: FlowDraft, stepId: string): MissingKind {
  const gap = draft.missing.find(item => item.step === stepId)
  const step = draft.steps.find(item => item.id === stepId)
  const capability = gap?.capability ?? ''
  const flowAt = lastWordAt(FLOW_WORDS, capability)
  const connectorAt = lastWordAt(CONNECTOR_WORDS, capability)
  if (flowAt >= 0 || connectorAt >= 0) return flowAt > connectorAt ? 'flow' : 'connector'
  if (CONNECTOR_WORDS.test(step?.does ?? '')) return 'connector'
  return 'skill'
}

/** Where one 让安安起草这个组件 stands (4.7 A05): sent and not yet answered with a candidate, or a candidate drafted. */
export type ComponentAskState = { state: 'asked' } | { state: 'drafted'; candidateId: string; kind?: 'skill' | 'flow' }

// The id the candidate store gives a drafted connector (`<contract name>-<16 hex>`). A reply carries it in its metadata
// (`master_connector_candidates`); a reply from before that carried it only in the receipt's English words.
const CANDIDATE_ID = /\b[a-z][a-z0-9_]{1,63}-[0-9a-f]{16}\b/g
const CANDIDATE_ID_ONLY = /^[a-z][a-z0-9_]{1,63}-[0-9a-f]{16}$/
const GENERATE_TOOL = 'master_generate_connector_candidate'

/**
 * What became of each ask, read from the conversation itself so it survives a reload. `asks` maps the exact text a
 * button sent to the placeholder it is about. An ask is answered by the first reply after it that really drafted a
 * candidate -- the reply names the candidate the drafting tool made (in its metadata, written only then; an older reply
 * in the receipt's words, when the drafting tool was called; a refusal names none) -- including a reply after the owner answered a question 安安 asked
 * first. The next ask for another placeholder closes the window, so a candidate drafted for that one is never credited
 * to this one. Reading this tests, approves or enables nothing.
 */
export function componentAskStates(
  messages: ReadonlyArray<{ sender: string; content: string; metadata?: unknown }>,
  asks: ReadonlyMap<string, string>,
): Record<string, ComponentAskState> {
  const out: Record<string, ComponentAskState> = {}
  let open = ''
  for (const message of messages) {
    // Structured candidates name the exact original source step, even after
    // a reload/new conversation. A similar sentence never binds another node.
    for (const candidate of componentCandidatesFrom(message.metadata)) {
      if (message.sender === 'user') continue
      out[`${candidate.source.message_id}\n${candidate.step_id}`] = {
        state: 'drafted', candidateId: candidate.candidate_id, kind: candidate.kind,
      }
    }
    const content = typeof message.content === 'string' ? message.content : ''
    if (message.sender === 'user') {
      const key = asks.get(content.trim())
      if (key) {
        open = key
        if (!out[key]) out[key] = { state: 'asked' }
      }
      continue
    }
    if (!open) continue
    const meta = message.metadata && typeof message.metadata === 'object' ? message.metadata as Record<string, unknown> : {}
    // The console names the candidate on the reply only when the drafting tool really made one (master_loop), so the
    // name is proof enough -- a reply whose turn broke after drafting carries it without the record of calls.
    const drafted = Array.isArray(meta.master_connector_candidates)
      ? meta.master_connector_candidates.filter((id): id is string => typeof id === 'string' && CANDIDATE_ID_ONLY.test(id))
      : []
    // An older reply had no such name: it is read from the receipt's words, and only when the drafting tool was called
    // (an id-shaped word in 安安's text is not a candidate). The receipt comes after 安安's own words: the last id wins.
    const calls = meta.master_tool_calls
    const calledGenerate = Array.isArray(calls) && calls.some(call => Boolean(call) && (call as { name?: unknown }).name === GENERATE_TOOL)
    const ids = drafted.length ? drafted : calledGenerate ? content.match(CANDIDATE_ID) : null
    if (!ids) continue
    if (out[open]?.state !== 'drafted') out[open] = { state: 'drafted', candidateId: ids[ids.length - 1] }
    open = ''
  }
  return out
}
