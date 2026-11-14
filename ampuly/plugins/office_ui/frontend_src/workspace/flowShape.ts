// 分支 / 汇合 on the builder cards (4.7 plan §174: branches, parallel work and joins are visible). From the wires alone:
// the steps a step continues from, and the steps that continue from it. Structure only -- when a step may start is the
// runtime's to decide (most dependencies there are soft: a step may begin once its upstream is under way), so nothing
// here says "waits for all" or "runs at the same time".

type Wired = { id: string; name?: string; depends_on?: readonly string[] }

export interface StepShape {
  /** The steps this one continues from, when there are two or more: a join. */
  joinFrom: string[]
  /** The steps that continue from this one, when there are two or more: the flow splits here. */
  splitTo: string[]
}

/** Only steps where the flow joins or splits; ids in the flow's own order. A wire to a step not in the flow, to itself or
 *  named twice counts once or not at all, as the canvas draws it. */
export function flowShape(steps: readonly Wired[]): Map<string, StepShape> {
  const ids = new Set(steps.map(step => step.id))
  const upstream = new Map<string, string[]>()
  const downstream = new Map<string, string[]>()
  for (const step of steps) {
    const from = [...new Set(step.depends_on ?? [])].filter(dep => ids.has(dep) && dep !== step.id)
    upstream.set(step.id, from)
    for (const dep of from) downstream.set(dep, [...(downstream.get(dep) ?? []), step.id])
  }
  const shapes = new Map<string, StepShape>()
  for (const step of steps) {
    const joinFrom = upstream.get(step.id) ?? []
    const splitTo = downstream.get(step.id) ?? []
    if (joinFrom.length > 1 || splitTo.length > 1) {
      shapes.set(step.id, { joinFrom: joinFrom.length > 1 ? joinFrom : [], splitTo: splitTo.length > 1 ? splitTo : [] })
    }
  }
  return shapes
}

/** What a card's marker says, and its longer hover text naming the steps. */
export function shapeWords(kind: 'join' | 'split', names: readonly string[], zh: boolean): { label: string; title: string } {
  const list = names.join(zh ? '、' : ', ')
  if (kind === 'join') {
    return zh
      ? { label: `汇合 ${names.length}`, title: `接在这 ${names.length} 步之后：${list}` }
      : { label: `joins ${names.length}`, title: `Continues from these ${names.length} steps: ${list}` }
  }
  return zh
    ? { label: `分出 ${names.length}`, title: `之后有 ${names.length} 步从这里接着做：${list}` }
    : { label: `splits ${names.length}`, title: `${names.length} steps continue from here: ${list}` }
}
