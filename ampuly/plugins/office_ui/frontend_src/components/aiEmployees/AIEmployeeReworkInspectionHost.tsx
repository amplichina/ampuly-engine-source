import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { VisualSocketClient } from '../../lib/wsClient'
import { AIEmployeeReworkInspectionController } from '../../lib/aiEmployeeReworkInspectionController'
import type { ReworkInspectionBinding } from '../../lib/aiEmployeeReworkInspectionScope'
import { AIEmployeeReworkInspection } from './AIEmployeeReworkInspection'
import { AIEmployeeReworkPendingHost } from './AIEmployeeReworkPendingHost'

type Transport = Pick<VisualSocketClient, 'send' | 'addAckObserver'>
interface Props {
  binding: ReworkInspectionBinding | null
  connected: boolean
  transport: Transport | null
}
type Owned = { controller: AIEmployeeReworkInspectionController; revision: string; connected: boolean; transport: Transport }
const EMPTY = Object.freeze({ generation: 0, owner: false, connected: false,
  scope: null, state: 'idle' as const, request: null, ack: null, message: '', disposed: false })
const noSubscription = () => () => undefined
const emptySnapshot = () => EMPTY

/** One manual read in the existing socket. No mount/poll/reconnect operation. */
export function AIEmployeeReworkInspectionHost({ binding, connected, transport }: Props) {
  const [owned, setOwned] = useState<Owned | null>(null)
  const timer = useRef<{ controller: AIEmployeeReworkInspectionController; id: number } | null>(null)
  const revision = binding?.context_revision ?? null
  const usable = !!binding && owned?.revision === revision && owned.connected === connected && owned.transport === transport
  const controller = usable ? owned.controller : null
  const snapshot = useSyncExternalStore(controller?.subscribe ?? noSubscription,
    controller?.getSnapshot ?? emptySnapshot, emptySnapshot)

  useEffect(() => {
    if (!binding || !transport) { setOwned(null); return }
    // Created per effect setup, not a disposed cached render instance: React
    // StrictMode's setup/cleanup/setup therefore gets a fresh bounded reader.
    const reader = new AIEmployeeReworkInspectionController({
      send: request => transport.send({ ...request }),
      newRequestId: () => `rework-read-${globalThis.crypto.randomUUID()}`,
    })
    reader.updateContext({ owner: true, connected, scope: binding.scope, context_revision: binding.context_revision })
    let live = true
    let unsubscribe: () => void = () => undefined
    try {
      unsubscribe = transport.addAckObserver(payload => {
        if (!live) return
        try {
          if (reader.receiveAckPayload(payload) && reader.getSnapshot().state !== 'pending'
            && timer.current?.controller === reader) {
            window.clearTimeout(timer.current.id); timer.current = null
          }
        } catch { /* fixed reader status/timeout, never throw into shared ACK */ }
      })
    } catch { reader.dispose(); return }
    setOwned({ controller: reader, revision: binding.context_revision, connected, transport })
    return () => {
      live = false
      if (timer.current?.controller === reader) { window.clearTimeout(timer.current.id); timer.current = null }
      try { unsubscribe() } catch { /* live=false also fences a stale observer */ }
      finally { reader.dispose() }
    }
    // The private revision contains every scope identity and actor. Fresh
    // equivalent scope objects must not repeatedly cancel an in-flight read.
  }, [revision, connected, transport])

  if (!binding) return null
  const read = () => {
    if (!controller || !connected || !snapshot.owner || snapshot.disposed || snapshot.state === 'pending') return
    if (timer.current) { window.clearTimeout(timer.current.id); timer.current = null }
    const token = controller.inspect()
    if (token.kind !== 'pending') return
    const ownedTimer = { controller, id: window.setTimeout(() => {
      if (timer.current !== ownedTimer) return
      timer.current = null
      controller.timeout(token.request_id, token.generation)
    }, token.timeout_ms) }
    timer.current = ownedTimer
  }
  return <details className="inbox-lesson" data-testid="ai-employee-rework-inspection-host">
    <summary>返工记录核对</summary>
    <AIEmployeeReworkPendingHost binding={binding} connected={connected} transport={transport} />
    <p>以下是旧格式返工记录的只读核对。</p>
    <button type="button" className="shell-btn" data-action="human_review_rework_inspect"
      disabled={!controller || !connected || !snapshot.owner || snapshot.disposed || snapshot.state === 'pending'} onClick={read}>
      {snapshot.state === 'pending' ? '正在核对，等回执' : '核对原记录'}
    </button>
    {snapshot.message && <p role="status">{snapshot.message}</p>}
    {snapshot.request && <AIEmployeeReworkInspection request={snapshot.request}
      ack={snapshot.ack} state={snapshot.state} connected={connected} />}
    {!connected && <p role="status">连接已断开，未沿用旧结果；重连后由你重新核对。</p>}
  </details>
}
