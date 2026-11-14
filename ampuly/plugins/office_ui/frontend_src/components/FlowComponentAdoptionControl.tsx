import { useEffect, useMemo, useRef, useState } from 'react'
import type { FlowComponentCandidate } from '../lib/flowComponentCandidate'
import { FlowComponentAdoptionController, type ComponentAdoptionState, type DraftSendResult } from '../lib/flowComponentAdoption'

export function FlowComponentAdoptionControl({ candidate, messageId, connected, onSend, ack }: {
  candidate: FlowComponentCandidate; messageId: string; connected: boolean
  onSend: (payload: Record<string, unknown>) => DraftSendResult; ack?: Record<string, unknown> | null
}) {
  const [state, setState] = useState<ComponentAdoptionState>({ phase: 'idle', message: '' })
  const sendRef = useRef(onSend)
  sendRef.current = onSend
  const controller = useMemo(() => new FlowComponentAdoptionController({ candidateMessageId: messageId,
    candidateId: candidate.candidate_id, source: candidate.source, send: payload => sendRef.current(payload),
    nextId: () => crypto.randomUUID(), changed: setState }), [messageId, candidate.candidate_id, candidate.source.project_id])
  useEffect(() => () => controller.dispose(), [controller])
  useEffect(() => { if (ack) controller.accept(ack) }, [controller, ack])
  const busy = state.phase === 'sending' || state.phase === 'inspecting'
  return <div data-testid="flow-component-adoption">
    {state.message && <span role="status">{state.message}</span>}
    {state.phase !== 'done' && <button type="button" disabled={!connected || busy}
      onClick={() => state.phase === 'ready' ? controller.confirm() : state.phase === 'unknown' ? controller.lookup() : controller.inspect()}>
      {busy ? '处理中…' : state.phase === 'ready' ? `采用到「${state.workflowName}」` : state.phase === 'unknown' ? '核对本次结果' : '采用为草稿'}
    </button>}
  </div>
}
