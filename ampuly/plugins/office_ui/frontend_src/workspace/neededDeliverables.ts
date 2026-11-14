// What a step needs, by name, and which upstream step hands it over -- and, while a wire is being dragged, where it
// may land. Business steps exchange named deliverables; there are no typed ports here. The same rules run on the
// server (workflow_packages.input_findings); this side only lets the canvas say it before a save.
import type { WorkflowNode } from '../components/WorkflowsPage'
import { connectRejection, type ConnectRejection } from './workflowGraph'

type Step = Pick<WorkflowNode, 'id' | 'depends_on' | 'deliverables'> & { inputs?: string[] }

/** An input is a claim about a file only when it is written like one ("brief/需求.md", "board/*.png"). Anything else
 *  ("客户素材", "the brief") is a note to the reader and is never checked. */
/** The blanks both sides trim -- exactly these, the same set the server uses (workflow_packages._BLANKS).
 *  String.trim() and Python's str.strip() disagree about a few characters (a byte-order mark, for one). */
export function clean(text: string): string {
  return text.replace(/^[ \t\r\n\u3000]+|[ \t\r\n\u3000]+$/g, '')
}

export function looksLikePath(text: string): boolean {
  const value = clean(text)
  return value.includes('/') || /\.[A-Za-z0-9]{1,8}$/.test(value) || value.includes('*')
}

/** `*` is any run of characters (slashes included, as the runtime's own matching has it), `?` is one. */
export function globMatch(pattern: string, text: string): boolean {
  // By code point and with the u flag, so `?` is one character here exactly as it is on the server.
  const source = Array.from(pattern).map(char => (char === '*' ? '[\\s\\S]*' : char === '?' ? '[\\s\\S]' : char.replace(/[.+^${}()|[\]\\\/]/g, '\\$&'))).join('')
  return new RegExp(`^${source}$`, 'u').test(text)
}

/** Does a promised deliverable cover a needed input? Either side may be the pattern ("board/*.png"). */
export function delivers(deliverable: string, input: string): boolean {
  const out = clean(deliverable)
  const need = clean(input)
  return out === need || globMatch(out, need) || globMatch(need, out)
}

export function ancestorsOf(nodes: readonly Step[], nodeId: string): Step[] {
  const byId = new Map(nodes.map(node => [node.id, node]))
  const seen = new Set<string>()
  const queue = [...(byId.get(nodeId)?.depends_on ?? [])]
  const found: Step[] = []
  while (queue.length) {
    const current = queue.shift() as string
    if (seen.has(current) || current === nodeId) continue
    seen.add(current)
    const node = byId.get(current)
    if (!node) continue
    found.push(node)
    queue.push(...(node.depends_on ?? []))
  }
  return found
}

/** The path-like inputs of a step that no upstream step (direct or further back) promises to deliver. */
export function undeliveredInputs(nodes: readonly Step[], node: Step): string[] {
  const needs = (node.inputs ?? []).map(clean).filter(item => item && looksLikePath(item))
  if (!needs.length) return []
  const upstream = ancestorsOf(nodes, node.id).flatMap(step => step.deliverables ?? [])
  return needs.filter(need => !upstream.some(deliverable => delivers(deliverable, need)))
}

/** What one wire carries: the target's declared inputs that the source itself promises. Empty when the target
 *  declares nothing -- the wire is then only an order of work, and the canvas does not pretend otherwise. */
export function edgeCarries(nodes: readonly Step[], sourceId: string, targetId: string): string[] {
  const source = nodes.find(node => node.id === sourceId)
  const target = nodes.find(node => node.id === targetId)
  if (!source || !target) return []
  return (target.inputs ?? []).map(clean).filter(need => need && (source.deliverables ?? []).some(deliverable => delivers(deliverable, need)))
}

export type WireVerdict = 'ok' | ConnectRejection

/** While a wire is dragged from `fromId`: for every other step, whether the wire may land there. From an output
 *  port the dragged end is the dependent step's input; from an input port it is the other way round. */
export function wireTargets(nodes: readonly Pick<WorkflowNode, 'id' | 'depends_on'>[], fromId: string, handle: 'source' | 'target'): Map<string, WireVerdict> {
  const verdicts = new Map<string, WireVerdict>()
  for (const node of nodes) {
    if (node.id === fromId) continue
    const rejection = handle === 'source' ? connectRejection(nodes, fromId, node.id) : connectRejection(nodes, node.id, fromId)
    verdicts.set(node.id, rejection ?? 'ok')
  }
  return verdicts
}
