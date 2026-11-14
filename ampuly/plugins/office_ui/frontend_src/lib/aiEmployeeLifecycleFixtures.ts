import assert from 'node:assert/strict'
import { EmployeeLifecycleController, LIFECYCLE_ACTIONS, lifecycleCandidateKey } from './aiEmployeeLifecycleController'
import type { LifecycleCandidate } from './aiEmployeeLifecycleController'
type Row = Record<string, unknown>
export const CANDIDATE: LifecycleCandidate = { project_id: 'project-a', package_id: 'support', revision: 'a'.repeat(64), fingerprint: 'b'.repeat(64) }
export const TRIAL = 'et-' + '1'.repeat(24)
export const CONTEXT = { model_config: { provider: 'test-provider', model: 'test-model', tier: 'default' }, capability_impls: {},
  runtime_profile_fingerprint: 'c'.repeat(64), params_fingerprint: 'd'.repeat(64), checker_versions: { schema: 'v1' }, skill_tree_fingerprints: {} }
export const BUDGET = { max_cost_cny: '0.100000', max_model_calls: 2, max_wall_clock_s: 60 }
export function lifecycleFixture() {
  let id = 0, timerId = 0, projectId: string | null = CANDIDATE.project_id, writable = true
  let result: 'sent' | 'queued' | 'queue-full' | 'send-failed' = 'sent'
  const sent: Readonly<Row>[] = [], timers = new Map<number, { callback: () => void; delay: number }>()
  const controller = new EmployeeLifecycleController({ nextRequestId: () => `lifecycle-${++id}`,
    projectId: () => projectId, canWrite: c => writable && c.project_id === projectId,
    send(message) { sent.push(message); return result },
    setTimer(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId }, clearTimer(handle) { timers.delete(handle as number) },
  })
  controller.connectionChanged(true)
  const ack = (requestId: string, extra: Row = {}): Row => {
    const req = sent.find(r => r.request_id === requestId); assert.ok(req)
    return { type: 'ack', payload: { action: req.type, project_id: req.project_id, request_id: requestId, ok: true, result: 'success',
      effects_produced: [LIFECYCLE_ACTIONS.start, LIFECYCLE_ACTIONS.review, LIFECYCLE_ACTIONS.publish].includes(req.type as never),
      external_effects_produced: req.type === LIFECYCLE_ACTIONS.start ? null : false,
      allowed_actions: [], problems: [], package_id: CANDIDATE.package_id,
      fingerprint: CANDIDATE.fingerprint, revision: CANDIDATE.revision, sample_count: 1, context: CONTEXT, ...extra } }
  }
  const prepare = (): string => {
    const requestId = controller.prepare(CANDIDATE)
    assert.equal(controller.receive(ack(requestId, { data_scope: 'package_taskset', human_quality_review_required: true,
      allowed_actions: [LIFECYCLE_ACTIONS.start] })), true)
    return requestId
  }
  const key = lifecycleCandidateKey(CANDIDATE)
  const trial = (startRequestId: string, state = 'awaiting_review', extra: Row = {}): Row => ({
    trial_id: TRIAL, state, test_request_id: startRequestId, candidate_revision: CANDIDATE.revision,
    review_request_id: ['approved', 'rejected'].includes(state) ? 'review-prior' : null,
    decision: state === 'approved' ? 'approve' : state === 'rejected' ? 'reject' : null,
    revision: 'e'.repeat(64), test_passed: state === 'approved', cases: [{ id: 'case-a', input: 'Synthetic package task', expected: 'No external actions' }],
    observations: [{ case_id: 'case-a', completed: true, hard_fail_count: 0, output: '{"answer":"Synthetic result"}', model: CONTEXT.model_config.model }],
    allowed_actions: state === 'awaiting_review' ? [LIFECYCLE_ACTIONS.review] : state === 'approved' ? [LIFECYCLE_ACTIONS.publish] : [], ...extra,
  })
  const ready = (state = 'awaiting_review'): string => {
    prepare(); const rid = controller.startTest(key, BUDGET, 'Explicit fixture verification')
    assert.equal(controller.receive(ack(rid, trial(rid, state))), true); return rid
  }
  const timeout = (): void => { const callbacks = [...timers.values()]; callbacks.forEach(t => t.callback()) }
  return { controller, sent, timers, key, ack, prepare, trial, ready, timeout,
    project(value: string | null) { projectId = value }, canWrite(value: boolean) { writable = value }, disposition(value: typeof result) { result = value } }
}

