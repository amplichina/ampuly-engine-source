/** A read of saved account files; none of these facts grants browser control. */
export interface BrowserAccountInspectRequest {
  project_id: string
  task_id: string
  binding_key: string
  request_id: string
}

export interface BrowserInspectionSession {
  project_id: string
  task_id: string
  binding_key: string
  resource_id?: string
  control_fence?: string
  state?: string
  live?: boolean
  account?: { account_id?: string } | null
}

export type BrowserAccountInspection = { state: 'unbound'; account: null } | {
  state: 'bound'
  account: { account_id: string; present: boolean; profile_present: boolean; owner_present: boolean; pending_present: boolean; revoked: boolean }
}

export interface BrowserAccountInspectionView {
  request: BrowserAccountInspectRequest
  session_signature: string
  status: 'pending' | 'ready' | 'error'
  inspection?: BrowserAccountInspection
  error?: 'not_sent' | 'refused' | 'invalid_response' | 'timeout'
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)
}

export function browserAccountInspectRequest(project: unknown, task: unknown, binding: unknown, request: unknown): BrowserAccountInspectRequest | null {
  if (![project, task, binding, request].every(identifier)) return null
  if (!/^[a-f0-9]{16}$/.test(binding as string) || (request as string).length > 128) return null
  return { project_id: project as string, task_id: task as string, binding_key: binding as string, request_id: request as string }
}

/** Only identity/state fields participate; never serialize page text, cookies or profile paths. */
export function browserInspectionSessionSignature(row: BrowserInspectionSession): string {
  return JSON.stringify([row.project_id, row.task_id, row.binding_key, row.resource_id ?? '', row.control_fence ?? '',
    row.state ?? '', row.live === true, row.account?.account_id ?? ''])
}

export function browserInspectionMatchesSession(view: BrowserAccountInspectionView, row: BrowserInspectionSession | null | undefined): boolean {
  return Boolean(row && view.request.project_id === row.project_id && view.request.task_id === row.task_id
    && view.request.binding_key === row.binding_key && view.session_signature === browserInspectionSessionSignature(row))
}

export function browserInspectionReplyMatches(payload: Record<string, unknown>, request: BrowserAccountInspectRequest): boolean {
  return payload.action === 'browser_account_inspect' && payload.project_id === request.project_id
    && payload.task_id === request.task_id && payload.binding_key === request.binding_key && payload.request_id === request.request_id
}

export function parseBrowserAccountInspection(value: unknown): BrowserAccountInspection | null {
  const inspection = record(value)
  if (!inspection) return null
  if (inspection.state === 'unbound') return inspection.account === null ? { state: 'unbound', account: null } : null
  if (inspection.state !== 'bound') return null
  const account = record(inspection.account)
  if (!account || typeof account.account_id !== 'string' || !/^[a-f0-9]{64}$/.test(account.account_id)) return null
  const keys = ['present', 'profile_present', 'owner_present', 'pending_present', 'revoked'] as const
  if (keys.some(key => typeof account[key] !== 'boolean')) return null
  return { state: 'bound', account: {
    account_id: account.account_id, present: account.present as boolean, profile_present: account.profile_present as boolean,
    owner_present: account.owner_present as boolean, pending_present: account.pending_present as boolean, revoked: account.revoked as boolean,
  } }
}
