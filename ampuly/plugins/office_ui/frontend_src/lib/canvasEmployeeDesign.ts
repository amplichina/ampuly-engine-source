import type { EmployeeAssetController } from './aiEmployeeAssetController'
import { employeeAssetWriteBlocked, projectEmployeeInstance } from './aiEmployeeAssetAdapter'
import { employeeBindingFrom, type PortableEmployeeBinding } from './workflowEmployeeBinding'
import { encodeAIEmployeeCandidate, parseAIEmployeeCandidate } from '../components/aiEmployees/AIEmployeeShelf'
import type { AIEmployeeInstanceRef, AIEmployeeInstanceView } from '../components/aiEmployees/AIEmployeeLibrary'

export interface CanvasEmployeeDesign {
  controller: EmployeeAssetController
  project_id: string | null
  workflows_read_project_id: string | null
  onOpenLibrary: () => void
}

/** Resolve a drag back to the current, correlated instance list. Display names,
 * readiness and authorizations never come from a drag payload. */
export function currentCanvasEmployee(design: CanvasEmployeeDesign | undefined,
  reference: AIEmployeeInstanceRef): AIEmployeeInstanceView | null {
  if (!design?.project_id || design.project_id !== design.workflows_read_project_id) return null
  const payload = encodeAIEmployeeCandidate(reference)
  if (!payload || !parseAIEmployeeCandidate(payload, design.project_id)) return null
  const state = design.controller.getSnapshot()
  const list = state.projects[design.project_id]?.instances
  if (!state.connected || state.writes.some(item => employeeAssetWriteBlocked(item.receipt, false))
      || state.project_id !== design.project_id || list?.state !== 'ready'
      || !Array.isArray(list.data?.instances)) return null
  try {
    const matches = list.data.instances.map(raw => projectEmployeeInstance(raw, design.project_id!))
      .filter(item => item.instance_id === reference.instance_id)
    if (matches.length !== 1) return null
    const match = matches[0]
    return match.instance_revision === reference.instance_revision && match.package_id === reference.package_id
      && match.package_version === reference.package_version ? match : null
  } catch { return null }
}

export function bindingForCanvasEmployee(reference: AIEmployeeInstanceRef,
  previous?: PortableEmployeeBinding | null): PortableEmployeeBinding | null {
  // Existing human review/mappings are never removed when changing executor.
  return employeeBindingFrom({ ...(previous ?? {}), package_id: reference.package_id,
    version_constraint: { mode: 'exact', version: reference.package_version }, primary_slot: 'executor' })
}
