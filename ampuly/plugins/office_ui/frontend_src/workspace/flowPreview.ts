// 流程预览 (frontend refactor U2-7): the same flow as the canvas, read top to bottom. Pure: the board is a view of the
// draft the canvas holds -- one model, two ways of looking at it -- so nothing here can be edited into being different
// from the canvas. Groups are the flow's role lanes (the engine's model has lanes; the owner's prototype shows
// business phases, which the package schema does not have).

export type PreviewStatus = 'needs_fixing' | 'waiting_for_answer' | 'trial_passed' | 'trial_failed' | 'trial_running' | 'not_tried'

export const PREVIEW_STATUS_LABELS: Record<PreviewStatus, readonly [string, string]> = {
  needs_fixing: ['待补', 'Needs fixing'],
  waiting_for_answer: ['试跑等你回答', 'Trial awaits your answer'],
  trial_passed: ['试跑通过', 'Trial passed'],
  trial_failed: ['试跑未过', 'Trial did not pass'],
  trial_running: ['试跑中', 'Trial running'],
  not_tried: ['未试跑', 'Not tried'],
}

type StepLike = { id: string; name?: string; role?: string; type?: string; gate?: string; responsibility?: string; depends_on?: readonly string[] }
type TrialLike = { node_id: string; state: string }

export type PreviewTile = {
  id: string
  name: string
  kind: string
  gated: boolean
  responsibility: string
  /** Steps it waits for that are really in the draft. */
  depends: number
  status: PreviewStatus
}
export type PreviewGroup = { key: string; title: string; index: number; tiles: PreviewTile[] }

/** A step's status, from facts only: what a save would refuse comes first; then the latest real trial of the saved
 *  step (trials arrive newest first). Never "previewed" or "simulated ok": nothing here pretends a step ran. */
export function previewStatus(stepId: string, needsFixing: ReadonlySet<string>, trials: readonly TrialLike[]): PreviewStatus {
  if (needsFixing.has(stepId)) return 'needs_fixing'
  const latest = trials.find(trial => trial.node_id === stepId)
  if (!latest) return 'not_tried'
  if (latest.state === 'waiting_for_answer') return 'waiting_for_answer'
  if (latest.state === 'succeeded') return 'trial_passed'
  if (latest.state === 'queued' || latest.state === 'running') return 'trial_running'
  return 'trial_failed'
}

export function previewGroups(
  steps: readonly StepLike[],
  roleLabel: (roleId: string) => string,
  needsFixing: ReadonlySet<string>,
  trials: readonly TrialLike[],
  noRoleTitle: string,
): PreviewGroup[] {
  const ids = new Set(steps.map(step => step.id))
  const groups = new Map<string, PreviewGroup>()
  for (const step of steps) {
    const key = (step.role ?? '').trim()
    if (!groups.has(key)) groups.set(key, { key, title: key ? roleLabel(key) : noRoleTitle, index: 0, tiles: [] })
    groups.get(key)!.tiles.push({
      id: step.id,
      name: step.name || step.id,
      kind: step.type ?? '',
      gated: Boolean(step.gate && step.gate !== 'none'),
      responsibility: (step.responsibility ?? '').trim(),
      depends: new Set((step.depends_on ?? []).filter(dep => dep !== step.id && ids.has(dep))).size,
      status: previewStatus(step.id, needsFixing, trials),
    })
  }
  // Lanes in the order the flow first uses them; steps with no role yet come last -- they are what is left to do.
  const ordered = [...groups.values()].sort((a, b) => Number(a.key === '') - Number(b.key === ''))
  ordered.forEach((group, position) => { group.index = position + 1 })
  return ordered
}

export type BuilderView = 'canvas' | 'preview' | 'package'

/** Which view an action leaves the builder in. Adding or dropping a component is something one does on the canvas,
 *  where the new step can be seen and wired: it switches there instead of adding a step nobody can see. */
export function viewAfter(view: BuilderView, action: 'add_step' | 'open_step' | 'toggle'): BuilderView {
  if (action === 'add_step') return 'canvas'
  if (action === 'toggle') return view === 'canvas' ? 'preview' : 'canvas'
  return view
}
