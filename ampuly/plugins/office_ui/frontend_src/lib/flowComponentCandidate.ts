/** Display data only. Adoption reloads the original server message/reference. */
export interface FlowDraftReference { schema_version: 1; project_id: string; message_id: string; draft_sha256: string }
export interface FlowComponentCandidate {
  candidate_id: string; kind: 'skill' | 'flow'; source: FlowDraftReference; step_id: string; capability: string
  status: 'draft'; tested: false; admitted: false; effects_produced: false
  payload: { skill_id?: string; files?: Record<string, string>; process_spec?: unknown; flow_draft?: unknown }
}

const id = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/
export function draftReference(value: unknown): FlowDraftReference | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const v = value as Record<string, unknown>
  if (v.schema_version !== 1 || typeof v.project_id !== 'string' || !id.test(v.project_id)
    || typeof v.message_id !== 'string' || !id.test(v.message_id)
    || typeof v.draft_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(v.draft_sha256)) return null
  return { schema_version: 1, project_id: v.project_id, message_id: v.message_id, draft_sha256: v.draft_sha256 }
}

export function componentCandidatesFrom(metadata: unknown): FlowComponentCandidate[] {
  if (!metadata || typeof metadata !== 'object') return []
  const raw = (metadata as Record<string, unknown>).master_component_candidates
  if (!Array.isArray(raw)) return []
  const out: FlowComponentCandidate[] = []
  for (const item of raw.slice(0, 4)) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const v = item as Record<string, unknown>, source = draftReference(v.source)
    if (!source || typeof v.candidate_id !== 'string' || !/^component-[0-9a-f]{24}$/.test(v.candidate_id)
      || !['skill', 'flow'].includes(String(v.kind)) || typeof v.step_id !== 'string' || !id.test(v.step_id)
      || v.status !== 'draft' || v.tested !== false || v.admitted !== false || v.effects_produced !== false
      || !v.payload || typeof v.payload !== 'object' || Array.isArray(v.payload)) continue
    const payload = v.payload as FlowComponentCandidate['payload']
    if (v.kind === 'skill' && (typeof payload.skill_id !== 'string' || !/^[a-z][a-z0-9_-]{1,63}$/.test(payload.skill_id)
      || !payload.files || Object.keys(payload.files).sort().join(',') !== 'SKILL.md,tests/contract.md'
      || Object.values(payload.files).some(text => typeof text !== 'string' || text.length > 65536))) continue
    if (v.kind === 'flow' && (!payload.flow_draft || typeof payload.flow_draft !== 'object')) continue
    out.push({ candidate_id: v.candidate_id, kind: v.kind as 'skill' | 'flow', source, step_id: v.step_id,
      capability: typeof v.capability === 'string' ? v.capability.slice(0, 160) : '', status: 'draft',
      tested: false, admitted: false, effects_produced: false, payload })
  }
  return out
}

export function continueDraftText(source: FlowDraftReference): string {
  return `请继续这份原流程草稿，先调用 master_resume_flow_draft 核对原消息和修订，再讨论修改：${JSON.stringify({ source })}`
}
