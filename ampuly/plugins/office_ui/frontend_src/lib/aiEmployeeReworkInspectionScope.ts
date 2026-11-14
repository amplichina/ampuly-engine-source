import type { ReworkInspectionRequest } from './aiEmployeeReworkInspection'

type Scope = Readonly<Pick<ReworkInspectionRequest, 'project_id' | 'order_id' | 'task_id'>>
export type ReworkInspectionBinding = Readonly<{ scope: Scope; context_revision: string }>
export interface ReworkInspectionScopeInput {
  hydrated: boolean
  pendingProjectId: string | null
  activeProjectId: unknown
  acceptedOrderProjectId: unknown
  generation: number
  account: { role: string; login: string; disabled: boolean; customer_ids?: string[] } | null | undefined
  order: { order_id: unknown; launched_task_id: unknown }
}
const safeId = (value: unknown): value is string => typeof value === 'string'
  && value.length >= 1 && value.length <= 256 && /^[A-Za-z0-9_.:-]+$/.test(value)
  && !value.includes('\n') && !value.includes('\r')

/** UI selection only. The RO server independently proves owner and exact root. */
export function reworkInspectionBinding(input: ReworkInspectionScopeInput): ReworkInspectionBinding | null {
  const account = input.account
  if (input.hydrated !== true || input.pendingProjectId !== null
    || !Number.isSafeInteger(input.generation) || input.generation < 0
    || !safeId(input.activeProjectId) || input.acceptedOrderProjectId !== input.activeProjectId
    || !account || account.role !== 'owner' || account.disabled !== false
    || typeof account.login !== 'string' || account.login.length === 0
    || new TextEncoder().encode(account.login).length > 64
    || (account.customer_ids !== undefined && (!Array.isArray(account.customer_ids) || account.customer_ids.length !== 0))
    || !safeId(input.order.order_id) || !safeId(input.order.launched_task_id)) return null
  const scope = Object.freeze({ project_id: input.activeProjectId,
    order_id: input.order.order_id, task_id: input.order.launched_task_id })
  // Private selection identity only. No login or context revision is sent,
  // rendered, logged or treated as an authorization/operation token.
  const context_revision = JSON.stringify([account.login, input.generation,
    scope.project_id, scope.order_id, scope.task_id])
  return Object.freeze({ scope, context_revision })
}
