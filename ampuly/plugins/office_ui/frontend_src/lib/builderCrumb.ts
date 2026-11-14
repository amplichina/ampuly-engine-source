// What the top bar says about the flow open in the builder (frontend refactor U2-2). Pure.
//
// Two different facts, never merged into one tag: the package's real status (draft / testing / published / retired --
// the server's), and whether this browser holds changes it has not saved.
//
// Which project a flow belongs to (policy): a flow package is global -- a template projects adopt at a
// pinned version -- unless it was derived for one project (派生为本项目草稿), when it carries that project and where it
// came from. The crumb says which of the two it is; the project selector beside it still decides where requests go.

export type BuilderCrumb = {
  id: string
  name: string
  version: string
  status: string
  /** This browser has an open draft of the steps: changes not saved yet. */
  unsaved: boolean
  steps: number
  wires: number
  /** The project this flow was derived for; '' for a global flow (a template projects adopt). */
  project: string
  /** Where a derived flow came from ("document-review v1.0.0"); '' otherwise. */
  derivedFrom: string
}

type FlowLike = {
  id: string; name: string; version: string; status: string; nodes: ReadonlyArray<{ id: string; depends_on?: readonly string[] }>
  project_id?: string; derived_from?: { id?: string; version?: string } | null
}

export function builderCrumb(flow: FlowLike, draftNodes: FlowLike['nodes'] | null): BuilderCrumb {
  // What is counted is what is on the canvas: the draft while one is open.
  const nodes = draftNodes ?? flow.nodes
  const ids = new Set(nodes.map(node => node.id))
  let wires = 0
  for (const node of nodes) wires += new Set((node.depends_on ?? []).filter(dep => dep !== node.id && ids.has(dep))).size
  const from = flow.derived_from?.id ? `${flow.derived_from.id}${flow.derived_from.version ? ` v${flow.derived_from.version}` : ''}` : ''
  return {
    id: flow.id, name: flow.name || flow.id, version: flow.version, status: flow.status, unsaved: draftNodes !== null, steps: nodes.length, wires,
    project: String(flow.project_id ?? '').trim(), derivedFrom: from,
  }
}

export function sameCrumb(a: BuilderCrumb | null, b: BuilderCrumb | null): boolean {
  if (a === null || b === null) return a === b
  return a.id === b.id && a.name === b.name && a.version === b.version && a.status === b.status
    && a.unsaved === b.unsaved && a.steps === b.steps && a.wires === b.wires && a.project === b.project && a.derivedFrom === b.derivedFrom
}
