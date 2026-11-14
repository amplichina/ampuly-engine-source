/** Receipt expiry is unknown, never permission to automatically replay. */
export type BuilderTrialRequest = {
  requestId: string
  projectId: string
  specId: string
  version: string
  phase: 'pending' | 'unknown'
}

export function matchesBuilderTrialReceipt(request: BuilderTrialRequest | null, payload: Record<string, unknown>): boolean {
  return !!request && payload.request_id === request.requestId
    && payload.project_id === request.projectId
    && payload.spec_id === request.specId && payload.version === request.version
}

/** A request id that works over plain HTTP too: crypto.randomUUID only exists in secure contexts. */
export function newRequestId(cryptoApi: { randomUUID?: () => string } | undefined = globalThis.crypto): string {
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function canReconcileBuilderTrial(request: BuilderTrialRequest | null, projectId: string,
  specId: string, version: string, rounds: Array<{ outcome: string }>): boolean {
  return !!request && request.phase === 'unknown' && request.projectId === projectId
    && request.specId === specId && request.version === version
    && !rounds.some(round => round.outcome === 'running')
}
