import type { WorkflowSummary } from '../components/WorkflowsPage'

// A save's answer carries the package as it is now saved (ws_handler `workflow_update_nodes` -> `workflow`, the same
// `to_payload()` shape as each `workflow_list` row). The draft is dropped in the same step, so the builder must draw
// from this package at once: left to the follow-up `workflow_list`, the canvas showed the package from before the save
// for the length of that round trip -- a dragged card jumped back and returned, a renamed step flashed its old name
// (builder_save_refresh_probe caught the card mid-jump). Only a row already listed is replaced; a save never adds one.
export function withSavedWorkflow(list: WorkflowSummary[], saved: unknown): WorkflowSummary[] {
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return list
  const id = (saved as { id?: unknown }).id
  if (typeof id !== 'string' || !id) return list
  const at = list.findIndex(item => item.id === id)
  if (at < 0) return list
  const next = list.slice()
  next[at] = saved as WorkflowSummary
  return next
}
