import { EmployeeInstanceAdoptionController } from './aiEmployeeInstanceAdoptionController'
import type { InstanceAdoptionCandidate } from './aiEmployeeInstanceAdoptionController'
export const ADOPTION_CREATE: InstanceAdoptionCandidate = { kind: 'create', project_id: 'project-a', package_id: 'support-agent',
  package_version: '2.0', package_fingerprint: 'a'.repeat(64), package_revision: 'b'.repeat(64), instance_id: null, instance_revision: null, instance_fingerprint: null }
export const ADOPTION_UPGRADE: InstanceAdoptionCandidate = { ...ADOPTION_CREATE, kind: 'adopt', instance_id: 'ei-0123456789abcdef', instance_revision: 3, instance_fingerprint: 'c'.repeat(64) }
type Row = Record<string, unknown>
export function adoptionFixture() {
  let id = 0, timer = 0, project = 'project-a', allowed = true, result: 'sent' | 'queued' | 'queue-full' | 'send-failed' = 'sent'
  const sent: Readonly<Row>[] = [], timers = new Map<number, () => void>(), confirmed: unknown[] = []
  const controller = new EmployeeInstanceAdoptionController({ send: message => { sent.push(message); return result }, nextRequestId: () => `adoption-${++id}`,
    projectId: () => project, canWrite: () => allowed, setTimer: fn => { timers.set(++timer, fn); return timer }, clearTimer: h => { timers.delete(h as number) },
    confirmed: (candidate, receipt) => confirmed.push([candidate, receipt]) })
  controller.connectionChanged(true)
  const ack = (requestId: string, overrides: Row = {}): Row => {
    const request = sent.filter(r => r.request_id === requestId).at(-1)!, create = request.type === 'employee_instance_create'
    return { type: 'ack', payload: { ok: true, action: request.type, project_id: request.project_id, request_id: requestId,
      instance_id: create ? 'ei-fedcba9876543210' : request.instance_id, revision: create ? 1 : Number(request.expected_revision) + 1,
      fingerprint: 'd'.repeat(64), package_version: request.package_version, package_fingerprint: request.expected_fingerprint,
      ...(create ? { package_id: request.package_id } : {}), result: 'success', effects_produced: request.check_only !== true,
      original_effects_produced: true, external_effects_produced: false, retest_scope: create ? [] : ['package_version', 'model_config', 'capability_impls'],
      allowed_actions: ['employee_instance_detail'], problems: [], ...overrides } }
  }
  return { controller, sent, timers, confirmed, ack, setProject: (v: string) => { project = v }, setAllowed: (v: boolean) => { allowed = v },
    disposition: (v: typeof result) => { result = v }, timeout: () => { for (const fn of [...timers.values()]) fn() } }
}
