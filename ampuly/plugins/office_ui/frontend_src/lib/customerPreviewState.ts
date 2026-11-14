import type { CustomerPreviewView } from '../components/CustomerPreviewPanel'

/** Validate the wire projection before it reaches React's render path. */
export function parseCustomerPreviewView(value: unknown, orderId: string): CustomerPreviewView | null {
  const record = (item: unknown): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item)
  if (!record(value) || value.order_id !== orderId
    || !['order_id', 'customer_id', 'status', 'brand_name'].every(key => typeof value[key] === 'string')
    || !record(value.fields) || !Object.values(value.fields).every(field => typeof field === 'string')
    || (value.fields_rule !== 'declared' && value.fields_rule !== 'fence')
    || typeof value.can_accept !== 'boolean'
    || typeof value.stages_hidden !== 'number' || !Number.isInteger(value.stages_hidden) || value.stages_hidden < 0
    || !Array.isArray(value.stages) || !value.stages.every(stage => record(stage) && typeof stage.at === 'string' && typeof stage.stage === 'string')
    || !Array.isArray(value.deliverables) || !value.deliverables.every(item => record(item) && typeof item.name === 'string' && typeof item.at === 'string')) return null
  const acceptance = value.acceptance
  if (!record(acceptance) || !['status', 'decided_at', 'note'].every(key => typeof acceptance[key] === 'string')
    || (acceptance.round_no !== undefined && (typeof acceptance.round_no !== 'number' || acceptance.round_no < 1))
    || !Array.isArray(acceptance.criteria) || !acceptance.criteria.every(item => typeof item === 'string')) return null
  return value as unknown as CustomerPreviewView
}

export interface CustomerPreviewRequest { projectId: string; orderId: string; requestId: string }

/** Whether a customer_view_preview reply answers the preview still asked for: the request out, for the project on
 *  screen, the same order and request id. Whether the reply is the project on screen's at all is lib/currentProject's
 *  gate (accepts), asked first by the handler. */
export function acceptsCustomerPreview(reply: Record<string, unknown>, pending: CustomerPreviewRequest | null, activeProject: string): boolean {
  return !!pending && !!pending.requestId
    && pending.projectId === activeProject && reply.order_id === pending.orderId
    && reply.request_id === pending.requestId
}
