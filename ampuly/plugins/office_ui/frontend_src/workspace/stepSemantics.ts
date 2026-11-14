// Workflow cards retain the role tint; a strip across the top says what
// kind of work the step is -- input, concept, visual, delivery, a person's check, a called flow. Front-end only: it is
// derived here from the step's own facts and never written into the package or used to decide anything.
//
// Facts, not keywords in a name: the step's type, its gate, the tools it may call, the files it promises (their
// folder and extension), and where it sits in the graph. Titles do not determine the semantic category.

export type StepSemantic = 'input' | 'concept' | 'visual' | 'delivery' | 'human' | 'call'

export const STEP_SEMANTIC_LABELS: Record<StepSemantic, readonly [string, string]> = {
  input: ['输入', 'Input'],
  concept: ['构思', 'Concept'],
  visual: ['视觉', 'Visual'],
  delivery: ['交付', 'Delivery'],
  human: ['人工确认', 'Person confirms'],
  call: ['调用流程', 'Calls a flow'],
}

const VISUAL_TOOLS = new Set(['image_generate'])
const IMAGE_FILE = /\.(png|jpe?g|webp|svg|gif)$/i
// The first folder of a promised file, where a package keeps its hand-off and its checks.
const DELIVERY_DIRS = new Set(['delivery', 'deliver', 'deliverables', 'handoff', 'release', '交付'])
const REVIEW_DIRS = new Set(['qa', 'review', 'check', 'checks', 'audit', 'acceptance', '审核', '验收'])

type StepLike = { id: string; type?: string; gate?: string; tools?: readonly string[]; deliverables?: readonly string[]; depends_on?: readonly string[] }

const firstFolder = (path: string) => String(path).replace(/\\/g, '/').replace(/^\.\//, '').split('/')[0]?.toLowerCase() ?? ''

export function stepSemantics(steps: readonly StepLike[]): Map<string, StepSemantic> {
  const ids = new Set(steps.map(step => step.id))
  const upstreamOf = new Set<string>()
  for (const step of steps) for (const dep of step.depends_on ?? []) if (ids.has(dep) && dep !== step.id) upstreamOf.add(dep)
  const result = new Map<string, StepSemantic>()
  for (const step of steps) {
    const kind = step.type ?? 'ai_do'
    const out = step.deliverables ?? []
    const folders = new Set(out.map(firstFolder))
    const gated = Boolean(step.gate && step.gate !== 'none')
    let semantic: StepSemantic
    if (kind === 'human_do' || kind === 'human_review') semantic = 'human'
    else if (kind === 'invoke_workflow') semantic = 'call'
    else if ([...folders].some(folder => DELIVERY_DIRS.has(folder))) semantic = 'delivery'
    else if (gated && [...folders].some(folder => REVIEW_DIRS.has(folder))) semantic = 'human'
    else if ((step.tools ?? []).some(tool => VISUAL_TOOLS.has(tool)) || out.some(path => IMAGE_FILE.test(path))) semantic = 'visual'
    else if (!(step.depends_on ?? []).some(dep => ids.has(dep) && dep !== step.id)) semantic = 'input'
    else if (!upstreamOf.has(step.id)) semantic = 'delivery'
    else semantic = 'concept'
    result.set(step.id, semantic)
  }
  return result
}
