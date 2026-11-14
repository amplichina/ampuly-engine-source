import type { FlowTrialSummary, FlowTrialTarget, PublishedFlowTrialCandidate } from '../components/BuilderActionDialogs'

type Trial = NonNullable<FlowTrialSummary>
const sha = /^[a-f0-9]{64}$/
const packageId = /^[a-z0-9][a-z0-9_-]{2,63}$/
const text = (value: unknown, limit: number): value is string => typeof value === 'string' && value.length > 0 && value.length <= limit && !/[\u0000-\u001f\u007f]/.test(value)

/** A malformed catalog is unavailable, not an empty catalog or a partly trusted list. */
export function readPublishedFlowCandidates(value: unknown, workflowId: string): PublishedFlowTrialCandidate[] | null {
  if (!Array.isArray(value) || value.length > 256) return null
  const seen = new Set<string>()
  const rows: PublishedFlowTrialCandidate[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const row = item as Record<string, unknown>
    if (!text(row.package_id, 64) || !packageId.test(row.package_id)
      || !text(row.package_fingerprint, 64) || !sha.test(row.package_fingerprint)
      || !text(row.package_version, 128) || !text(row.workflow_version, 128)
      || row.workflow_id !== workflowId || !text(row.name, 256)) return null
    const key = `${row.package_id}:${row.package_fingerprint}`
    if (seen.has(key)) return null
    seen.add(key)
    rows.push({ package_id: row.package_id, package_version: row.package_version, package_fingerprint: row.package_fingerprint,
      workflow_id: workflowId, workflow_version: row.workflow_version, name: row.name })
  }
  return rows
}

/** This key keeps pending writes for draft/published versions separate. Project changes clear the owning map. */
export function flowTrialTargetKey(workflowId: string, target: FlowTrialTarget): string | null {
  if (!text(workflowId, 64) || !text(target.workflowVersion, 128)) return null
  if (target.source === 'saved_draft') {
    if (target.packageId !== undefined || target.fingerprint !== undefined) return null
    return JSON.stringify([workflowId, target.workflowVersion, 'saved_draft'])
  }
  if (target.source !== 'published_employee' || !target.packageId || !packageId.test(target.packageId)
    || !target.fingerprint || !sha.test(target.fingerprint)) return null
  return JSON.stringify([workflowId, target.workflowVersion, target.packageId, target.fingerprint])
}

export function recordedFlowTrialTarget(trial: Trial): FlowTrialTarget | null {
  const freeze = trial.freeze
  if (freeze && (freeze.freeze_source !== 'employee_packages_published'
    || freeze.workflow_version !== trial.workflow_version)) return null
  const target: FlowTrialTarget = { source: freeze ? 'published_employee' : 'saved_draft', workflowVersion: trial.workflow_version,
    flowId: trial.flow_id, ...(freeze ? { packageId: freeze.package_id, fingerprint: freeze.package_fingerprint } : {}) }
  return flowTrialTargetKey(trial.workflow_id, target) ? target : null
}

/** Continue/stop can only name the original record. A missed lookup must never become start. */
export function matchingFlowTrial(rows: readonly Trial[], workflowId: string, target: FlowTrialTarget): Trial | null {
  if (!target.flowId) return null
  const key = flowTrialTargetKey(workflowId, target)
  if (!key) return null
  const matches = rows.filter(row => row.flow_id === target.flowId && row.workflow_id === workflowId)
  if (matches.length !== 1) return null
  const row = matches[0]
  const recorded = recordedFlowTrialTarget(row)
  return recorded && row.phase && row.phase !== 'finished' && flowTrialTargetKey(workflowId, recorded) === key ? row : null
}

/** A new published round must come from the server's current catalog, never from a text field or stale record. */
export function selectableFlowTrialTarget(rows: readonly PublishedFlowTrialCandidate[], workflowId: string, target: FlowTrialTarget): boolean {
  return Boolean(flowTrialTargetKey(workflowId, target)) && !target.flowId && (target.source === 'saved_draft'
    || rows.some(row => row.workflow_id === workflowId && row.workflow_version === target.workflowVersion
      && row.package_id === target.packageId && row.package_fingerprint === target.fingerprint))
}
